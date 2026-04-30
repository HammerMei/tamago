# Tamago — Agent Base Instructions

This section is automatically merged into agent system prompts by tamago's `setup.py`.
It covers tamago mechanics common to all agents: memory system, sync behavior, and tooling.


# Deployment Health Check

After setup on a new machine, or when something feels off, run:

```bash
bash ~/.tamago/scripts/health-check.sh
```

All green = good to go. Add `--fix` to auto-fix simple issues.
Specify `--project <dir>` to check a different project directory.


# Persistent Memory

## Memory Structure

Memory is stored in two layers accessible via {{AGENT_MEMORY_LOCATION_DESC}}.

{{AGENT_MEMORY_PATH_WARNING}}
>
> **Path equivalence**: `{{AGENT_MEMORY_ROOT}}{{AGENT_MEMORY_DIR}}/` and
> `{{AGENT_MEMORY_ROOT}}{{AGENT_NAME}}/` are **the same location** — tamago creates both
> as symlinks to the same source. Claude Code may inject either form in the
> "Persistent Agent Memory" section depending on version; both work.
> (Tracking: https://github.com/anthropics/claude-code/issues/54208)

### 🌐 Shared Memory (synced across all instances)
Path: `{{AGENT_MEMORY_ROOT}}{{AGENT_MEMORY_DIR}}/`
- `MEMORY.md` — memory index; auto-loaded at session start when available. **If not present in context, load it explicitly with the Read tool before proceeding.**
- Topic files (e.g. `user.md`, `feedback.md`) — load on demand when relevant
- All content synced via git to every instance of this agent

### 🖥️ Environment-Specific Memory (local to this machine)
Path: `{{AGENT_MEMORY_ROOT}}{{AGENT_MEMORY_DIR}}/env-{hostname}/`
- Per-machine private memory (hardware, installed tools, local config)
- Also git-synced, but only read by the matching hostname instance
- Use `hostname` command to get the current machine name

**Memory-sensitive topics**: Before answering, load the relevant topic file when needed
for persona details, user preferences, prior corrections, recurring routines, or other
remembered facts.

**When to save**: Durable facts worth remembering — user preferences, feedback/corrections,
recurring patterns. Write a topic file and update `MEMORY.md` index.

**Update, don't just append**: Correct or remove outdated entries rather than accumulating
stale info.

**Do NOT store**: secrets, credentials, PII, health data, or anything session-specific.

## Memory Auto-Sync

Memory syncs automatically via git hooks (configured in `settings/claude/settings.json`):
- **Before each prompt (UserPromptSubmit)**: `git pull --rebase` to fetch latest memory
- **After each reply (Stop)**: if `agents/memory/` changed, commit → pull → push
- Push failures surface as a `systemMessage` warning
- Pull failures are injected into Claude context as a note — **if you see a sync warning in context, proactively tell the user at the start of your reply, even if they only said "Hi"**
- Note: `UserPromptSubmit` hook stdout → context only; stderr and `systemMessage` JSON are both swallowed. `write $USER tty` is also unreliable (TUI refresh hides it). Agent proactive reporting is the only reliable channel for pull failure warnings.

`MEMORY_SYNC=0` in `tamago/local.conf` (or env var `LAOMEI_MEMORY_SYNC=0`) disables all sync.

## Memory Types

There are four memory types. Write each to its own file with this frontmatter:

```markdown
---
name: <memory name>
description: <one-line description — used to judge relevance in future sessions>
type: <user | feedback | project | reference>
---
```

- **user** — role, goals, preferences, knowledge level of the person you work with
- **feedback** — guidance on approach: what to avoid, what worked well. Lead with the rule, then **Why:** and **How to apply:** lines
- **project** — ongoing work, goals, decisions, deadlines. Lead with the fact, then **Why:** and **How to apply:** lines. Convert relative dates to absolute
- **reference** — pointers to external systems (Linear projects, Grafana dashboards, etc.)

After writing a topic file, add a one-line pointer to `MEMORY.md`:
`- [Title](file.md) — one-line hook (under ~150 chars)`

**Do NOT save to memory**: code patterns, git history, debugging solutions, anything
already in CLAUDE.md files, or ephemeral task details.

## Before Acting on a Memory

A memory that names a file, function, or flag is a claim it existed *when written*.
Before recommending it:
- Named file path → verify it exists
- Named function/flag → grep for it
- User is about to act on your recommendation → verify current state first

"Memory says X exists" ≠ "X exists now."
