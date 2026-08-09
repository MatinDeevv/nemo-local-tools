from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_THREAD_LOCK = threading.RLock()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path, *, after: int = 0) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[after:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


@contextmanager
def file_mutex(lock_path: Path, timeout: float = 8.0) -> Iterator[None]:
    """A dependency-free inter-process lock suitable for short state writes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for state lock: {lock_path}")
            time.sleep(0.025)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


class RunStore:
    """Small event-sourced state store shared by the supervisor and TUI."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock_path = self.root / ".state.lock"

    @property
    def meta_path(self) -> Path:
        return self.root / "run.json"

    def initialize(self, meta: dict[str, Any], roles: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        for folder in ("agents", "logs", "worktrees", "servers"):
            (self.root / folder).mkdir()
        self.write_json(self.meta_path, meta)
        for role in roles:
            folder = self.root / "agents" / role
            folder.mkdir()
            self.write_json(
                folder / "state.json",
                {
                    "id": role,
                    "status": "queued",
                    "attempt": 0,
                    "tool_calls": 0,
                    "files": 0,
                    "commits": 0,
                    "tokens": 0,
                    "updated": time.time(),
                },
            )
        (self.root / "events.jsonl").touch()
        (self.root / "chat.jsonl").touch()

    def write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _THREAD_LOCK, file_mutex(self.lock_path):
            temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)

    def append(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _THREAD_LOCK, file_mutex(self.lock_path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def update_meta(self, **changes: Any) -> dict[str, Any]:
        current = read_json(self.meta_path, {}) or {}
        current.update(changes)
        current["updated"] = time.time()
        self.write_json(self.meta_path, current)
        return current

    def agent_state(self, role: str) -> dict[str, Any]:
        return read_json(self.root / "agents" / role / "state.json", {}) or {}

    def update_agent(self, role: str, **changes: Any) -> dict[str, Any]:
        path = self.root / "agents" / role / "state.json"
        current = read_json(path, {}) or {"id": role}
        current.update(changes)
        current["updated"] = time.time()
        self.write_json(path, current)
        return current

    def emit(self, role: str, kind: str, message: str, **data: Any) -> dict[str, Any]:
        event = {
            "id": uuid.uuid4().hex,
            "at": utc_now(),
            "time": time.time(),
            "role": role,
            "kind": kind,
            "message": message,
            **data,
        }
        self.append(self.root / "events.jsonl", event)
        if role not in {"controller", "team"}:
            self.append(self.root / "agents" / role / "events.jsonl", event)
        return event

    def chat(self, sender: str, target: str, text: str, kind: str = "message") -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex,
            "at": utc_now(),
            "time": time.time(),
            "from": sender,
            "to": target,
            "kind": kind,
            "text": text.strip(),
        }
        self.append(self.root / "chat.jsonl", item)
        return item

    def stop_requested(self) -> bool:
        return (self.root / "STOP").exists()
