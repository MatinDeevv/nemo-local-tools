"""Focused OpenCode-style console for a Nemo engineering run.

Only public assistant text and observable tool/Git/test activity is rendered.
Private reasoning and raw provider JSON are deliberately excluded.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, RichLog, Static

from nemo.state import read_json, read_jsonl


STATUS_GLYPH = {
    "queued": "○",
    "booting": "◌",
    "thinking": "◐",
    "working": "●",
    "waiting": "◍",
    "retrying": "↻",
    "integrating": "◆",
    "testing": "◇",
    "ready": "✓",
    "complete": "✓",
    "idle": "·",
    "error": "!",
    "failed": "×",
}


def _clip(value: Any, maximum: int = 900) -> str:
    text = str(value or "").replace("\r", "").strip()
    if len(text) <= maximum:
        return text
    return text[: maximum - 1] + "…"


def _duration(seconds: float) -> str:
    minutes, second = divmod(max(0, int(seconds)), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02}:{second:02}" if hours else f"{minute:02}:{second:02}"


def _short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1].replace("nemotron-3-", "nemo-")


def _command(command: list[str], cwd: Path, timeout: int = 6) -> str:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        ).stdout.strip()
    except Exception as error:
        return f"unavailable: {error}"


class NemoConsole(App[None]):
    TITLE = "Nemo Engineering Team"
    CSS = """
    Screen { background: #07090d; color: #d7dee9; }
    #top { height: 3; padding: 1 2 0 2; background: #0b0e14; border-bottom: solid #202735; }
    #brand { width: 18; color: #75d7ff; text-style: bold; }
    #runline { width: 1fr; color: #a7b1c2; }
    #clock { width: 30; color: #8de5ae; text-align: right; }
    #gates { height: 2; padding: 0 2; background: #0b0e14; color: #758195; }
    #tabs { height: 3; padding: 1 2 0 2; background: #080b10; }
    .agent-tab { width: 1fr; min-width: 14; color: #738096; text-align: center; padding: 0 1; }
    .active-tab { color: #f4f7fb; background: #16202e; text-style: bold; }
    #body { height: 1fr; padding: 0 2 1 2; }
    #focus { width: 1fr; border: round #293448; background: #0a0e14; }
    #focus-head { height: 3; padding: 1 1 0 1; background: #0d121a; }
    #focus-title { width: 1fr; text-style: bold; color: #edf3fb; }
    #focus-state { width: 34; text-align: right; color: #7bdcff; }
    #activity { height: 3; padding: 0 1; color: #9fdbff; background: #0d141f; border-bottom: solid #202c3e; }
    #stream { height: 1fr; padding: 1 2; background: #080b10; scrollbar-color: #314158; scrollbar-color-hover: #45607f; }
    #statusline { height: 3; padding: 1 1 0 1; background: #0d121a; border-top: solid #202c3e; color: #8795a9; }
    #rail { width: 40; margin-left: 1; }
    .rail-panel { border: round #293448; background: #0a0e14; padding: 1; }
    .rail-title { height: 2; color: #b8c6d9; text-style: bold; }
    #team-panel { height: 2fr; }
    #team { height: 1fr; }
    #chat-panel { height: 3fr; margin-top: 1; }
    #chat { height: 1fr; background: #080b10; scrollbar-color: #314158; }
    #metrics-panel { height: 2fr; margin-top: 1; }
    #metrics { height: 1fr; color: #9baabd; }
    Footer { height: 1; background: #0b0e14; color: #8391a5; }
    """
    BINDINGS = [
        ("1", "select_1", "Manager"),
        ("2", "select_2", "SWE 1"),
        ("3", "select_3", "SWE 2"),
        ("4", "select_4", "SWE 3"),
        ("5", "select_5", "SWE 4"),
        ("tab", "next_agent", "Next"),
        ("shift+tab", "previous_agent", "Previous"),
        ("c", "show_chat", "Chat"),
        ("g", "show_git", "Git"),
        ("t", "show_tests", "Tests"),
        ("e", "show_errors", "Errors"),
        ("o", "toggle_output", "Tool output"),
        ("/", "show_palette", "Commands"),
        ("r", "refresh_now", "Refresh"),
        ("q", "quit", "Detach"),
    ]

    def __init__(self, run_dir: Path):
        super().__init__()
        self.run_dir = run_dir.resolve()
        self.meta: dict[str, Any] = read_json(self.run_dir / "run.json", {}) or {}
        self.team: list[dict[str, Any]] = list(self.meta.get("team") or [])
        if not self.team:
            self.team = [{"id": "manager", "number": 1, "title": "Manager", "color": "cyan", "model": "unknown"}]
        self.roles = [str(item["id"]) for item in self.team]
        self.selected = self.roles[0]
        self.mode = "agent"
        self.show_tool_output = False
        self.event_offsets = {role: 0 for role in self.roles}
        self.chat_offset = 0
        self.last_mode_signature = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("NEMO  /  BUILD", id="brand")
            yield Static("loading…", id="runline")
            yield Static("● CONNECTING", id="clock")
        yield Static(id="gates")
        with Horizontal(id="tabs"):
            for item in self.team:
                yield Static(str(item.get("title", item["id"])), id=f"tab-{item['id']}", classes="agent-tab")
        with Horizontal(id="body"):
            with Vertical(id="focus"):
                with Horizontal(id="focus-head"):
                    yield Static(id="focus-title")
                    yield Static(id="focus-state")
                yield Static("Waiting for the first native OpenCode event…", id="activity")
                yield RichLog(id="stream", wrap=True, markup=False, max_lines=1400, auto_scroll=True)
                yield Static(id="statusline")
            with Vertical(id="rail"):
                with Vertical(id="team-panel", classes="rail-panel"):
                    yield Static("TEAM", classes="rail-title")
                    yield Static(id="team")
                with Vertical(id="chat-panel", classes="rail-panel"):
                    yield Static("TEAM CHAT  /  COORDINATION", classes="rail-title")
                    yield RichLog(id="chat", wrap=True, markup=False, max_lines=220, auto_scroll=True)
                with Vertical(id="metrics-panel", classes="rail-panel"):
                    yield Static("SELECTED SESSION", classes="rail-title")
                    yield Static(id="metrics")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.28, self.refresh_dashboard)
        self.refresh_dashboard()

    def _person(self, role: str | None = None) -> dict[str, Any]:
        target = role or self.selected
        return next((item for item in self.team if item.get("id") == target), self.team[0])

    def _state(self, role: str) -> dict[str, Any]:
        return read_json(self.run_dir / "agents" / role / "state.json", {}) or {}

    def select(self, index: int) -> None:
        if not 0 <= index < len(self.roles):
            return
        self.selected = self.roles[index]
        self.mode = "agent"
        self.event_offsets[self.selected] = 0
        self.query_one("#stream", RichLog).clear()
        self.refresh_dashboard()

    def action_select_1(self) -> None: self.select(0)
    def action_select_2(self) -> None: self.select(1)
    def action_select_3(self) -> None: self.select(2)
    def action_select_4(self) -> None: self.select(3)
    def action_select_5(self) -> None: self.select(4)
    def action_next_agent(self) -> None: self.select((self.roles.index(self.selected) + 1) % len(self.roles))
    def action_previous_agent(self) -> None: self.select((self.roles.index(self.selected) - 1) % len(self.roles))
    def action_refresh_now(self) -> None: self.refresh_dashboard()

    def action_toggle_output(self) -> None:
        self.show_tool_output = not self.show_tool_output
        self.event_offsets[self.selected] = 0
        self.query_one("#stream", RichLog).clear()
        self.mode = "agent"
        self.refresh_dashboard()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.last_mode_signature = ""
        self.query_one("#stream", RichLog).clear()
        self.refresh_dashboard()

    def action_show_chat(self) -> None: self._set_mode("chat")
    def action_show_git(self) -> None: self._set_mode("git")
    def action_show_tests(self) -> None: self._set_mode("tests")
    def action_show_errors(self) -> None: self._set_mode("errors")
    def action_show_palette(self) -> None: self._set_mode("palette")

    def refresh_dashboard(self) -> None:
        self.meta = read_json(self.run_dir / "run.json", self.meta) or self.meta
        states = {role: self._state(role) for role in self.roles}
        self._render_header(states)
        self._render_tabs(states)
        self._render_team(states)
        self._render_chat_rail()
        self._render_metrics(states[self.selected])
        if self.mode == "agent":
            self._render_agent(states[self.selected])
        else:
            self._render_mode()

    def _delivery_progress(self, states: dict[str, dict[str, Any]]) -> tuple[int, str]:
        phase = str(self.meta.get("phase", "boot"))
        if phase == "complete": return 100, "DELIVERED"
        if phase == "delivery": return 96, "MERGING VERIFIED BRANCH"
        if phase == "verification":
            passed = any(row.get("kind") == "test_pass" for row in read_jsonl(self.run_dir / "events.jsonl"))
            return (94 if passed else 84), "REAL TEST GATE"
        if phase == "integration": return 72, "MANAGER INTEGRATION"
        if phase == "coding":
            swes = self.roles[1:]
            ready = sum(states[role].get("status") in {"ready", "complete"} for role in swes)
            return 12 + int(ready * 14), f"PRODUCTION HANDOFFS {ready}/{len(swes)}"
        return 5, "RUNTIME BOOT"

    def _render_header(self, states: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        elapsed = _duration(now - float(self.meta.get("started", now)))
        status = str(self.meta.get("status", "starting"))
        phase = str(self.meta.get("phase", "boot")).upper()
        active = sum(state.get("status") in {"thinking", "working", "integrating", "testing", "retrying"} for state in states.values())
        self.query_one("#runline", Static).update(f"{self.meta.get('project', '?')}  /  {phase}")
        health_color = "#8de5ae" if status not in {"failed", "stopped"} else "#ff7b83"
        clock = self.query_one("#clock", Static)
        clock.styles.color = health_color
        clock.update(f"● {active}/{len(self.roles)} ACTIVE   {elapsed}")
        percent, label = self._delivery_progress(states)
        width = 26
        filled = round(percent * width / 100)
        bar = "━" * filled + "─" * (width - filled)
        self.query_one("#gates", Static).update(f"DELIVERY GATES  {bar}  {percent:3}%   {label}  ·  real milestones, not an ETA")

    def _render_tabs(self, states: dict[str, dict[str, Any]]) -> None:
        for item in self.team:
            role = str(item["id"])
            state = states[role]
            glyph = STATUS_GLYPH.get(str(state.get("status", "queued")), "·")
            widget = self.query_one(f"#tab-{role}", Static)
            widget.set_classes("agent-tab active-tab" if role == self.selected and self.mode == "agent" else "agent-tab")
            widget.update(f"{item.get('number', '?')}  {glyph} {item.get('title', role)}")

    def _render_team(self, states: dict[str, dict[str, Any]]) -> None:
        lines: list[str] = []
        for item in self.team:
            role = str(item["id"])
            state = states[role]
            status = str(state.get("status", "queued"))
            glyph = STATUS_GLYPH.get(status, "·")
            activity = _clip(state.get("activity", "waiting"), 34)
            lines.append(f"{item.get('number')}  {glyph} {item.get('title', role):7}  {status.upper():11}\n   {activity}")
        self.query_one("#team", Static).update("\n".join(lines))

    def _render_chat_rail(self) -> None:
        rows = read_jsonl(self.run_dir / "chat.jsonl")
        widget = self.query_one("#chat", RichLog)
        for row in rows[self.chat_offset :]:
            target = "" if row.get("to") == "all" else f" → {str(row.get('to')).upper()}"
            header = Text(f"{str(row.get('from', 'team')).upper()}{target}  ·  {row.get('kind', 'message')}", style="bold cyan")
            widget.write(header)
            widget.write(Text(_clip(row.get("text"), 420), style="#b5c0cf"))
        self.chat_offset = len(rows)

    def _render_metrics(self, state: dict[str, Any]) -> None:
        person = self._person()
        active_model = str(state.get("model") or person.get("model", "?"))
        session = str(state.get("session_id", "not created"))
        branch = str((self.meta.get("worktrees") or {}).get(self.selected, {}).get("branch", "—"))
        duration = _duration(float(state.get("last_duration", 0)))
        output = "ON" if self.show_tool_output else "errors only"
        text = (
            f"model     {_short_model(active_model)}\n"
            f"session   {session[:18]}{'…' if len(session) > 18 else ''}\n"
            f"branch    {_clip(branch, 29)}\n\n"
            f"turn      {state.get('attempt', 0)}\n"
            f"tools     {state.get('tool_calls', 0)}\n"
            f"files     {state.get('files', 0)}\n"
            f"commits   {state.get('commits', 0)}\n"
            f"tokens    {int(state.get('tokens', 0)):,}\n"
            f"last turn {duration}\n\n"
            f"tool output  {output}  [O]"
        )
        self.query_one("#metrics", Static).update(text)

    def _render_agent(self, state: dict[str, Any]) -> None:
        person = self._person()
        status = str(state.get("status", "queued"))
        self.query_one("#focus-title", Static).update(f"{person.get('number')}  {person.get('title')}  /  LIVE SESSION")
        active_for = ""
        if status in {"thinking", "working", "integrating", "testing"} and state.get("turn_started"):
            active_for = "  " + _duration(time.time() - float(state["turn_started"]))
        self.query_one("#focus-state", Static).update(f"{STATUS_GLYPH.get(status, '·')} {status.upper()}{active_for}")
        self.query_one("#activity", Static).update(f"NOW  {_clip(state.get('activity', 'Waiting for work'), 280)}")
        active_model = str(state.get("model") or person.get("model", "?"))
        self.query_one("#statusline", Static).update(
            f"{_short_model(active_model)}  ·  turn {state.get('attempt', 0)}  ·  "
            f"{state.get('tool_calls', 0)} tools  ·  {state.get('files', 0)} files  ·  {state.get('commits', 0)} commits"
        )
        events = read_jsonl(self.run_dir / "agents" / self.selected / "events.jsonl")
        stream = self.query_one("#stream", RichLog)
        for event in events[self.event_offsets[self.selected] :]:
            kind = str(event.get("kind", "event"))
            message = _clip(event.get("message"), 10000)
            if kind == "thinking":
                stream.write(Text("◐  thinking…", style="dim italic"))
            elif kind == "assistant":
                stream.write(Text("\n" + message, style="#e3e9f1"))
            elif kind in {"tool", "tool_error"}:
                tool = str(event.get("tool", "tool"))
                failed = kind == "tool_error"
                stream.write(Text(f"› {tool:10} {message}", style="bold red" if failed else "#75d7ff"))
                output = _clip(event.get("output"), 5000)
                if output and (failed or self.show_tool_output):
                    stream.write(Text("  " + output.replace("\n", "\n  "), style="red" if failed else "dim"))
            elif kind in {"error", "fatal", "git_error"}:
                stream.write(Text(f"! {message}", style="bold red"))
            elif kind in {"commit", "ready", "verified", "merge", "coordination"}:
                stream.write(Text(f"✓ {message}", style="bold green"))
            elif kind == "retry":
                stream.write(Text(f"↻ {message}", style="yellow"))
            elif kind in {"turn_start", "turn_end", "server"}:
                stream.write(Text(f"· {message}", style="dim"))
        self.event_offsets[self.selected] = len(events)

    def _swap(self, title: str, activity: str, lines: list[Text], signature: str) -> None:
        self.query_one("#focus-title", Static).update(title)
        self.query_one("#focus-state", Static).update("VIEW")
        self.query_one("#activity", Static).update(activity)
        self.query_one("#statusline", Static).update("Press 1–5 to return to a focused live session")
        if signature == self.last_mode_signature:
            return
        stream = self.query_one("#stream", RichLog)
        stream.clear()
        for line in lines:
            stream.write(line)
        self.last_mode_signature = signature

    def _render_mode(self) -> None:
        events = read_jsonl(self.run_dir / "events.jsonl")
        if self.mode == "chat":
            rows = read_jsonl(self.run_dir / "chat.jsonl")
            lines = []
            for row in rows:
                target = "" if row.get("to") == "all" else f" → {str(row.get('to')).upper()}"
                lines.append(Text(f"{str(row.get('from')).upper()}{target}  [{row.get('kind')}]", style="bold cyan"))
                lines.append(Text(_clip(row.get("text"), 2000) + "\n", style="#c3cbd7"))
            self._swap("TEAM CHAT  /  FULL COORDINATION", "Messages deliberately exclude routine log noise.", lines or [Text("No messages yet.", style="dim")], f"chat-{len(rows)}")
            return
        if self.mode == "tests":
            rows = [event for event in events if event.get("kind") in {"test_pass", "test_fail", "verified"}]
            lines = []
            for row in rows:
                passed = row.get("kind") in {"test_pass", "verified"}
                lines.append(Text(f"{'PASS' if passed else 'FAIL'}  {row.get('message')}", style="bold green" if passed else "bold red"))
                if row.get("output"):
                    lines.append(Text(_clip(row.get("output"), 8000) + "\n", style="dim"))
            self._swap("VERIFICATION  /  REAL COMMANDS", "No fake checks: these are controller-observed exit codes.", lines or [Text("Verification starts after all production branches are integrated.", style="dim")], f"tests-{len(rows)}")
            return
        if self.mode == "errors":
            rows = [event for event in events if event.get("kind") in {"error", "fatal", "tool_error", "git_error", "test_fail", "conflict"}]
            lines = [Text(f"{str(row.get('role')).upper()}  {row.get('kind')}\n{_clip(row.get('message'), 3000)}\n", style="red") for row in rows[-100:]]
            self._swap("ERRORS  /  RETRIES", "Failures stay visible; transient failures are resumed automatically.", lines or [Text("No errors recorded.", style="green")], f"errors-{len(rows)}")
            return
        if self.mode == "git":
            meta_worktrees = self.meta.get("worktrees") or {}
            manager_tree = Path(meta_worktrees.get("manager", {}).get("worktree", self.meta.get("repo", ".")))
            log = _command(["git", "log", "--all", "--oneline", "--decorate", "-20"], manager_tree)
            status = _command(["git", "status", "--short", "--branch"], manager_tree)
            value = f"COMMITS\n{log or 'No commits yet.'}\n\nMANAGER WORKTREE\n{status or 'clean'}"
            self._swap("GIT  /  ISOLATED WORKTREES", "Four branches merge only after explicit SWE handoff.", [Text(value, style="#c5cfdb")], f"git-{hash(value)}")
            return
        lines = [
            Text("1–5     switch focused engineer", style="bold cyan"),
            Text("Tab     next engineer"),
            Text("C       full team chat"),
            Text("G       Git branches and commits"),
            Text("T       real verification output"),
            Text("E       errors and automatic retries"),
            Text("O       toggle completed tool output"),
            Text("R       refresh now"),
            Text("Q       detach; team keeps running"),
            Text("\nProgress is based on completed delivery gates, never a timer or invented ETA.", style="dim"),
        ]
        self._swap("COMMANDS", "Focused controls inspired by OpenCode and Claude Code.", lines, "palette")


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach to a focused Nemo engineering console.")
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    if not (args.run / "run.json").is_file():
        raise SystemExit(f"Not a Nemo run: {args.run}")
    NemoConsole(args.run).run()


if __name__ == "__main__":
    main()
