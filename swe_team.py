"""Legacy command name for the Nemo 2 OpenCode engineering team.

This preserves the PowerShell command users already have while routing every
run through the rebuilt supervisor. New scripts should invoke ``nemo.py``.
"""

from opencode_team import main


if __name__ == "__main__":
    raise SystemExit(main())
