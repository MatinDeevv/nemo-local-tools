from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from nemo.cli import build_parser
from nemo.config import TEAM, load_dotenv, safe_slug
from nemo.gitops import bootstrap_repository, commit_if_dirty, commits_ahead, create_worktrees, git
from nemo.opencode import _error_text, _token_count, _tool_summary, opencode_config
from nemo.state import RunStore, read_json, read_jsonl
from nemo.supervisor import _split_requirements, _swe_message, detect_test_command
from nemo_tui import NemoConsole


def test_safe_slug_and_dotenv_do_not_leak_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert safe_slug("  Super PhD / Econ!!!  ") == "super-phd-econ"
    env = tmp_path / ".env"
    env.write_text("# comment\nNVIDIA_API_KEY_1='secret'\nexport VALID_NAME=ok\nBAD KEY=no\n", encoding="utf-8")
    monkeypatch.delenv("NVIDIA_API_KEY_1", raising=False)
    loaded = load_dotenv(env)
    assert loaded == {"NVIDIA_API_KEY_1": "NVIDIA_API_KEY_1", "VALID_NAME": "VALID_NAME"}
    assert "secret" not in repr(loaded)


def test_run_store_concurrent_chat_and_atomic_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "run")
    store.initialize({"team": [{"id": "manager"}], "started": time.time()}, ["manager"])

    def writer(index: int) -> None:
        store.chat("manager", "all", f"message-{index}")
        store.update_agent("manager", activity=f"write-{index}")

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = read_jsonl(store.root / "chat.jsonl")
    assert len(rows) == 30
    assert {row["text"] for row in rows} == {f"message-{index}" for index in range(30)}
    assert read_json(store.root / "agents" / "manager" / "state.json")["activity"].startswith("write-")


def test_opencode_event_summaries_are_readable() -> None:
    part = {
        "tool": "bash",
        "state": {"status": "completed", "input": {"command": "python -m pytest -q"}, "output": "12 passed"},
    }
    assert _tool_summary(part) == ("bash", "python -m pytest -q", "12 passed", False)
    assert _error_text({"name": "APIError", "data": {"message": "rate limited"}}) == "rate limited"
    assert _token_count({"tokens": {"input": 10, "output": 20, "cache": {"read": 5}}}) == 35
    config = json.loads(opencode_config(TEAM[1]))
    assert config["permission"]["*"] == "allow"
    assert config["agent"]["nemo"]["steps"] == 200
    assert "production code" in config["agent"]["nemo"]["prompt"].lower()
    assert "never generate source through set-content" in config["agent"]["nemo"]["prompt"].lower()


def test_git_bootstrap_worktrees_and_checkpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = bootstrap_repository(repo)
    ignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "*.egg-info/" in ignore
    assert ".pytest_cache/" in ignore
    run = tmp_path / "runtime" / "run"
    (run / "worktrees").mkdir(parents=True)
    trees = create_worktrees(repo, run, TEAM[:2])
    worker = Path(trees["swe1"]["worktree"])
    (worker / "real_code.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    assert commit_if_dirty(worker, "feat: real code")
    assert commits_ahead(repo, base, trees["swe1"]["branch"]) == 1
    assert git(worker, "status", "--porcelain")[1] == ""


def test_detect_test_commands(tmp_path: Path) -> None:
    python = tmp_path / "venv" / "Scripts" / "python.exe"
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    assert detect_test_command(tmp_path, python) == [str(python), "-m", "pytest", "-q"]
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
    assert detect_test_command(tmp_path, python)[-1] == "test"


def test_requirement_split_is_non_overlapping_and_keeps_platform_work_on_swe3() -> None:
    task = """Build MarketMath.
    - Implement return and covariance mathematics.
    - Implement a two-asset minimum-variance portfolio.
    - Implement deterministic GBM simulation.
    - Implement VaR and expected shortfall metrics.
    - Add a Python CLI and package entrypoint.
    - Add a complete README and configuration example.
    - Engineers must claim files and test after coding.
    """
    split = _split_requirements(task)
    assigned = [item for items in split.values() for item in items]
    assert len(assigned) == len(set(assigned)) == 6
    assert any("return and covariance" in item for item in split["swe1"])
    assert any("minimum-variance" in item for item in split["swe2"])
    assert any("CLI" in item for item in split["swe3"])
    assert any("README" in item for item in split["swe3"])
    assert any("GBM" in item for item in split["swe4"])
    assert any("expected shortfall" in item for item in split["swe4"])
    prompt = _swe_message(task, TEAM[1], Path("C:/runtime/run"))
    assert "HARD FILE BOUNDARY" in prompt
    assert "Do not create package manifests" in prompt


def test_cli_requires_an_explicit_task_source() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--project", "demo", "--task", "build it"])
    assert args.project == "demo"
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--project", "demo"])


def test_tui_mounts_and_uses_real_delivery_gates(tmp_path: Path) -> None:
    run = tmp_path / "run"
    store = RunStore(run)
    meta = {
        "schema": 2,
        "project": "demo",
        "phase": "coding",
        "status": "running",
        "started": time.time(),
        "team": [person.public_dict() for person in TEAM],
        "worktrees": {},
    }
    store.initialize(meta, [person.id for person in TEAM])
    for person in TEAM[1:3]:
        store.update_agent(person.id, status="ready")

    async def exercise() -> None:
        app = NemoConsole(run)
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            gates = str(app.query_one("#gates").render())
            assert "PRODUCTION HANDOFFS 2/4" in gates
            await pilot.press("3")
            assert app.selected == "swe2"
            await pilot.press("/")
            assert app.mode == "palette"

    asyncio.run(exercise())
