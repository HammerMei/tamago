# Memory Sync — How It Works

Tamago agents have **persistent, cross-machine memory** backed by a git repo (your profile repo).
This document covers the sync mechanics, directory structure, and how to configure or disable it.

---

## Overview

Memory lives in your **profile repo** under `agents/memory/<agent-name>/`.
Tamago automatically syncs it on every Claude Code session using git hooks.

Two memory layers:

| Layer | Path | Scope |
|-------|------|-------|
| **Shared** | `agents/memory/<name>/` | All machines, all instances |
| **Env-specific** | `agents/memory/<name>/env-<hostname>/` | Local to this machine |

> The `.claude/agent-memory/<name>/` path inside your project is a **symlink** created by
> `tamago install`. It points into the real profile repo so Claude Code can read/write through it
> without permission prompts, while git tracks the actual path.

---

## Directory Structure

```
<profile-repo>/
└── agents/memory/
    └── <agent-name>/
        ├── MEMORY.md                   ← index; auto-loaded into session context
        ├── user_profile.md             ← user preferences and background
        ├── feedback_*.md               ← behavioral guidance / corrections
        ├── project_*.md                ← ongoing project context
        ├── reference_*.md              ← external resource pointers
        └── env-<hostname>/
            ├── MEMORY.md               ← machine-specific notes
            └── visited.md              ← session log (append-only)
```

---

## Auto-Sync Hooks

Hooks are configured in `settings/claude/settings.json` and executed by Claude Code.
The logic lives in `scripts/memory-sync.sh`.

```
Session starts
  └── SessionStart → memory-sync.sh --init claude-code
          For each agent in AGENT_NAMES (falls back to AGENT_NAME):
            Creates env-<hostname>/ on first visit
            Appends to visited.md when harness/model changes
          Commits + pushes once so the next pull sees a clean tree

User sends a message
  └── UserPromptSubmit → memory-sync.sh --pull
          git pull --rebase — fetch latest memory from remote

Claude finishes a reply
  └── Stop → memory-sync.sh --push
          If agents/memory/ changed: git add → commit → pull --rebase → push
          On push failure: surfaces a warning into the Claude context
```

---

## Configuration

### How tamago knows which profile to sync

`tamago install` writes a shell-sourceable file at `.tamago/machine.env` (gitignored):

```bash
PROFILE_REPO=/Users/you/.tamago/my-profile
AGENT_NAME=my-agent           # first (or only) agent — used as default-agent pointer
AGENT_NAMES='my-agent'        # space-separated list of all non-disabled agents
MEMORY_SYNC=1
TTS_ENABLED=1
```

With multiple `[[agents]]` declared in `tamago.conf`, `AGENT_NAMES` holds all of them:

```bash
AGENT_NAME=hammer.mei
AGENT_NAMES='hammer.mei wave.bro'
```

`memory-sync.sh` sources this file at hook time. Discovery order:

1. `.tamago/machine.env` — project-specific install (always wins)
2. `~/.tamago/machine.env` — global install fallback (for globally-scoped agents)
3. Neither found → exit silently (not a tamago project; no-op)

### Disabling sync

Set `MEMORY_SYNC=0` in `.tamago/machine.env`, or export it in your shell:

```bash
export MEMORY_SYNC=0   # disable for this shell session
```

The legacy env var `LAOMEI_MEMORY_SYNC=0` also works for backward compatibility.

---

## Multi-Agent Support ("全家桶" — Family Bucket)

> 🪣 Like the KFC 炸雞 bucket that feeds the whole family, this mode packs all your agents
> into one repo.
>
> | EN | ZH | JA |
> |----|----|----|
> | *Family Bucket* — everything in one bucket, hot and ready | 全家桶 — 肯德基炸雞全家桶，一桶搞定全家 | ファミリーバーレル — KFC の桶みたいに、エージェントをまとめて一箱 |
>
> *Chickens lay eggs. Tamago (たまご) approves. 🥚🐔*

You can install multiple agents in a single repo by adding multiple `[[agents]]` blocks
to your project `tamago.conf`:

```toml
[[agents]]
name   = "hammer.mei"
source = "profile"
tts    = true
memory = true

[[agents]]
name   = "wave.bro"
source = "profile"
```

`tamago install` writes all non-disabled agent names to `AGENT_NAMES` in `machine.env`.
`memory-sync.sh --init` then creates an `env-<hostname>/` dir (with `MEMORY.md` and
`visited.md`) for **each** agent on first session, in a single git commit.

`--pull` and `--push` operate on the whole `agents/memory/` tree — no per-agent loop needed.

> **Backward compat**: scripts that only read `AGENT_NAME` continue to work — it always
> holds the first non-disabled agent name (the default-agent pointer).

---

## OpenCode Support

OpenCode has no `SessionStart` hook, so tamago uses a TypeScript plugin instead.
`settings/opencode/plugins/memory-bootstrap.ts` fires session init on the **first message**
of each session (using `chat.params` + an in-memory `Set<string>` to deduplicate by session ID).

The plugin also injects `MEMORY.md` into the system prompt automatically.

---

## Session Footprint: `visited.md`

Tracks when the harness or model changes — not every session, just transition points.

Format:
```
- 2026-04-15 | claude-code | claude-sonnet-4-5
```

Dedup rule: if the last line already matches the current harness + model, nothing is written.
This keeps the log readable without polluting it with identical entries on every session.

---

## Setup on a New Machine

```bash
# 1. Clone tamago
git clone https://github.com/HammerMei/tamago ~/.tamago

# 2. Install global hooks
python3 ~/.tamago/setup.py install-global

# 3. Install into a project (this writes .tamago/machine.env)
cd ~/path/to/project
python3 ~/.tamago/setup.py install --profile-dir ~/.tamago/my-profile

# 4. Verify
bash ~/.tamago/scripts/health-check.sh
```

---

## Known Gotchas

| Gotcha | Detail |
|--------|--------|
| `~` doesn't expand in `${VAR:-~/path}` | Scripts use `$HOME` not `~` |
| `CLAUDECODE` unset in SessionStart hooks | Harness name is passed explicitly via CLI arg |
| `git pull --rebase` fails on dirty tree | `--init` commits immediately after writing `visited.md` |
| OpenCode fire-and-forget race | `runSessionInit` must be `await`ed so Stop hook doesn't push before init completes |
| Memory files in `~/.claude/` won't sync | Memory must live inside `$PROFILE_REPO/agents/memory/` |
| `AGENT_NAMES` absent in old machine.env | `--init` falls back to `AGENT_NAME`; re-run `tamago install` to upgrade |
