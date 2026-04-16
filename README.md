<div align="center">
  <img src="logo.png" width="180" alt="Tamago mascot" />

  # 🥚 tamago

  **AI agent engine shell — hatch your own agent**

  *Skills · Hooks · Memory sync · Setup*
</div>

---

Tamago (たまご, "egg") is the reusable engine layer for running persistent AI agents with Claude Code and OpenCode.  
It provides the mechanics — **you bring the soul**.

## What's inside

| Path | Purpose |
|------|---------|
| `skills/` | Packaged skills (TTS, daily briefing, restart-cli, **hatch**) |
| `templates/` | Profile scaffolding templates used by the hatch skill |
| `scripts/memory-sync.sh` | Git-backed memory sync across machines |
| `scripts/health-check.sh` | Post-setup environment health check |
| `setup.py` | Symlink installer (`install-global`, `install --profile`) |
| `git-hooks/post-merge` | Auto-regenerates agent files after `git pull` on tamago |
| `docs/tamago-agent-base.md` | Common mechanics merged into every agent system prompt |
| `settings/claude/settings.json` | Hooks + permissions (no agent name) |
| `settings/opencode/opencode.json` | OpenCode config base |
| `agents/` | Generic agents (code-reviewer, technical-writer) |
| `local.conf` | *(gitignored)* Points to your profile repo and project dir |


## How it works

```
tamago (engine)               +   your-profile (soul)
──────────────────────            ──────────────────────────────
hooks, skills, scripts            <name>.persona.md
settings without agent name   +   settings with agent name
templates/                        agents/memory/ (git synced)
local.conf → profile repo         secrets/
```

`setup.py install --profile` merges tamago's common mechanics (`docs/tamago-agent-base.md`)
with your persona file (`agents/<name>.persona.md`) into a single generated agent `.md`.  
Memory sync runs on every session — pulling before your prompt, pushing after your reply.  
The profile repo can live anywhere: GitHub, a private bare repo on your home server, whatever keeps your data yours.


## Quick start

```bash
# 1. Clone tamago
git clone https://github.com/HammerMei/tamago ~/workspace/tamago

# 2. Install global hooks + settings
python3 ~/workspace/tamago/setup.py install-global

# 3. Clone (or create) your profile repo
git clone <your-profile-remote> ~/workspace/your-profile
# — OR — hatch a brand-new one (see below)

# 4. Install into a project
cd ~/workspace/your-project
python3 ~/workspace/tamago/setup.py install --profile ~/workspace/your-profile

# 5. Run the health check to verify everything is wired up
bash ~/workspace/tamago/scripts/health-check.sh
```


## Hatch a new agent 🐣

The `hatch` skill lets you create a complete agent profile through a short conversation — no manual file editing required.

### Interactive (recommended)

Once tamago is installed in a project, tell your agent:

> "Help me hatch a new agent"

The agent will:
1. List available skills
2. Present a table of proposed defaults (name, language, tone, TTS, etc.)
3. Wait for you to confirm or adjust
4. Run `hatch.py` and install the new profile automatically

### CLI / manual

```bash
python3 .claude/skills/hatch/hatch.py \
  --name "xiao.mei" \
  --display-name "小小妹" \
  --description "Junior research and task-delegation assistant" \
  --language "Traditional Chinese" \
  --tone "Energetic, helpful, slightly junior" \
  --user-address "老哥" \
  --profile-dir ~/workspace/xiao.mei-profile \
  --tts --tts-voice "Meijia" \
  --skills "text-to-speech,daily-briefing" \
  --install     # runs setup.py install --profile automatically
```

After hatching, **restart Claude** to activate the new agent.

#### Key flags

| Flag | Description |
|------|-------------|
| `--name` | Agent identifier slug, e.g. `xiao.mei` |
| `--display-name` | Human-readable name |
| `--description` | One-line description |
| `--language` | Response language (default: `Traditional Chinese`) |
| `--tone` | Personality/style description |
| `--user-address` | How the agent addresses the user (default: `老哥`) |
| `--profile-dir` | Where to create the profile repo |
| `--remote` | Git remote URL (optional) |
| `--tts` | Include TTS instructions in the persona |
| `--tts-voice` | TTS voice name (default: `Meijia`) |
| `--skills` | Comma-separated skill names to include |
| `--install` | Auto-run `setup.py install --profile` after creation |
| `--dry-run` | Preview what would be created without writing files |


## Profile structure

A profile repo contains the "soul" — everything agent-specific:

```
your-profile/
  .gitignore
  agents/
    <name>.persona.md          ← persona: identity, tone, TTS, etc.
    memory/
      <name>/
        MEMORY.md              ← memory index (auto-loaded each session)
        user_profile.md        ← user facts (loaded on demand)
        feedback_*.md          ← agent behavior corrections
        project_*.md           ← ongoing project context
        env-<hostname>/        ← machine-specific memory
          MEMORY.md
          visited.md
  settings/
    claude/
      settings.json            ← {"agent": "<name>"}
    opencode/
      opencode.json            ← {"default_agent": "<name>"}
  secrets/                     ← (gitignored) KeePass databases, keys
```

### Persona file format

`agents/<name>.persona.md` has two sections separated by `---`:

```markdown
---
name: my.agent
description: >
  What this agent does, in one line.
skills:
  - text-to-speech
  - daily-briefing
memory: project
maxTurns: 12
---

# Persona
... identity, language style, TTS config, etc. ...
```

`setup.py install --profile` merges this with `docs/tamago-agent-base.md` (memory mechanics)
into the generated `.claude/agents/<name>.md`. **Edit only the persona file, never the generated file.**


## Memory sync

Memory is git-backed and syncs automatically via Claude Code hooks:

| Hook | Action |
|------|--------|
| `SessionStart` | Create env dir + record harness/model in `visited.md` |
| `UserPromptSubmit` | `git pull --rebase` — fetch latest memory |
| `Stop` | Commit changed memory files → pull → push |

All memory lives in `<profile-repo>/agents/memory/<agent-name>/`.  
Set `LAOMEI_MEMORY_SYNC=0` to disable sync (offline / emergency use).


## Available skills

| Skill | Description |
|-------|-------------|
| `text-to-speech` | macOS TTS with background queue, voice selection, rate control |
| `daily-briefing` | Markets, TechCrunch, Hacker News, GitHub trending, news, 微博熱搜 |
| `restart-cli` | Restart Claude Code / OpenCode session via AppleScript or cmux |
| `hatch` | Guided creation of a new agent profile (this skill) |

Add a skill to your persona's frontmatter `skills:` list to activate it.


## Regenerating agent files

After editing `<name>.persona.md` or pulling tamago updates:

```bash
cd ~/workspace/your-project
python3 ~/workspace/tamago/setup.py install --profile ~/workspace/your-profile
```

The `git-hooks/post-merge` hook (installed by `install-global`) does this automatically
after every `git pull` on tamago.

---

<div align="center">
  <sub>Mascot & spokesperson: <strong>Hammer Mei</strong> 🔨</sub>
</div>
