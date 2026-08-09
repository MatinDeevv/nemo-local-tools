from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_ENV,
    INTEGRATION_MANAGER_MODEL,
    PROJECTS_ROOT,
    RUNTIME_ROOT,
    TEAM,
    Engineer,
    find_opencode,
    load_dotenv,
    missing_keys,
    safe_slug,
)
from .gitops import (
    GitError,
    bootstrap_repository,
    commit_if_dirty,
    commits_ahead,
    create_worktrees,
    ensure_venv,
    git,
    run_process,
)
from .opencode import OpenCodeServer, TurnResult
from .state import RunStore, read_json, read_jsonl


SCHEMA_VERSION = 2


def _mail_script() -> Path:
    return Path(__file__).resolve().parent.parent / "nemo_mail.py"


def _chat_commands(run_dir: Path, role: str) -> str:
    script = _mail_script()
    base = f'python "{script}"'
    return f"""TEAM CHAT (real messages shared with all five engineers):
- Read: {base} read --run "{run_dir}" --for {role}
- Post: {base} post --run "{run_dir}" --from {role} --to all --text "your concise message"
Post one file/interface claim before your first edit. Read chat at the start of every continuation. Post again only for a blocker, interface change, or handoff."""


def _split_requirements(task: str) -> dict[str, list[str]]:
    """Make a fast deterministic split; no manager round-trip blocks first edits."""
    bullets = [
        match.group(1).strip()
        for line in task.splitlines()
        if (match := re.match(r"^\s*[-*]\s+(.+)$", line))
    ]
    result = {person.id: [] for person in TEAM[1:]}
    if not bullets:
        return result
    platform_words = re.compile(r"\b(cli|command.line|configuration|config|readme|report|chart|package|entrypoint|user.interface|ui)\b", re.I)
    process_words = re.compile(r"\b(engineers?|tests? after|no web|do not|must claim|team chat)\b", re.I)
    platform = [bullet for bullet in bullets if platform_words.search(bullet)]
    domain = [bullet for bullet in bullets if bullet not in platform and not process_words.search(bullet)]
    result["swe3"] = platform
    targets = ["swe1", "swe2", "swe4"] if platform else ["swe1", "swe2", "swe3", "swe4"]
    base, remainder = divmod(len(domain), len(targets))
    sizes = [base] * len(targets)
    # Put odd extra requirements on later engineers, leaving SWE 1 focused on
    # the first/core interface and reducing overlap on from-scratch projects.
    for index in range(remainder):
        sizes[len(sizes) - 1 - index] += 1
    cursor = 0
    for target, size in zip(targets, sizes):
        result[target].extend(domain[cursor : cursor + size])
        cursor += size
    return result


def _swe_message(task: str, person: Engineer, run_dir: Path, *, continuation: bool = False) -> str:
    action = (
        "Continue from the current files and Git history. Your tool budget has reset. Read team chat, then immediately complete missing production implementation."
        if continuation
        else "The full project brief is saved at the path below. Read it once with the native read tool, inspect the repository briefly, claim your slice in team chat, then start production edits immediately."
    )
    worktree = run_dir / "worktrees" / person.id
    owned = _split_requirements(task).get(person.id, [])
    explicit = "\n".join(f"- {item}" for item in owned) or f"- {person.focus}"
    return f"""{action}

You are {person.title}, one of four SWEs running concurrently.
OWNERSHIP DEFAULT: {person.focus}
EXPLICIT OWNED REQUIREMENTS:
{explicit}
Implement these requirements. Do not take another SWE's requirements and do not wait for the manager.
HARD FILE BOUNDARY: edit only files directly required by the explicit requirements above. Do not create package manifests, README files, configuration, CLI files, or package __init__ files unless they are explicitly assigned to you. If one adjacent file is truly required, announce the exact path in team chat before touching it.
CURRENT WORKTREE: {worktree}
It is intentionally almost empty for a from-scratch project. Work here. Do not inspect the base repository, parent runtime folders, or other worktrees.

{_chat_commands(run_dir, person.id)}

THROUGHPUT CONTRACT:
- Production code now. No prose plan, TODO dump, placeholder modules, or test-first pass.
- After one `git status`/file-list check, write the actual implementation with native write/edit tools. Use PowerShell only when a shell command is necessary.
- Implement deep behavior, error handling, typing, docs where useful, and real interfaces—not stubs.
- Commit checkpoints before a long command or tool limit. Tests come after the production slice exists.
- If another engineer owns an interface you need, implement against a clear contract and post it to chat.
- End your visible response with NEMO_DONE only when your owned production slice is implemented and committed.

PROJECT TASK:
{task[:6000]}

AUTHORITATIVE FULL BRIEF: {run_dir / 'task.md'}"""


def _manager_coordination_message(task: str, run_dir: Path) -> str:
    # Four deterministic ownership prompts are already dispatched by the
    # controller. Keep the manager warm without burning a coding-length turn;
    # it becomes the active integrator as soon as the four handoffs arrive.
    return """Coordination heartbeat. The controller has dispatched four non-overlapping SWE ownership prompts and they are coding now. Use no tools, do not inspect files, and reply exactly NEMO_COORDINATION_READY. You will receive the merged production branches next and will then own integration and verification."""


def _manager_integration_message(task: str, run_dir: Path, failure: str = "") -> str:
    failure_block = f"\nLATEST VERIFICATION FAILURE:\n{failure[-6000:]}\n" if failure else ""
    return f"""The four SWE branches are now merged into your worktree. You are the integrating manager and now you must code.

{_chat_commands(run_dir, "manager")}

Inspect the merged implementation, resolve interface mismatches, fill missing requirements, install only declared project dependencies, then run the real test suite and CLI smoke path. Fix every failure yourself. This is the only test/fix phase: do not merely review or describe problems. Preserve substantial working code from the SWEs. Commit all integration fixes.
{failure_block}
Finish with NEMO_PROJECT_DONE only when the full requested product is implemented and the actual tests and smoke path pass.

PROJECT TASK:
{task[:6000]}

AUTHORITATIVE FULL BRIEF: {run_dir / 'task.md'}"""


def prepare_new_run(
    project: str,
    task: str,
    *,
    projects_root: Path = PROJECTS_ROOT,
    runtime_root: Path = RUNTIME_ROOT,
    env_file: Path = DEFAULT_ENV,
    run_id: str | None = None,
) -> Path:
    find_opencode()
    load_dotenv(env_file)
    absent = missing_keys()
    if absent:
        raise RuntimeError("Missing NVIDIA keys: " + ", ".join(absent))
    repo = (projects_root / safe_slug(project)).resolve()
    base_branch = bootstrap_repository(repo)
    venv_python = ensure_venv(repo)
    stamp = safe_slug(run_id or time.strftime("%Y%m%d-%H%M%S"))
    run_dir = (runtime_root / "runs" / f"{safe_slug(project)}-{stamp}").resolve()
    store = RunStore(run_dir)
    meta: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "id": run_dir.name,
        "project": safe_slug(project),
        "repo": str(repo),
        "base_branch": base_branch,
        "venv_python": str(venv_python),
        "env_file": str(env_file.resolve()),
        "task_file": str(run_dir / "task.md"),
        "status": "preparing",
        "phase": "boot",
        "started": time.time(),
        "updated": time.time(),
        "team": [person.public_dict() for person in TEAM],
    }
    store.initialize(meta, [person.id for person in TEAM])
    (run_dir / "task.md").write_text(task.strip() + "\n", encoding="utf-8")
    try:
        worktrees = create_worktrees(repo, run_dir, TEAM)
    except Exception:
        store.update_meta(status="failed", phase="boot", error="Could not create isolated Git worktrees")
        raise
    store.update_meta(status="ready", worktrees=worktrees)
    store.emit("controller", "prepared", "Five isolated worktrees and project virtualenv are ready")
    return run_dir


def _branch_file_count(repo: Path, base: str, branch: str) -> int:
    code, output = git(repo, "diff", "--name-only", f"{base}..{branch}")
    return len(output.splitlines()) if code == 0 and output else 0


def _checkpoint(
    store: RunStore,
    repo: Path,
    worktree: Path,
    person: Engineer,
    branch: str,
    base: str,
    attempt: int,
) -> tuple[int, int]:
    try:
        made = commit_if_dirty(worktree, f"wip({person.id}): automatic checkpoint {attempt}")
        if made:
            store.emit(person.id, "commit", f"Automatic checkpoint {attempt} committed")
    except GitError as error:
        # Unmerged paths are intentionally left for the manager conflict loop.
        store.emit(person.id, "git_error", str(error)[-1600:])
    commits = commits_ahead(repo, base, branch)
    files = _branch_file_count(repo, base, branch)
    store.update_agent(person.id, commits=commits, files=files)
    return commits, files


def _retry_delay(result: TurnResult, consecutive_failures: int) -> float:
    if result.limit_hit:
        return 0.25
    if result.transient:
        return float(min(60, max(1, 2 ** min(consecutive_failures - 1, 6))))
    return 1.0


def _handoff_claimed(store: RunStore, role: str) -> bool:
    signals = ("ready for integration", "handoff complete", "slice complete", "swe done", "done:")
    for row in reversed(read_jsonl(store.root / "chat.jsonl")):
        if row.get("from") != role:
            continue
        text = str(row.get("text", "")).lower()
        return row.get("kind") == "handoff" or any(signal in text for signal in signals)
    return False


def _run_swe(
    store: RunStore,
    server: OpenCodeServer,
    person: Engineer,
    task: str,
    repo: Path,
    base: str,
    branch: str,
    task_file: Path,
) -> None:
    state = store.agent_state(person.id)
    if state.get("status") in {"ready", "complete"} and commits_ahead(repo, base, branch) > 0:
        return
    existing_commits = commits_ahead(repo, base, branch)
    existing_files = _branch_file_count(repo, base, branch)
    if existing_commits > 0 and existing_files > 0 and _handoff_claimed(store, person.id):
        store.update_agent(person.id, status="ready", activity="Production handoff committed", commits=existing_commits, files=existing_files)
        store.emit(person.id, "ready", "Existing model-authored handoff accepted for integration")
        return
    consecutive_failures = 0
    while not store.stop_requested():
        continuation = bool(server.session_id)
        result = server.run_turn(
            _swe_message(task, person, store.root, continuation=continuation),
            task_file=task_file,
            title=f"{store.root.name} · {person.title}",
            completion_probe=lambda: _handoff_claimed(store, person.id) and commits_ahead(repo, base, branch) > 0,
        )
        attempt = int(store.agent_state(person.id).get("attempt", 1))
        commits, files = _checkpoint(store, repo, server.worktree, person, branch, base, attempt)
        if result.permanent:
            consecutive_failures += 1
            if consecutive_failures >= 2:
                raise RuntimeError(f"Permanent OpenCode/NVIDIA failure for {person.title}: {result.errors[-1]}")
        elif result.code == 0:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
        if ("NEMO_DONE" in result.text or _handoff_claimed(store, person.id)) and commits > 0 and files > 0:
            store.update_agent(person.id, status="ready", activity="Production slice committed")
            store.chat(person.id, "manager", f"Handoff complete: {commits} commit(s), {files} changed file(s).", "handoff")
            store.emit(person.id, "ready", "Production slice is ready for integration")
            return
        delay = _retry_delay(result, consecutive_failures)
        reason = "native tool limit reset" if result.limit_hit else "implementation not marked complete"
        store.update_agent(person.id, status="retrying", activity=f"Continuing: {reason}", retry_in=delay)
        store.emit(person.id, "retry", f"Continuing same session after {delay:g}s: {reason}")
        time.sleep(delay)
    raise RuntimeError(f"{person.title} stopped before completing its production slice")


def _coordinate_manager(
    store: RunStore,
    server: OpenCodeServer,
    person: Engineer,
    task: str,
    repo: Path,
    base: str,
    branch: str,
    task_file: Path,
) -> None:
    if store.agent_state(person.id).get("coordination_complete"):
        return
    # Dispatch the fifth model concurrently, but never put the four coding
    # branches behind a slow coordinator. Ownership is deterministic, so this
    # is only a warm-up/heartbeat; integration later uses the fast manager
    # model and the exact same persisted session/worktree.
    result = server.run_turn(
        _manager_coordination_message(task, store.root),
        task_file=task_file,
        title=f"{store.root.name} · Manager",
        timeout=30,
    )
    attempt = int(store.agent_state(person.id).get("attempt", 1))
    _checkpoint(store, repo, server.worktree, person, branch, base, attempt)
    acknowledged = "NEMO_COORDINATION_READY" in result.text
    store.update_agent(
        person.id,
        status="waiting",
        activity="Waiting to integrate four branches",
        coordination_complete=True,
    )
    store.emit(
        person.id,
        "coordination",
        "Manager heartbeat acknowledged" if acknowledged else "Manager heartbeat timebox ended; deterministic ownership remains active",
    )


def _merge_one(
    store: RunStore,
    manager_server: OpenCodeServer,
    source: Engineer,
    source_branch: str,
    task: str,
) -> None:
    manager_tree = manager_server.worktree
    code, output = git(manager_tree, "merge", "--no-ff", "--no-edit", source_branch, timeout=300)
    if code == 0:
        store.emit("manager", "merge", f"Integrated {source.title}", source=source.id)
        store.chat("manager", "all", f"Integrated {source.title}'s branch.", "integration")
        return
    store.emit("manager", "conflict", f"Resolving merge conflicts from {source.title}", output=output[-2000:])
    while not store.stop_requested():
        conflict_prompt = f"""A Git merge of {source.title} is in progress and has conflicts. Resolve every unmerged file by preserving the strongest compatible implementation from both sides. Run git status, edit the conflicts, git add the resolutions, and complete the merge commit. Do not abort the merge. Then continue integrating the project.

{_manager_integration_message(task, store.root, output)}"""
        result = manager_server.run_turn(conflict_prompt)
        unresolved = git(manager_tree, "diff", "--name-only", "--diff-filter=U")[1]
        merge_head = git(manager_tree, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0
        if not unresolved:
            if merge_head:
                git(manager_tree, "add", "-A", check=True)
                git(manager_tree, "commit", "--no-edit", check=True)
            store.emit("manager", "merge", f"Resolved and integrated {source.title}", source=source.id)
            return
        if result.permanent:
            raise RuntimeError(f"Manager could not resolve merge from {source.title}: {result.errors[-1]}")
        time.sleep(_retry_delay(result, 1))
    raise RuntimeError("Run stopped during merge conflict resolution")


def detect_test_command(worktree: Path, venv_python: Path) -> list[str] | None:
    python_project = any((worktree / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini"))
    if python_project or (worktree / "tests").is_dir():
        return [str(venv_python), "-m", "pytest", "-q"]
    package = worktree / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if "test" in scripts:
            return ["npm.cmd" if os.name == "nt" else "npm", "test"]
    if (worktree / "Cargo.toml").is_file():
        return ["cargo", "test"]
    if (worktree / "go.mod").is_file():
        return ["go", "test", "./..."]
    if any(worktree.rglob("*.py")):
        return [str(venv_python), "-m", "compileall", "-q", "."]
    return None


def _ensure_pytest(venv_python: Path, worktree: Path) -> str:
    probe = run_process([str(venv_python), "-c", "import pytest"], worktree, timeout=30)
    if probe.returncode == 0:
        return ""
    install = run_process([str(venv_python), "-m", "pip", "install", "pytest"], worktree, timeout=300)
    return install.stdout if install.returncode else ""


def run_verification(store: RunStore, worktree: Path, venv_python: Path) -> tuple[bool, str]:
    command = detect_test_command(worktree, venv_python)
    if not command:
        return False, "No executable test or compile command was detected. Add a real automated verification path."
    if "pytest" in command:
        install_error = _ensure_pytest(venv_python, worktree)
        if install_error:
            return False, install_error[-6000:]
    env = os.environ.copy()
    env["CI"] = "1"
    try:
        result = run_process(command, worktree, timeout=1200, env=env)
        output = result.stdout
        code = result.returncode
    except subprocess.TimeoutExpired as error:
        output = f"Verification timed out after {error.timeout} seconds"
        code = 124
    index = int(store.agent_state("manager").get("test_runs", 0)) + 1
    log = store.root / "logs" / f"verification-{index:03}.log"
    log.write_text(output, encoding="utf-8")
    store.update_agent("manager", test_runs=index, last_test_exit=code)
    store.emit(
        "manager",
        "test_pass" if code == 0 else "test_fail",
        f"{'PASS' if code == 0 else 'FAIL'} · {' '.join(command)}",
        output=output[-5000:],
        command=command,
        exit_code=code,
    )
    return code == 0, output


def _integrate_and_verify(
    store: RunStore,
    servers: dict[str, OpenCodeServer],
    task: str,
    repo: Path,
    base: str,
    worktrees: dict[str, dict[str, str]],
    venv_python: Path,
) -> None:
    manager = TEAM[0]
    manager_server = servers[manager.id]
    if manager_server.person.model != INTEGRATION_MANAGER_MODEL:
        store.emit("manager", "model_switch", "Switching manager from coordination model to fast integration model")
        coordination_session = manager_server.session_id
        manager_server.stop()
        # Integration is a different job with a different model. Reusing the
        # tiny OSS heartbeat transcript makes the Super model inherit stale
        # coordination instructions and an unnecessarily large context.
        store.update_agent(
            "manager",
            coordination_session_id=coordination_session,
            session_id=None,
            activity="Starting clean integration session",
        )
        integration_manager = replace(manager, model=INTEGRATION_MANAGER_MODEL)
        manager_server = OpenCodeServer(store, integration_manager, manager_server.worktree, venv_python)
        manager_server.start()
        servers[manager.id] = manager_server
    store.update_meta(phase="integration", status="running")
    store.update_agent("manager", status="integrating", activity="Merging four SWE branches")
    for source in TEAM[1:]:
        _merge_one(store, manager_server, source, worktrees[source.id]["branch"], task)
    failure = ""
    verification_attempt = 0
    while not store.stop_requested():
        verification_attempt += 1
        store.update_meta(phase="verification")
        store.update_agent("manager", status="integrating", activity="Coding integration and verification fixes")
        result = manager_server.run_turn(_manager_integration_message(task, store.root, failure))
        manager_branch = worktrees["manager"]["branch"]
        _checkpoint(
            store,
            repo,
            manager_server.worktree,
            manager,
            manager_branch,
            base,
            int(store.agent_state("manager").get("attempt", 1)),
        )
        store.update_agent("manager", status="testing", activity="Running actual project verification")
        passed, failure = run_verification(store, manager_server.worktree, venv_python)
        if passed:
            store.update_agent("manager", status="complete", activity="Integration and verification passed")
            store.emit("manager", "verified", f"Project verification passed on attempt {verification_attempt}")
            return
        if result.permanent:
            raise RuntimeError(f"Manager model failed permanently while tests are red: {result.errors[-1]}")
        store.update_agent("manager", status="retrying", activity="Tests failed; fixing next")
    raise RuntimeError("Run stopped before verification passed")


def _merge_to_base(store: RunStore, repo: Path, manager_branch: str) -> None:
    store.update_meta(phase="delivery")
    code, output = git(repo, "merge", "--no-ff", "--no-edit", manager_branch, timeout=300)
    if code:
        raise GitError(f"Could not merge the verified manager branch into the project branch:\n{output}")
    store.update_meta(status="complete", phase="complete", completed=time.time())
    store.emit("controller", "complete", "Verified manager branch merged into the project")


def run_supervisor(run_dir: Path) -> None:
    store = RunStore(run_dir)
    meta = read_json(store.meta_path, {}) or {}
    if int(meta.get("schema", 0)) != SCHEMA_VERSION:
        raise RuntimeError("This run was created by an incompatible Nemo version.")
    load_dotenv(Path(meta["env_file"]))
    absent = missing_keys()
    if absent:
        raise RuntimeError("Missing NVIDIA keys: " + ", ".join(absent))
    repo = Path(meta["repo"])
    base = str(meta["base_branch"])
    task_file = Path(meta["task_file"])
    task = task_file.read_text(encoding="utf-8")
    venv_python = Path(meta["venv_python"])
    worktrees: dict[str, dict[str, str]] = meta["worktrees"]
    store.update_meta(
        status="running",
        phase="boot",
        supervisor_pid=os.getpid(),
        error=None,
        failed=None,
        completed=None,
        team=[person.public_dict() for person in TEAM],
    )
    servers = {
        person.id: OpenCodeServer(store, person, Path(worktrees[person.id]["worktree"]), venv_python)
        for person in TEAM
    }
    try:
        store.emit("controller", "parallel_boot", "Starting five warm OpenCode runtimes concurrently")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="nemo-boot") as pool:
            boot = [pool.submit(server.start) for server in servers.values()]
            for future in boot:
                future.result()
        store.update_meta(phase="coding")
        store.emit("controller", "parallel_start", "Manager and four SWEs started together")
        manager = TEAM[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="nemo-agent") as pool:
            futures: list[concurrent.futures.Future[None]] = [
                pool.submit(
                    _coordinate_manager,
                    store,
                    servers[manager.id],
                    manager,
                    task,
                    repo,
                    base,
                    worktrees[manager.id]["branch"],
                    task_file,
                )
            ]
            for person in TEAM[1:]:
                futures.append(
                    pool.submit(
                        _run_swe,
                        store,
                        servers[person.id],
                        person,
                        task,
                        repo,
                        base,
                        worktrees[person.id]["branch"],
                        task_file,
                    )
                )
            for future in futures:
                future.result()
        _integrate_and_verify(store, servers, task, repo, base, worktrees, venv_python)
        _merge_to_base(store, repo, worktrees["manager"]["branch"])
    except Exception as error:
        store.update_meta(status="stopped" if store.stop_requested() else "failed", error=str(error), failed=time.time())
        store.emit("controller", "fatal", str(error))
        raise
    finally:
        for server in servers.values():
            server.stop()
        store.update_meta(supervisor_pid=None)
