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
| `skills/` | Packaged skills (TTS, daily briefing, …) |
| `scripts/memory-sync.sh` | Git-backed memory sync across machines |
| `setup.py` | Symlink installer (`install-global`, `install --profile`) |
| `settings/claude/settings.json` | Hooks + permissions (no agent name) |
| `settings/opencode/opencode.json` | OpenCode config base |
| `agents/` | Generic agents (code-reviewer, technical-writer) |
| `local.conf` | *(gitignored)* Points to your profile repo |

## How it works

```
tamago (engine)          +   your-profile (soul)
─────────────────────        ──────────────────────────
hooks, skills, scripts       persona .md, memory, secrets
settings without agent   +   settings with agent name
local.conf → profile repo    agents/memory/ (git synced)
```

Memory sync runs on every session — pulling before your prompt, pushing after your reply.  
The profile repo can live anywhere: GitHub, a private bare repo on your home server, whatever keeps your data yours.

## Quick start

```bash
# 1. Clone tamago
git clone https://github.com/HammerMei/tamago ~/workspace/tamago

# 2. Clone your profile repo (wherever it lives)
git clone <your-profile-remote> ~/workspace/your-profile

# 3. Install global hooks
cd ~/workspace/your-project
python3 ~/workspace/tamago/setup.py install-global

# 4. Install into a project with your profile
python3 ~/workspace/tamago/setup.py install --profile ~/workspace/your-profile
```

---

<div align="center">
  <sub>Mascot & spokesperson: <strong>Hammer Mei</strong> 🔨</sub>
</div>
