from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_ENV, PROJECTS_ROOT, RUNTIME_ROOT, TEAM, find_opencode, load_dotenv, missing_keys
from .opencode import OpenCodeServer
from .state import RunStore, read_json
from .supervisor import prepare_new_run, run_supervisor


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _entrypoint() -> Path:
    return Path(__file__).resolve().parent.parent / "nemo.py"


def launch_worker(run_dir: Path) -> int:
    store = RunStore(run_dir)
    meta = read_json(store.meta_path, {}) or {}
    existing = meta.get("supervisor_pid") or meta.get("worker_pid")
    if _pid_alive(existing):
        return int(existing)
    log = (run_dir / "logs" / "supervisor.log").open("a", encoding="utf-8")
    creationflags = 0
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        [sys.executable, str(_entrypoint()), "_worker", "--run", str(run_dir)],
        cwd=_entrypoint().parent,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        **kwargs,
    )
    store.update_meta(worker_pid=process.pid, status="starting")
    return process.pid


def _open_tui(run_dir: Path) -> None:
    from nemo_tui import NemoConsole

    NemoConsole(run_dir).run()


def _latest_run(project: str | None = None) -> Path | None:
    root = RUNTIME_ROOT / "runs"
    if not root.is_dir():
        return None
    candidates = [path for path in root.iterdir() if path.is_dir() and (not project or path.name.startswith(project + "-"))]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _resolve_run(value: Path | None, latest: str | None = None) -> Path:
    run = value.resolve() if value else _latest_run(latest)
    if not run or not (run / "run.json").is_file():
        raise SystemExit("Nemo run not found. Pass --run or use --latest PROJECT.")
    return run


def _human_duration(seconds: float) -> str:
    minutes, second = divmod(max(0, int(seconds)), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}h {minute:02}m" if hours else f"{minute}m {second:02}s"


def print_status(run_dir: Path) -> None:
    meta = read_json(run_dir / "run.json", {}) or {}
    now = time.time()
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        console.print(
            f"[bold cyan]NEMO[/]  {meta.get('project', '?')}  ·  {str(meta.get('phase', '?')).upper()}  ·  "
            f"{str(meta.get('status', '?')).upper()}  ·  {_human_duration(now - float(meta.get('started', now)))}"
        )
        table = Table(box=None, padding=(0, 2))
        for column in ("Agent", "State", "Activity", "Tools", "Files", "Commits"):
            table.add_column(column)
        for person in TEAM:
            state = read_json(run_dir / "agents" / person.id / "state.json", {}) or {}
            table.add_row(
                person.title,
                str(state.get("status", "queued")),
                str(state.get("activity", ""))[:62],
                str(state.get("tool_calls", 0)),
                str(state.get("files", 0)),
                str(state.get("commits", 0)),
            )
        console.print(table)
        if meta.get("error"):
            console.print(f"[bold red]Error:[/] {meta['error']}")
        console.print(f"[dim]{run_dir}[/]")
    except ImportError:
        print(json.dumps(meta, indent=2))


def doctor(env_file: Path, live: bool = False) -> int:
    checks: list[tuple[str, bool, str]] = []
    try:
        binary = find_opencode()
        version = subprocess.run([str(binary), "--version"], text=True, capture_output=True, timeout=20).stdout.strip()
        checks.append(("OpenCode", True, f"{version} · {binary}"))
    except Exception as error:
        checks.append(("OpenCode", False, str(error)))
    try:
        load_dotenv(env_file)
        absent = missing_keys()
        checks.append(("NVIDIA keys", not absent, "5/5 loaded" if not absent else "missing " + ", ".join(absent)))
    except Exception as error:
        checks.append(("NVIDIA keys", False, str(error)))
    for command, name in ((["git", "--version"], "Git"), ([sys.executable, "--version"], "Python")):
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=20)
            checks.append((name, result.returncode == 0, (result.stdout or result.stderr).strip()))
        except Exception as error:
            checks.append((name, False, str(error)))
    try:
        import textual  # noqa: F401

        checks.append(("TUI", True, "Textual available"))
    except ImportError:
        checks.append(("TUI", False, "pip install -r requirements.txt"))
    for name, ok, detail in checks:
        print(f"{'OK ' if ok else 'FAIL'}  {name:12} {detail}")
    if not all(ok for _, ok, _ in checks):
        return 1
    if live:
        return live_smoke(env_file)
    return 0


def live_smoke(env_file: Path) -> int:
    """Exercise all five keys/models through warm OpenCode attach mode in parallel."""
    load_dotenv(env_file)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = RUNTIME_ROOT / "smoke" / stamp
    store = RunStore(root)
    store.initialize(
        {
            "schema": 2,
            "id": stamp,
            "project": "provider-smoke",
            "status": "running",
            "phase": "smoke",
            "started": time.time(),
            "team": [person.public_dict() for person in TEAM],
        },
        [person.id for person in TEAM],
    )
    servers: dict[str, OpenCodeServer] = {}
    for person in TEAM:
        worktree = root / "worktrees" / person.id
        worktree.mkdir()
        servers[person.id] = OpenCodeServer(store, person, worktree, Path(sys.executable))
    print("LIVE  starting five isolated OpenCode/NVIDIA runtimes…")
    results: dict[str, tuple[bool, str]] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            starts = [pool.submit(server.start) for server in servers.values()]
            for future in starts:
                future.result()

            def probe(person: Any) -> tuple[str, bool, str]:
                result = servers[person.id].run_turn(
                    "Connectivity diagnostic. Override normal coding workflow: use no tools and reply exactly NEMO_SMOKE_OK.",
                    title=f"Nemo smoke · {person.title}",
                    timeout=600,
                )
                ok = result.code == 0 and "NEMO_SMOKE_OK" in result.text
                detail = result.text[-160:] if result.text else (result.errors[-1] if result.errors else f"exit {result.code}")
                return person.id, ok, detail

            probes = [pool.submit(probe, person) for person in TEAM]
            for future in concurrent.futures.as_completed(probes):
                role, ok, detail = future.result()
                results[role] = (ok, detail)
                print(f"{'OK ' if ok else 'FAIL'}  {role:8} {detail}")
    finally:
        for server in servers.values():
            server.stop()
    passed = len(results) == 5 and all(ok for ok, _ in results.values())
    store.update_meta(status="complete" if passed else "failed", completed=time.time())
    print(f"{'PASS' if passed else 'FAIL'}  live five-key smoke · {root}")
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nemo", description="Four parallel OpenCode SWEs plus one integrating manager.")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("run", help="Create a project run and launch its live console")
    new.add_argument("--project", required=True)
    source = new.add_mutually_exclusive_group(required=True)
    source.add_argument("--task")
    source.add_argument("--task-file", type=Path)
    new.add_argument("--projects-root", type=Path, default=PROJECTS_ROOT)
    new.add_argument("--env", type=Path, default=DEFAULT_ENV)
    new.add_argument("--run-id")
    new.add_argument("--no-tui", action="store_true")
    new.add_argument("--foreground", action="store_true")
    resume = sub.add_parser("resume", help="Resume exact sessions from an interrupted run")
    resume.add_argument("--run", type=Path)
    resume.add_argument("--latest")
    resume.add_argument("--no-tui", action="store_true")
    status = sub.add_parser("status", help="Show real phase, tools, files, and commits")
    status.add_argument("--run", type=Path)
    status.add_argument("--latest")
    tui = sub.add_parser("tui", help="Attach the focused live console")
    tui.add_argument("--run", type=Path)
    tui.add_argument("--latest")
    stop = sub.add_parser("stop", help="Request a graceful stop without deleting work")
    stop.add_argument("--run", type=Path)
    stop.add_argument("--latest")
    health = sub.add_parser("doctor", help="Validate local runtime and optionally all five providers")
    health.add_argument("--env", type=Path, default=DEFAULT_ENV)
    health.add_argument("--live", action="store_true")
    worker = sub.add_parser("_worker")
    worker.add_argument("--run", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(args.env, args.live)
    if args.command == "_worker":
        run_supervisor(args.run.resolve())
        return 0
    if args.command == "run":
        task = args.task_file.read_text(encoding="utf-8") if args.task_file else args.task
        run = prepare_new_run(
            args.project,
            task,
            projects_root=args.projects_root,
            env_file=args.env,
            run_id=args.run_id,
        )
        print(f"READY  {run}")
        if args.foreground:
            run_supervisor(run)
            print_status(run)
            return 0
        pid = launch_worker(run)
        print(f"START  five native sessions · supervisor {pid}")
        if not args.no_tui:
            _open_tui(run)
        else:
            print_status(run)
        return 0
    run = _resolve_run(args.run, args.latest)
    if args.command == "status":
        print_status(run)
        return 0
    if args.command == "tui":
        _open_tui(run)
        return 0
    if args.command == "stop":
        (run / "STOP").write_text(f"requested {time.time()}\n", encoding="utf-8")
        RunStore(run).emit("controller", "stop_requested", "Graceful stop requested; worktrees and commits are preserved")
        print(f"STOP REQUESTED  {run}")
        return 0
    if args.command == "resume":
        (run / "STOP").unlink(missing_ok=True)
        pid = launch_worker(run)
        print(f"RESUME  exact OpenCode sessions · supervisor {pid}")
        if not args.no_tui:
            _open_tui(run)
        else:
            print_status(run)
        return 0
    return 2
