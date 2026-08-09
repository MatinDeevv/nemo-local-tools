# Nemo 2

Nemo runs four real OpenCode software engineers in parallel and one engineering manager. Each person has a separate NVIDIA Build key, native file/shell/Git tools, a persistent OpenCode session, and an isolated Git worktree. A deterministic requirement splitter gives all four SWEs non-overlapping work immediately while the manager's warm session starts alongside them. The manager then integrates their branches, codes the missing glue, and keeps fixing until an observed test command passes.

This version uses OpenCode itself as the coding-agent runtime. It does not ask models to emit home-made JSON tool calls.

## What changed

- Five warm OpenCode servers start concurrently, so continuation turns avoid server and plugin cold boot.
- Four coding prompts are dispatched together. No SWE waits for the manager or another SWE before editing production files; the fifth-model manager heartbeat is hard-timeboxed to 30 seconds and cannot delay coding.
- Integration opens a clean Super-model session in the manager worktree, so stale OSS coordination context cannot slow or confuse merge/fix work.
- Retries target the exact recorded session ID. A resource/tool limit creates a Git checkpoint and resumes the same engineer; it never pretends the engineer is finished.
- OpenCode's official NVIDIA provider is used directly. Every server receives one key and cannot see the other four.
- OpenCode data, caches, worktrees, logs, and venvs live under `C:\Users\marti\Documents\NemoRuntime`, outside ChatGPT/Codex folders.
- Four branches are integrated only after explicit production handoff. Tests are the manager's final phase, after production code exists.
- The live console is focused: use `1`–`5` to switch agents. It displays public model text and readable tool activity, not raw JSON or private chain-of-thought.
- The progress bar is a delivery-gate meter (boot, four SWE handoffs, integration, verification, delivery), not an invented clock or ETA.

## Quick start

PowerShell:

```powershell
$task = @'
Build the project here. Put the complete detailed specification here.
'@

python C:\Users\marti\Documents\ChatGPT\nemo\nemo.py run `
  --project my-project `
  --task "$task"
```

The command prepares a project under `C:\Users\marti\Documents\NemoProjects`, launches the supervisor in the background, and opens the TUI. Press `q` to detach; the engineers continue running.

For very large briefs, use a file and avoid Windows command-line length limits:

```powershell
python C:\Users\marti\Documents\ChatGPT\nemo\nemo.py run `
  --project my-project `
  --task-file C:\path\to\spec.md
```

## Commands

```powershell
# Validate local software and all five environment-variable names
python .\nemo.py doctor

# Make one live request through every key/model in parallel
python .\nemo.py doctor --live

# Status without opening the full-screen UI
python .\nemo.py status --latest my-project

# Reattach the TUI
python .\nemo.py tui --latest my-project

# Resume the exact saved OpenCode sessions after a reboot or interruption
python .\nemo.py resume --latest my-project

# Gracefully stop; worktrees, commits, logs, and sessions are preserved
python .\nemo.py stop --latest my-project
```

The old `swe_team.py`, `engineering_team.py`, and `opencode_team.py` entrypoints now route to Nemo 2 as compatibility wrappers, but new commands should use `nemo.py`.

## TUI keys

- `1`–`5`: focus manager or one SWE
- `Tab` / `Shift+Tab`: next / previous engineer
- `C`: full team coordination chat
- `G`: Git branches and commits
- `T`: controller-observed verification output
- `E`: errors and retries
- `O`: show/hide completed tool output
- `/`: command reference
- `Q`: detach without stopping the team

## Runtime guarantees

Nemo never stores API-key values in run metadata, events, prompts, or UI state. It loads `NVIDIA_API_KEY_1` through `NVIDIA_API_KEY_5` from `C:\Users\marti\Desktop\.env` by default and injects only the assigned value into each isolated OpenCode process.

All new repositories receive a local-only Git identity and an allowed empty initial commit, avoiding the previous bootstrap failures. Existing repositories must be clean before a parallel run so user changes are never overwritten.
