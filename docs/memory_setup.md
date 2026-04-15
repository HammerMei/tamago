# Memory System Setup

How Hammer Mei's persistent memory works across machines and harnesses.

---

## Overview

Memory is stored in the `assistant` git repo and synced automatically via hooks.
Two layers:

| Layer | Path | Scope |
|-------|------|-------|
| **Shared** | `agents/memory/hammer.mei/` | All instances, all machines |
| **Env-specific** | `agents/memory/hammer.mei/env-{hostname}/` | Per-machine private |

> ⚠️ **All memory files must live inside the `assistant` repo** (`$ASSISTANT_SETUP_REPO`).
> Never write to `~/.claude/` — that path is outside git and won't sync.

The `.claude/agent-memory/hammer.mei/` path is a **symlink** (created by `setup.py`) pointing
to the real directory above. Claude Code reads through it; git tracks the real path.

---

## Directory Structure

```
agents/memory/
└── hammer.mei/
    ├── MEMORY.md                        # Index file — auto-loaded into session context
    ├── user_profile.md                  # User preferences and background
    ├── feedback_*.md                    # Behavioral feedback/corrections
    ├── project_*.md                     # Project context
    ├── reference_*.md                   # External resource pointers
    └── env-{hostname}/
        ├── MEMORY.md                    # Machine-specific notes
        └── visited.md                   # Session footprint log (append-only)
```

---

## Auto-Sync via Git Hooks

Hooks are configured in `settings/claude/settings.json` and executed by Claude Code.
The actual logic lives in `scripts/memory-sync.sh`.

### Hook Flow

```
claude -c / claude (new session)
    │
    └── SessionStart → memory-sync.sh --init claude-code
            • Creates env-{hostname}/ if first visit
            • Appends to visited.md if harness/model changed (last-record dedup)
            • Immediately commits + pushes so next pull sees a clean working tree

User sends message
    │
    └── UserPromptSubmit → memory-sync.sh --pull
            • git pull --rebase to get latest memory from remote

Claude finishes reply
    │
    └── Stop → memory-sync.sh --push
            • If agents/memory/ has changes: git add + commit + pull --rebase + push
            • On push failure: surfaces {"systemMessage": "⚠️ Memory sync failed..."} to user
```

### Environment Variable

| Variable | Default | Purpose |
|----------|---------|---------|
| `ASSISTANT_SETUP_REPO` | `$HOME/workspace/assistant` | Path to assistant repo |
| `LAOMEI_MEMORY_SYNC` | `1` | Set to `0` to disable all sync (offline/emergency) |

> **Note:** Use `$HOME`, not `~`, in shell scripts and hook commands.
> `~` does not expand inside `${VAR:-~/path}` parameter substitution.

---

## Session Footprint: `visited.md`

Tracks when harness or model changes — not every session, just change points.

**Format:**
```
- {YYYY-MM-DD} | {harness} | {model}
```

**Dedup rule:** If the last line matches current harness + model → skip. Only appends on change.

**Harness detection:** Passed explicitly by the caller (not detected from env vars,
because `CLAUDECODE` is not set when `SessionStart` hooks run).

| Caller | Argument |
|--------|----------|
| Claude Code `SessionStart` hook | `--init claude-code` |
| OpenCode plugin (`chat.params`) | `--init opencode` |

**Model detection:** Reads `defaultModel` from `settings/claude/settings.json` (best effort, `unknown` if not set).

---

## OpenCode Support

OpenCode has no `SessionStart` hook (upstream issue [#5409](https://github.com/sst/opencode/issues/5409)).
Instead, `settings/opencode/plugins/memory-bootstrap.ts` fires session init on the **first message**
of each session using `chat.params` + an in-memory `Set<string>` to deduplicate by session ID.

The plugin also injects `MEMORY.md` into the system prompt via `experimental.chat.system.transform`.

---

## Setup on a New Machine

```bash
# 1. Clone the repo
git clone glin@<remote>:~/git.repos/assistant.git ~/workspace/assistant

# 2. Install symlinks for a project
cd ~/path/to/project
python3 ~/workspace/assistant/setup.py install

# 3. (Optional) If using a non-default repo path, add to ~/.zshrc:
export ASSISTANT_SETUP_REPO="/custom/path/to/assistant"
source ~/.zshrc
```

`setup.py install` creates symlinks in `.claude/` and `.opencode/` for agents, skills, settings,
and agent-memory. It also injects `ASSISTANT_SETUP_REPO` into `~/.zshrc` **only** if a non-default
path is used (default `$HOME/workspace/assistant` is already covered by the fallback).

---

## Known Gotchas

| Gotcha | Detail |
|--------|--------|
| `~` doesn't expand in `${VAR:-~/path}` | Use `$HOME` in hook commands and scripts |
| `CLAUDECODE` unset in `SessionStart` hooks | Pass harness name explicitly via CLI arg |
| `git pull --rebase` fails if working tree is dirty | `--init` now commits immediately after writing `visited.md` |
| OpenCode fire-and-forget race | `runSessionInit` must be `await`ed so Stop hook doesn't push before init completes |
| Memory files in `~/.claude/` don't sync | Must be inside `$ASSISTANT_SETUP_REPO/agents/memory/` |
