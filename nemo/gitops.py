from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import Engineer, RUNTIME_ROOT, safe_slug


class GitError(RuntimeError):
    pass


def run_process(
    command: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def git(cwd: Path, *args: str, timeout: int = 120, check: bool = False) -> tuple[int, str]:
    result = run_process(["git", *args], cwd, timeout=timeout)
    if check and result.returncode:
        raise GitError(f"git {' '.join(args)} failed in {cwd}:\n{result.stdout}")
    return result.returncode, result.stdout.strip()


def bootstrap_repository(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", check=True)
    # A project-local identity makes brand-new local projects deterministic and
    # does not alter the user's global Git configuration.
    git(repo, "config", "user.name", "Nemo Engineering Team", check=True)
    git(repo, "config", "user.email", "nemo@local.invalid", check=True)
    if git(repo, "rev-parse", "--verify", "HEAD")[0] != 0:
        ignore = repo / ".gitignore"
        if not ignore.exists():
            ignore.write_text(
                "__pycache__/\n*.py[cod]\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n.coverage\nhtmlcov/\n*.egg-info/\n.venv/\nvenv/\nnode_modules/\ndist/\nbuild/\n",
                encoding="utf-8",
            )
        git(repo, "add", "-A", check=True)
        git(repo, "commit", "--allow-empty", "-m", "chore: initialize project", check=True)
    code, dirty = git(repo, "status", "--porcelain")
    if code:
        raise GitError(dirty)
    if dirty:
        raise GitError(
            "The target project has uncommitted user changes. Commit or stash them before starting a parallel run."
        )
    return git(repo, "branch", "--show-current", check=True)[1] or "master"


def ensure_venv(repo: Path) -> Path:
    """Create a fast reusable venv; projects install their own declared deps later."""
    root = RUNTIME_ROOT / "venvs" / safe_slug(repo.name)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        result = run_process([sys.executable, "-m", "venv", str(root)], repo, timeout=180)
        if result.returncode:
            raise RuntimeError(f"Could not create project virtualenv:\n{result.stdout}")
    return python


def create_worktrees(
    repo: Path,
    run_dir: Path,
    team: tuple[Engineer, ...],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for person in team:
        branch = f"nemo/{run_dir.name}-{person.id}"
        worktree = run_dir / "worktrees" / person.id
        git(repo, "worktree", "add", "-b", branch, str(worktree), "HEAD", timeout=180, check=True)
        result[person.id] = {"branch": branch, "worktree": str(worktree)}
    return result


def commit_if_dirty(worktree: Path, message: str) -> bool:
    if not git(worktree, "status", "--porcelain")[1]:
        return False
    git(worktree, "add", "-A", check=True)
    result, output = git(worktree, "commit", "-m", message)
    if result:
        raise GitError(output)
    return True


def commits_ahead(repo: Path, base: str, branch: str) -> int:
    code, output = git(repo, "rev-list", "--count", f"{base}..{branch}")
    if code:
        return 0
    try:
        return int(output)
    except ValueError:
        return 0


def changed_files(worktree: Path, base: str = "HEAD") -> int:
    _, output = git(worktree, "diff", "--name-only", base)
    _, staged = git(worktree, "diff", "--cached", "--name-only", base)
    return len({line for line in (output + "\n" + staged).splitlines() if line.strip()})
