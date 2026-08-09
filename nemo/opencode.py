from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Engineer, find_opencode, runtime_environment
from .state import RunStore


LIMIT_MARKERS = (
    "worker local total request limit reached",
    "resourceexhausted",
    "maximum steps",
    "step limit",
)
TRANSIENT_MARKERS = LIMIT_MARKERS + (
    "rate limit",
    "too many requests",
    "temporarily unavailable",
    "service unavailable",
    "connection reset",
    "connection closed",
    "timed out",
    "timeout",
    "status code 429",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 504",
)
PERMANENT_MARKERS = (
    "invalid api key",
    "unauthorized",
    "authentication failed",
    "model not found",
    "unknown model",
    "provider not found",
    "invalid model",
)


@dataclass(slots=True)
class TurnResult:
    code: int
    session_id: str | None
    text: str = ""
    errors: list[str] = field(default_factory=list)
    limit_hit: bool = False
    transient: bool = False
    permanent: bool = False
    tool_calls: int = 0
    duration: float = 0.0
    probe_stopped: bool = False


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _error_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        for key in ("message", "name", "error"):
            if value.get(key):
                return _error_text(value[key])
    return str(value or "Unknown OpenCode error")


def _clip(value: Any, maximum: int = 2400) -> str:
    text = str(value or "").replace("\r", "").strip()
    if len(text) <= maximum:
        return text
    return text[: maximum - 1] + "…"


def _tool_summary(part: dict[str, Any]) -> tuple[str, str, str, bool]:
    tool = str(part.get("tool") or "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inputs = state.get("input") if isinstance(state.get("input"), dict) else {}
    status = str(state.get("status") or "completed")
    title = str(state.get("title") or "").strip()
    if not title:
        if tool in {"bash", "shell", "run"}:
            title = _clip(inputs.get("command"), 500)
        elif tool in {"read", "write", "edit", "patch"}:
            title = str(inputs.get("filePath") or inputs.get("path") or "file")
        elif tool in {"glob", "grep"}:
            title = str(inputs.get("pattern") or inputs.get("query") or tool)
        elif tool == "task":
            title = str(inputs.get("description") or inputs.get("prompt") or "subtask")
        else:
            title = tool
    output = state.get("output") or state.get("error") or ""
    return tool, _clip(title, 600), _clip(output), status == "error"


def _token_count(part: dict[str, Any]) -> int:
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        return 0
    total = tokens.get("total")
    if isinstance(total, (int, float)):
        return int(total)
    value = 0
    for item in tokens.values():
        if isinstance(item, (int, float)):
            value += int(item)
        elif isinstance(item, dict):
            value += sum(int(x) for x in item.values() if isinstance(x, (int, float)))
    return value


def build_agent_prompt(person: Engineer) -> str:
    base = """You are one member of a five-person professional software engineering team. You have native full-access coding tools: read, search, edit, write, shell, language servers, and Git. Use them directly. Work inside the current Git worktree only unless invoking the supplied team-chat helper.

Operating rules:
- BUILD FIRST. Spend no more than two tool calls inspecting before the first production edit.
- Do not write a long plan, placeholder tree, fake implementation, or test scaffolding before production code.
- Prefer coherent substantial modules and batched PowerShell commands over dozens of tiny files or commands.
- The shell is PowerShell 7 on Windows. Write source files only with the native write/edit/patch tools. Never generate source through Set-Content, PowerShell here-strings, echo, or redirection: shell quoting can silently corrupt code. For shell commands use Get-ChildItem and New-Item; never use Unix heredocs, brace expansion, touch, grep, or mkdir -p.
- Keep going through errors. Repair code instead of explaining what someone else should do.
- Checkpoint real work with focused Git commits. Never reset or erase another engineer's changes.
- Use team chat only for file/interface claims, blockers, and handoffs; it is coordination, not a running log.
- Do not expose credentials or inspect the .env file.
- End with NEMO_DONE only when your owned production slice is implemented and committed. End the manager's first coordination turn with NEMO_COORDINATION_READY.
"""
    if person.id == "manager":
        return base + "\nYou are the engineering manager. A deterministic controller owns initial work allocation. Acknowledge the heartbeat quickly; later own integration, conflict resolution, verification, and any code needed to make the complete product pass."
    return base + f"\nYou are {person.title}. {person.focus} You are an implementation engineer: spend the overwhelming majority of the turn writing and repairing production code."


def opencode_config(person: Engineer) -> str:
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"*": "allow"},
        "agent": {
            "nemo": {
                "description": f"Nemo {person.title} with full native coding access",
                "mode": "primary",
                "prompt": build_agent_prompt(person),
                "steps": 200,
                "permission": {"*": "allow"},
            }
        },
    }
    return json.dumps(config, separators=(",", ":"))


class OpenCodeServer:
    """One warm, role-isolated OpenCode server and its explicit session."""

    def __init__(
        self,
        store: RunStore,
        person: Engineer,
        worktree: Path,
        venv_python: Path,
    ) -> None:
        self.store = store
        self.person = person
        self.worktree = worktree.resolve()
        self.venv_python = venv_python
        self.binary = find_opencode()
        self.port = 0
        self.process: subprocess.Popen[str] | None = None
        self.log_handle: Any = None
        self.session_id = store.agent_state(person.id).get("session_id")
        self.seen_parts: set[str] = set()
        self.env = runtime_environment(person, store.root, venv_python)
        self.env["OPENCODE_CONFIG_CONTENT"] = opencode_config(person)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def healthy(self) -> bool:
        if not self.process or self.process.poll() is not None or not self.port:
            return False
        try:
            with urllib.request.urlopen(self.url + "/global/health", timeout=1.0) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def start(self) -> None:
        if self.healthy():
            return
        self.stop()
        self.port = _available_port()
        log_path = self.store.root / "logs" / f"{self.person.id}-server.log"
        self.log_handle = log_path.open("a", encoding="utf-8")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "WARN",
            ],
            cwd=self.worktree,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
        )
        self.store.write_json(
            self.store.root / "servers" / f"{self.person.id}.json",
            {"pid": self.process.pid, "port": self.port, "url": self.url, "started": time.time()},
        )
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if self.healthy():
                self.store.emit(self.person.id, "server", "Warm OpenCode runtime ready", port=self.port)
                self.store.update_agent(self.person.id, model=self.person.model)
                return
            time.sleep(0.2)
        code = self.process.poll()
        raise RuntimeError(f"OpenCode server for {self.person.title} did not become healthy (exit {code}).")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _ensure_session(self, title: str | None) -> None:
        if self.session_id:
            return
        value = self._request_json("POST", "/session", {"title": title or f"Nemo · {self.person.title}"})
        if not isinstance(value, dict) or not value.get("id"):
            raise RuntimeError(f"OpenCode did not create a session for {self.person.title}")
        self.session_id = str(value["id"])
        self.store.update_agent(self.person.id, session_id=self.session_id)
        self.store.emit(self.person.id, "session", f"Persistent session {self.session_id[:18]}… created")

    def _abort_session(self) -> None:
        if not self.session_id:
            return
        session = urllib.parse.quote(self.session_id, safe="")
        try:
            self._request_json("POST", f"/session/{session}/abort")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass

    def _normalize(self, payload: dict[str, Any], result: TurnResult) -> None:
        kind = str(payload.get("type") or "event")
        session_id = payload.get("sessionID")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
            result.session_id = session_id
            self.store.update_agent(self.person.id, session_id=session_id)
        part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        part_id = str(part.get("id") or "")
        if part_id:
            if part_id in self.seen_parts:
                return
            self.seen_parts.add(part_id)
        if kind == "step_start":
            self.store.update_agent(self.person.id, status="thinking", activity="Thinking")
            self.store.emit(self.person.id, "thinking", "Thinking")
            return
        if kind == "step_finish":
            tokens = _token_count(part)
            if tokens:
                state = self.store.agent_state(self.person.id)
                self.store.update_agent(self.person.id, tokens=int(state.get("tokens", 0)) + tokens)
            return
        if kind == "tool_use":
            tool, title, output, failed = _tool_summary(part)
            result.tool_calls += 1
            state = self.store.agent_state(self.person.id)
            self.store.update_agent(
                self.person.id,
                status="working" if not failed else "error",
                activity=f"{tool}: {title}",
                tool_calls=int(state.get("tool_calls", 0)) + 1,
            )
            self.store.emit(
                self.person.id,
                "tool_error" if failed else "tool",
                title,
                tool=tool,
                output=output,
            )
            if failed and output:
                result.errors.append(output)
            return
        if kind in {"text", "reasoning"}:
            text = str(part.get("text") or "").strip()
            if not text:
                return
            if kind == "text":
                result.text = text
                self.store.emit(self.person.id, "assistant", text)
            # Reasoning text is intentionally not persisted or displayed. The
            # honest "thinking" state above remains visible without exposing CoT.
            return
        if kind == "error":
            message = _error_text(payload.get("error"))
            result.errors.append(message)
            self.store.update_agent(self.person.id, status="error", activity=message)
            self.store.emit(self.person.id, "error", message)

    def _catch_up(self, result: TurnResult) -> None:
        """Reconcile parts the attach CLI may miss while its event stream closes."""
        if not self.session_id:
            return
        session = urllib.parse.quote(self.session_id, safe="")
        try:
            messages = self._request_json("GET", f"/session/{session}/message")
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            self.store.emit(self.person.id, "sync_error", f"Could not reconcile session transcript: {error}")
            return
        if not isinstance(messages, list):
            return
        for message in messages:
            if not isinstance(message, dict):
                continue
            info = message.get("info") if isinstance(message.get("info"), dict) else {}
            if info.get("role") != "assistant":
                continue
            for part in message.get("parts", []):
                if not isinstance(part, dict):
                    continue
                kind = str(part.get("type") or "")
                if kind == "tool":
                    state = part.get("state") if isinstance(part.get("state"), dict) else {}
                    if state.get("status") not in {"completed", "error"}:
                        continue
                    event_kind = "tool_use"
                elif kind in {"step-start", "step-finish"}:
                    event_kind = kind.replace("-", "_")
                elif kind == "text":
                    event_kind = "text"
                else:
                    # In particular, never copy private reasoning from storage.
                    continue
                self._normalize({"type": event_kind, "sessionID": self.session_id, "part": part}, result)

    def run_turn(
        self,
        message: str,
        *,
        task_file: Path | None = None,
        title: str | None = None,
        timeout: int = 4 * 60 * 60,
        completion_probe: Callable[[], bool] | None = None,
    ) -> TurnResult:
        self.start()
        self._ensure_session(title)
        command = [
            str(self.binary),
            "run",
            "--attach",
            self.url,
            "--dir",
            str(self.worktree),
            "--format",
            "json",
            "--auto",
            "--model",
            self.person.model,
            "--agent",
            "nemo",
        ]
        command += ["--session", str(self.session_id)]
        # Do not use OpenCode's multi-value --file flag here. On current Windows
        # builds it can greedily consume the following positional prompt as a
        # second filename. The role prompt carries the task and its saved path.
        command.append(message)
        attempt = int(self.store.agent_state(self.person.id).get("attempt", 0)) + 1
        self.store.update_agent(
            self.person.id,
            status="thinking",
            activity="Starting model turn",
            attempt=attempt,
            turn_started=time.time(),
        )
        self.store.emit(self.person.id, "turn_start", f"Model turn {attempt} started", attempt=attempt)
        log_path = self.store.root / "logs" / f"{self.person.id}-turn-{attempt:03}.jsonl"
        result = TurnResult(1, self.session_id)
        started = time.monotonic()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=self.worktree,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            assert process.stdout is not None
            for raw in process.stdout:
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        with log_path.open("a", encoding="utf-8") as raw_log:
            deadline = time.monotonic() + timeout
            while True:
                if completion_probe is not None and completion_probe():
                    result.probe_stopped = True
                    self._abort_session()
                    process.terminate()
                    self.store.emit(self.person.id, "handoff_stop", "Committed handoff observed; stopping redundant model tail")
                    break
                if self.store.stop_requested() or time.monotonic() >= deadline:
                    process.terminate()
                    result.errors.append("Turn stopped" if self.store.stop_requested() else "Turn timed out")
                    break
                try:
                    raw = lines.get(timeout=0.25)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if raw is None:
                    break
                raw_log.write(raw)
                raw_log.flush()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    text = raw.strip()
                    if text:
                        result.errors.append(text) if "error" in text.lower() else None
                    continue
                if isinstance(payload, dict):
                    self._normalize(payload, result)
        try:
            result.code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            result.code = process.wait(timeout=10)
        self._catch_up(result)
        result.duration = time.monotonic() - started
        joined = "\n".join(result.errors).lower()
        result.limit_hit = any(marker in joined for marker in LIMIT_MARKERS)
        result.permanent = any(marker in joined for marker in PERMANENT_MARKERS)
        result.transient = result.limit_hit or any(marker in joined for marker in TRANSIENT_MARKERS)
        status = "idle" if result.code == 0 and not result.errors else "error"
        self.store.update_agent(
            self.person.id,
            status=status,
            activity="Turn complete" if status == "idle" else _clip(result.errors[-1] if result.errors else f"Exit {result.code}", 500),
            last_exit=result.code,
            last_duration=result.duration,
        )
        self.store.emit(
            self.person.id,
            "turn_end",
            f"Model turn {attempt} exited {result.code}",
            attempt=attempt,
            duration=result.duration,
            limit_hit=result.limit_hit,
        )
        return result
