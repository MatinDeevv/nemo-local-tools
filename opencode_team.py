"""Compatibility entrypoint for Nemo 2.

New usage is ``python nemo.py run --project NAME --task TEXT``.  The legacy
shape remains accepted so existing PowerShell snippets do not silently break.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from nemo.cli import main as nemo_main


def main() -> int:
    parser = argparse.ArgumentParser(description="Compatibility wrapper for the Nemo 2 OpenCode team.")
    parser.add_argument("task")
    parser.add_argument("--project", required=True)
    parser.add_argument("--projects-root", type=Path)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--resume-run", type=Path)
    parser.add_argument("--no-tui", action="store_true")
    args = parser.parse_args()
    if args.resume_run:
        command = ["resume", "--run", str(args.resume_run)]
    else:
        command = ["run", "--project", args.project, "--task", args.task]
        if args.projects_root:
            command += ["--projects-root", str(args.projects_root)]
        if args.env:
            command += ["--env", str(args.env)]
        if args.run_id:
            command += ["--run-id", args.run_id]
    if args.no_tui:
        command.append("--no-tui")
    return nemo_main(command)


if __name__ == "__main__":
    raise SystemExit(main())
