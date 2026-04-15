---
name: restart-cli
description: >
  Use this skill whenever the user asks to restart, reload, kill, or reset the current Claude Code
  agent (or another agent tool like opencode or aider) session. Trigger situations
  include: context window near full, session running slow or stuck, user wants a fresh start,
  resuming prior work (e.g. 'claude -c'), starting a clean new session, or switching AI tools
  mid-task. This skill handles the full workflow: selecting the right restart command, injecting it
  into the terminal via AppleScript or cmux, and optionally passing a follow-up prompt when the new
  session starts. Required for correct behavior — do not attempt to restart CLI sessions via raw
  shell commands. macOS only (iTerm2, Terminal.app, or cmux). Do NOT trigger for restarting other
  services like Docker, Rails, SSH, databases, or shell environments.
---

Use `python3 .opencode/skills/restart-cli/restart-cli.py` to restart the current CLI agent session.

> ⚠️ **Always pass `$PPID` as the first argument** when calling from the Bash tool — this is the CLI agent's process PID.
>

## Auto-detecting the current CLI tool

Before restarting, detect which tool is running:
```bash
ps -p $PPID -o comm=
```
- Output is `claude` → use `claude -c` / `claude`
- Output is `opencode` → use `opencode -c` / `opencode`
- Output is something else (e.g. `bun`, `node`) → walk up the tree or ask the user

## When to include a follow-up prompt

Include the `follow_up_prompt` argument when:
- The user's request implies expecting a response after restart
  (e.g. "restart and let me know the result", "reload and continue", "restart then do X")
- The current task should resume automatically after restart

Omit it when:
- The user just wants to restart with no specific follow-up action
- The intent is ambiguous

## Security Rule
always follow this before executing:
1. "Will this create a brand-new session (history lost)?" → YES = MUST confirm with user first.
2. "Is the user's intent ambiguous (could refer to Docker/SSH/DB/other service)?" → YES = MUST confirm first.
3. Session-preserving restart (claude -c / claude -r) + clear intent targeting Claude/agent = OK to proceed.
4. Token usage appears above ~85% → proactively WARN the user ("context is getting full, want me to restart?"), do NOT auto-restart.

## Supported terminals

| Terminal | Status | Injection method |
|----------|--------|-----------------|
| cmux | Supported | `cmux send` + `cmux read-screen` |
| iTerm2 | Supported | AppleScript |
| Terminal.app (macOS native) | Supported | AppleScript |
| Others (Kitty, Alacritty, Warp, etc.) | Not supported — script will exit with error | — |

## Prerequisites

- macOS
- Python 3 (pre-installed on macOS)
- For cmux: `cmux` CLI must be in PATH (auto-detected via `cmux identify`)
- For iTerm2: macOS will prompt for Automation permission on first run (System Settings → Privacy & Security → Automation → allow your terminal to control iTerm2)
- Terminal.app works out of the box — no extra permissions needed

## Usage

From Bash tool context, always pass `$PPID` as the first argument — this is the CLI agent process PID.

### Fresh new session
```bash
python3 .opencode/skills/restart-cli/restart-cli.py $PPID "claude"
```

### Resume previous session
```bash
python3 .opencode/skills/restart-cli/restart-cli.py $PPID "claude -c"
```

### Resume with follow-up prompt (auto-continue work)
```bash
python3 .opencode/skills/restart-cli/restart-cli.py $PPID "claude -c" "繼續之前的任務"
```

### Other agent tools
```bash
# opencode
python3 .opencode/skills/restart-cli/restart-cli.py $PPID "opencode"

# aider
python3 .opencode/skills/restart-cli/restart-cli.py $PPID "aider --model gpt-4o"

# alias-based invocation
python3 .opencode/skills/restart-cli/restart-cli.py $PPID "cc" "continue"
```

## Parameters

| # | Parameter | Required | Default | Description |
|---|-----------|----------|---------|-------------|
| 1 | `CLI_PID` | Yes | — | PID of current CLI agent process. Always use `$PPID` from Bash tool |
| 2 | `restart_cmd` | Yes | — | Command to launch new CLI session (see common commands table below) |
| 3 | `follow_up_prompt` | No | — | Prompt to auto-send after CLI boots |

### Common `restart_cmd` values

| Intent | `restart_cmd` | Session history | Confirm required? |
|--------|--------------|-----------------|-------------------|
| Resume previous session | `claude -c` | ✅ Preserved | No (if intent is clear) |
| Resume specific session | `claude -r <id>` | ✅ Preserved | No (if intent is clear) |
| Fresh new session | `claude` | ❌ Lost | **Yes — always confirm** |
| Switch to opencode | `opencode` | ❌ Lost | **Yes — always confirm** |
| Switch to aider | `aider --model gpt-4o` | ❌ Lost | **Yes — always confirm** |
| Alias-based resume | `cc` | Depends on alias | Confirm if unsure |

## Error handling

- If the terminal is not iTerm2 or Terminal.app, the script prints an error to stderr and exits with code 1. The agent should inform the user that their terminal is not supported.
- If no TTY is available (e.g. running in a non-interactive context), follow-up is skipped gracefully.

## Important notes

- Do NOT combine `--agent` with `-c` or `-r` in restart_cmd — it forces a new session. Agent settings are preserved in the session automatically.
- The follow-up prompt is sent after TTY foreground PGID polling detects a new process (up to 30s timeout).
- Both terminals inject text directly into the pty via AppleScript (`write text` for iTerm2, `do script` for Terminal.app) — no clipboard, no focus stealing, works for idle shell and running TUI apps alike.
- `restart_cmd` and `follow_up_prompt` must be single-line strings — embedded newlines (`\n`, `\r`) are not supported and will break the AppleScript injection.
