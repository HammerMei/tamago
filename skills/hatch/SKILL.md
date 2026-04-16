---
name: hatch
description: >
  Guided creation of a new tamago agent profile — persona, memory scaffold,
  settings, and optional git repo. Trigger when the user says "hatch an agent",
  "create a new agent", or wants to set up a new assistant persona.
---

# Skill: hatch

Scaffold a new tamago agent profile through a short guided conversation,
then materialize the files via `hatch.py`.

## What gets created

```
<profile-dir>/
  .gitignore
  agents/
    <name>.persona.md                    ← persona + optional TTS instructions
    memory/<name>/MEMORY.md              ← memory index (with birth_certificate pointer)
    memory/<name>/birth_certificate.md   ← 🥚 easter egg: birth timestamp, zodiac,
                                            hostname, hatcher lineage, tamago DNA
  settings/
    claude/settings.json       ← {"agent": "<name>"}
    opencode/opencode.json     ← {"default_agent": "<name>"}
```

## Conversation flow

### Turn 1 — gather and propose

List available tamago skills first (use the project-scoped symlink — no approval needed):

```bash
ls .claude/skills/
```

Then present a single table with proposed defaults and ask the user to confirm or revise:

| Field | Default / Proposed |
|-------|--------------------|
| `--name` | slug from user's idea, e.g. `xiao.mei` |
| `--display-name` | display name, e.g. `小小妹` |
| `--description` | one sentence summary |
| `--language` | `Traditional Chinese` |
| `--tone` | `Friendly and helpful` |
| `--user-address` | `老哥` |
| `--profile-dir` | `~/.tamago/<name>-profile` |
| `--remote` | (none — skip for local-only) |
| TTS enabled | no |
| `--tts-voice` | `Meijia` (zh-TW) / `Samantha` (en-US), only if TTS enabled |
| `--skills` | `text-to-speech` (if TTS), else none |

### Turn 2 — user confirms or revises values

### Turn 3 — clarify install target, then run hatch.py

> ⚠️ **One profile per project (current limitation)**
> Running `--install` in the current project replaces the active agent — the current
> profile's `settings.json` symlink and `local.conf` will be overwritten.
> If the user wants to hatch a sibling agent alongside the current one, they need a
> **separate project directory**.

Ask the user **before running** with `--install`:
> "Where should this agent be installed?
> - **Current project** (`<cwd>`) — replaces the active agent here
> - **New project dir** — I'll hatch the profile and give you the install command to run there"

If they choose a **new project dir**: run hatch **without** `--install`, then output:
```bash
mkdir -p <project-dir>
cd <project-dir>
python3 ~/.tamago/setup.py install-global   # if not done yet
python3 ~/.tamago/setup.py install --profile-dir <profile-dir>
```

If they choose the **current project**: proceed with `--install`:

```bash
python3 .claude/skills/hatch/hatch.py \
  --name "<name>" \
  --display-name "<display-name>" \
  --description "<description>" \
  --language "<language>" \
  --tone "<tone>" \
  --user-address "<user-address>" \
  --profile-dir "<profile-dir>" \
  [--remote "<url>"] \
  [--tts [--tts-voice "<voice>"]] \
  [--skills "<s1>,<s2>"] \
  [--hatcher "<your-agent-name>"] \
  [--lineage "<ancestor-chain>"] \
  [--no-memory-sync] \
  --install
```

#### Determining `--hatcher` and `--lineage`

Before running `hatch.py`, resolve the birth lineage:

1. **You have an agent name** (you are a named persona, not plain Claude):
   - Pass `--hatcher <your-agent-name>`
   - Check if your own `birth_certificate.md` exists at `$PROFILE_REPO/agents/memory/<your-agent-name>/birth_certificate.md`
   - If it exists: read the `家族族譜` row value (e.g. `石頭蹦出來的 🪨 → hammer.mei`)
     and pass `--lineage "<that-value> → <your-agent-name>"`
   - If it doesn't exist: omit `--lineage` (defaults to just your name)

2. **Bootstrap case** (plain Claude, no agent persona):
   - Omit both `--hatcher` and `--lineage`
   - hatch.py will record the hatcher as `石頭蹦出來的 🪨` 🪨

If no `--remote` is given, add `--no-memory-sync` to disable git memory sync and avoid
false failures in the health check. The user can enable sync later by removing
`MEMORY_SYNC=0` from `local.conf` after setting up a remote.

## After success

1. Tell the user: **restart Claude to activate the new agent**
2. If `--remote` was set: remind them to push:
   ```bash
   cd <profile-dir> && git push -u origin main
   ```
3. Let them know the persona lives at:
   `<profile-dir>/agents/<name>.persona.md`
   and can be regenerated after edits with:
   `python3 ~/.tamago/setup.py install --profile-dir <profile-dir>`

## Error handling

- **Profile dir exists and is non-empty**: ask user for a different `--profile-dir`
- **hatch.py exits non-zero**: show the error message; do not silently skip
- **setup.py install fails**: report the error; the profile files were still created
  and the user can run install manually
