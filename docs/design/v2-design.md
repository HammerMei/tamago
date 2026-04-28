# Tamago v2 Design Spec

> **STATUS: IMPLEMENTED ✅**
> This document describes the tamago v2 architecture as built.
> Where the implementation differs from the original spec, the actual behavior is noted inline.
> See also: [README](../../README.md) · [docs/memory-sync.md](../memory-sync.md)

---

> Key shift from v1: tamago.conf (TOML) is the central deployment manifest — scope/config
> are deployment decisions, not baked into agent files.

---

## Core Problems Being Solved

| # | Problem | Root Cause |
|---|---------|-----------|
| 1 | Settings are rigid — can't customize | tamago symlinks a monolithic settings.json |
| 2 | Per-project install creates friction | No central manifest; CLI flags control everything |
| 3 | Single-agent assumption | No clean story for multiple personas or utility agents |
| 4 | No external skill support | Skills must live inside tamago or profile repo |
| 5 | Scope baked into agent files | Deployment decisions shouldn't live in the persona source |

---

## Design Principles

1. **tamago.conf is the source of truth** — like `package.json`; one file declares what agents/skills to install and how
2. **Agent files stay pure** — persona `.md` files describe the agent, not where it gets deployed
3. **Tamago patches, not owns** — settings files belong to the user; tamago only injects its sections
4. **Skills are pluggable** — any git repo can be a skill; tamago manages the install
5. **Scope is a deployment decision** — declared in tamago.conf, not in the agent source file

---

## The New tamago.conf (TOML)

This file lives at **`.tamago/tamago.conf`** inside the project (committed to git — it's a
declaration, not machine state). Machine-specific paths go in `.tamago/machine.toml` (gitignored).

```toml
# tamago.conf — project agent deployment manifest

[[profiles]]
# Named profile (tamago looks for ~/.tamago/<name>-profile/)
name = "hammer.mei"
# OR: git URL (tamago clones on first install, pulls on update)
# repo = "git@github.com:you/hammer.mei-profile.git"
# Only one [[profiles]] entry for now; array syntax is future-proof for multi-profile

[[agents]]
name = "hammer.mei"           # must match a *.persona.md in the profile's agents/ dir
source = "profile"            # profile | tamago | <git-url>
scope = "project"             # global | project
tts = true
memory = true

# Same profile, same agent, TTS off — different project, different tamago.conf
# [[agents]]
# name = "hammer.mei"
# source = "profile"
# scope = "project"
# tts = false                 # tamago strips the TTS section when generating agent .md
# memory = true

[[agents]]
name = "code-reviewer"
source = "tamago"             # built-in tamago agent
scope = "global"

[[agents]]
name = "technical-writer"
source = "tamago"
scope = "global"

[[skills]]
name = "text-to-speech"
source = "tamago"             # built-in tamago skill
scope = "global"

[[skills]]
name = "hatch"
source = "tamago"
scope = "global"

# External skill — tamago clones and symlinks
[[skills]]
name = "my-workflow-skill"
source = "https://github.com/user/my-workflow-skill"
scope = "project"

[settings]
memory_sync = true            # set false to disable git memory sync
```

### Machine-specific state: `.tamago/machine.toml` (gitignored)

```toml
# .tamago/machine.toml — auto-generated, never commit
# Stores resolved paths and install state for this machine

[profiles]
# array index matches [[profiles]] order in tamago.conf
"hammer.mei" = "/Users/glin/.tamago/hammer.mei-profile"

[skill_cache]
"my-workflow-skill" = "/Users/glin/.tamago/skill-cache/my-workflow-skill"
```

---

## Answering: hammer.mei vs hammer.mei-headless?

**You don't need two persona files.** The persona source (`hammer.mei.persona.md`) is the same.
The deployment config controls the output:

```
Project A — tamago.conf:            Project B — tamago.conf:
  [[agents]]                          [[agents]]
  name = "hammer.mei"                 name = "hammer.mei"
  tts = true          ──┐             tts = false         ──┐
                        ↓                                   ↓
              .claude/agents/                     .claude/agents/
              hammer.mei.md                       hammer.mei.md
              (with TTS section)                  (TTS section stripped)
```

Same agent name. Same profile. Same persona source. Different generated output per project.
No `hammer.mei-headless.persona.md` needed — ever.

---

## Answering: Multiple Persona Profiles (hammer.mei + edm_mei)?

Yes, fully supported. Two options:

### Option A: Same profile repo, multiple personas

```
hammer.mei-profile/
└── agents/
    ├── hammer.mei.persona.md
    └── edm_mei.persona.md
```

```toml
# tamago.conf
[[profiles]]
name = "hammer.mei"           # profile repo (contains both personas)

[[agents]]
name = "hammer.mei"
source = "profile"
scope = "project"

[[agents]]
name = "edm_mei"
source = "profile"
scope = "project"
tts = true
```

### Option B: Separate profile repos

```toml
[[agents]]
name = "hammer.mei"
source = "git@github.com:you/hammer.mei-profile.git"
scope = "project"

[[agents]]
name = "edm_mei"
source = "git@github.com:you/edm_mei-profile.git"
scope = "global"
```

tamago clones each source to the skill/profile cache and generates from there.

---

## Change 1: Settings — Patch/Merge, Not Symlink

tamago injects only three things into settings files; everything else is untouched:

| Key | Merge Rule |
|-----|-----------|
| `hooks.<event>[]` | Append tamago entries; dedup by `command` string literal (unexpanded); operate on the inner `hooks[]` of the first matcher, or create `{"matcher":"","hooks":[...]}` if none exists |
| `permissions.allow[]` | Set union |
| `statusLine` | Set only if key is absent |

> ⚠️ **Hooks structure note**: Claude Code settings have two levels — outer `hooks.<event>` is an
> array of **matchers** (objects with optional `matcher` + inner `hooks[]`). tamago appends to the
> inner `hooks[]` of the first existing matcher, or creates a new bare matcher if none exists.
> Dedup compares the `command` string **as written** (env vars not expanded — write them literally,
> e.g. `"$HOME/.tamago/scripts/memory-sync.sh"` — don't expand at install time).

### Where patches go

| Content | Target |
|---------|--------|
| Core hooks (memory-sync, nagori) | `~/.claude/settings.json` (global, once per machine) |
| Agent/skill-specific permissions | Same scope as the agent/skill |

### Tracking: sidecar manifest under `.tamago/`

tamago records what it injected in a **separate sidecar file** under its own directory,
not in the user's `settings.json`. Global and project state are symmetric:

```
~/.tamago/settings-manifest.toml    # tracks patches to ~/.claude/settings.json  (tamago home)
.tamago/settings-manifest.toml      # tracks patches to .claude/settings.json    (already gitignored)
```

```toml
version = "0.2.0"

[managed_hooks]
SessionStart     = ["...memory-sync --init..."]
UserPromptSubmit = ["...memory-sync --pull..."]
Stop             = ["...memory-sync --push..."]

managed_perms = [
  "Bash(python3 *.claude/skills/*.py *)"
]
```

Benefits of `.tamago/` over `.claude/`:
- `.tamago/` is already gitignored — no extra ignore rules needed
- All tamago state in one place: `machine.toml` + `settings-manifest.toml`
- Keeps `.claude/` clean (only user + Claude Code content)

`install` reads existing settings → merges → writes merged settings + updates sidecar.
`uninstall` reads sidecar → removes exactly those entries from settings → deletes sidecar.

### Shell bridge: `.tamago/machine.env` (gitignored)

Shell scripts (like `memory-sync.sh`) cannot parse TOML. tamago generates a lightweight
KEY=VALUE shell-sourceable file alongside `machine.toml` at install time:

```
.tamago/machine.env        # project scope (auto-generated by tamago install, gitignored)
~/.tamago/machine.env      # global scope (auto-generated by tamago install)
```

Contents — stripped, safe to `source`:
```bash
PROFILE_REPO=/Users/glin/.tamago/hammer.mei-profile
MEMORY_SYNC=1
AGENT_NAME=hammer.mei
```

`memory-sync.sh` sources `.tamago/machine.env` rather than grep-parsing `tamago.conf`.
`machine.env` is always regenerated by `tamago install` from `machine.toml` — never hand-edit.

### Global agents × memory-sync hook interaction

Hooks injected into `~/.claude/settings.json` fire for **every** project Claude Code opens,
including non-tamago ones. `memory-sync.sh` must exit silently when there is no tamago context.

Discovery priority for `machine.env` (evaluated by `memory-sync.sh` at hook fire time):

1. `$PWD/.tamago/machine.env` — project-specific profile
2. `~/.tamago/machine.env` — global fallback (for global-scope agents)
3. Neither found → `exit 0` (not a tamago project or no agents installed; sync skipped)

**Consequence**: if you open a project with its own `tamago.conf`, its profile always wins
over any globally-installed profile. If the project has no `tamago.conf`, the global profile
is used (useful for single-user machines where all agents are globally installed).

### Migration from old symlink installs
If `.claude/settings.json` is a symlink → detect it → unlink → re-apply as patched real file.

---

## Change 2: Agent Installation (frontmatter NOT changed)

Agent `.md` and `.persona.md` files **stay as-is** — no new frontmatter fields needed.
All deployment decisions (scope, tts, memory) live in `tamago.conf`.

### Install behavior by agent type

**`*.persona.md`** (e.g., `hammer.mei.persona.md`):
- tamago merges: `tamago-agent-base.md` + persona file → generated `<name>.md`
- Memory path injected based on tamago.conf `scope`:
  - `scope = "project"` → `.claude/agent-memory/<name>/`
  - `scope = "global"`  → `~/.claude/agent-memory/<name>/`
- If `tts = false` in tamago.conf → TTS section stripped during generation

**Plain `*.md`** (e.g., `code-reviewer.md`):
- Symlinked to install target as-is
- No merge, no memory

### Install targets

| tamago.conf scope | Agents target | Memory target |
|-------------------|---------------|---------------|
| `project` | `.claude/agents/<name>.md` | `.claude/agent-memory/<name>/` |
| `global`  | `~/.claude/agents/<name>.md` | `~/.claude/agent-memory/<name>/` |

---

## Change 3: Skills — External Repo Support ("App Store")

Skills can now come from any git repo, not just tamago or the profile.

### Two skill repo modes

Skills can live in a **single-skill repo** (repo root = skill) or a **multi-skill repo**
(repo contains multiple skills in subdirectories). An optional `path` field distinguishes them.

**Mode A — one repo, one skill** (no `path`):
```toml
[[skills]]
name = "text-to-speech"
source = "https://github.com/user/my-tts-skill"
# repo root is the skill dir — contains SKILL.md directly
```

**Mode B — one repo, many skills** (with `path`):
```toml
[[skills]]
name = "text-to-speech"
source = "https://github.com/user/my-skill-collection"
path = "skills/text-to-speech"

[[skills]]
name = "daily-briefing"
source = "https://github.com/user/my-skill-collection"
path = "skills/daily-briefing"
```

### Source resolution table

| `source` | `path` | tamago uses |
|---|---|---|
| `"tamago"` | n/a | `~/.tamago/skills/<name>/` |
| `"profile"` | absent | `<profile-repo>/skills/<name>/` |
| `"profile"` | present | `<profile-repo>/<path>/` |
| git URL | absent | cloned repo root (single-skill mode) |
| git URL | present | `<cloned-repo>/<path>/` (multi-skill mode) |

### Install flow for external skills

```
1. Compute cache key: hash of repo URL → ~/.tamago/repo-cache/<hash>/
2. If cache miss → git clone <source> into cache dir
   If cache hit  → skip (git pull happens on tamago update, not install)
3. Resolve skill dir: cache root (no path) OR cache/<path> (with path)
4. Symlink resolved skill dir → install target
```

**Smart caching**: two skills sharing the same `source` URL clone the repo once;
both symlink different subdirectories from the same cache entry.

`tamago update` → `git pull --rebase` in each unique cached repo, updating all
skills from that source in one operation.

### Install targets (same scope logic as agents)

| tamago.conf scope | Skills target |
|-------------------|---------------|
| `global`  | `~/.claude/skills/<name>/` and `~/.opencode/skills/<name>/` (symlink) |
| `project` | `.claude/skills/<name>/` and `.opencode/skills/<name>/` (symlink) |

Skills stay as **symlinks** (not copies) — skill directories coexist fine, no merge needed.
URL-sourced skills support both scopes. Skills with a `bin/` directory automatically get
their CLI entry points symlinked into `~/.local/bin` regardless of scope.

### If user wants a custom skill not in tamago

Two options:
1. Point to their own git repo: `source = "git@github.com:user/my-skill"`
2. Put it in their profile repo: `source = "profile"` (profile has a `skills/` dir)

---

## Change 4: `tamago install` New Flow

```bash
tamago install [--config ./tamago.conf]   # defaults to ./tamago.conf in cwd
```

No more `--scope`, `--no-tts`, `--agents`, `--profile`, `--install-global` flags.
Everything is declared in tamago.conf.

### Step-by-step

```
1. Read tamago.conf (TOML) from current dir (or --config path)
2. Resolve profile(s) from [[profiles]] (currently one; array is future-proof):
   - profiles[0].name → ~/.tamago/<name>-profile/
   - profiles[0].repo → clone/pull to ~/.tamago/<repo-name>/
   - write resolved path(s) to .tamago/machine.toml
3. Pull latest tamago + profile repos

4. For each [[agents]] entry:
   a. Locate source file (tamago built-in / profile / clone from git)
   b. Determine generated name: <name>.md
   c. Install:
      *.persona.md → merge tamago-base + persona (apply tts flag) → write to scope target
      *.md         → symlink to scope target
   d. If memory = true → create memory symlink at scope target

5. For each [[skills]] entry:
   a. Resolve source (tamago / profile / clone from git URL)
   b. Symlink skill dir to scope target (global or project)
   c. Record cache path in .tamago/machine.toml

6. Patch settings (NOT symlink):
   a. Patch ~/.claude/settings.json with tamago core hooks + base perms
   b. Write sidecar ~/.claude/.settings-tamago-manifest.json

7. Update .gitignore (add .tamago/ if not present)
8. Run health check
```

### `tamago update` — alias for `tamago install`

Currently identical behavior. Future extension: `update` could do "only pull latest skill repos without full reinstall". For now, alias.

### Removed commands
- `install-global` — **removed**. Use `scope = "global"` in tamago.conf.
- `uninstall-global` — **removed**. `tamago uninstall` handles all scopes.

---

## Change 5: `tamago uninstall`

```bash
tamago uninstall [--config ./tamago.conf]
```

```
1. Read tamago.conf + .tamago/machine.toml (for resolved paths)
2. For each installed agent (from machine.toml):
   - persona: delete generated .md from install target
   - utility: unlink
   - memory: remove memory symlink
3. For each installed skill:
   - Unlink from install target
   - (Cache in ~/.tamago/skill-cache/ kept — use `tamago prune` to clean)
4. Un-patch settings:
   - Read sidecar → remove tracked entries from settings files → delete sidecar
5. Clean .gitignore entries
```

---

## File Layout After Refactor

```
project-root/
├── tamago.conf                      ← NEW: committed, the deployment manifest
├── .tamago/
│   ├── machine.toml                 ← NEW: gitignored, machine-specific state
│   └── settings-manifest.toml      ← NEW: gitignored, tracks tamago's settings patches
├── .gitignore                        ← tamago adds: .tamago/ (whole dir gitignored)
├── .claude/
│   ├── settings.json                 ← user-owned; tamago patches, doesn't own
│   ├── agents/
│   │   └── hammer.mei.md            ← generated by tamago (gitignored)
│   ├── skills/
│   │   └── text-to-speech → ...     ← symlink (gitignored)
│   └── agent-memory/
│       └── hammer.mei → ...         ← symlink (gitignored)
└── ...

~/.tamago/                           ← tamago home
├── hammer.mei-profile/              ← profile repo (cloned)
├── skill-cache/
│   └── my-workflow-skill/           ← external skill (cloned)
├── settings-manifest.toml           ← NEW: tracks patches to ~/.claude/settings.json
└── scripts/
    └── memory-sync.sh

~/.claude/
├── settings.json                    ← global; tamago patches core hooks here
├── agents/
│   └── code-reviewer.md            ← global utility agents (symlinked)
└── skills/
    └── hatch → ...                  ← global skills (symlinked)
```

---

## Open Questions (Updated)

### Q8: memory-sync.sh TOML bridge ✅ Decided
`tamago install` writes `.tamago/machine.env` (KEY=VALUE, gitignored). `memory-sync.sh` sources
this file instead of grep-parsing `tamago.conf`. Keeps sync script in plain bash — no TOML parser.

### Q9: Global agent × memory-sync hook interaction ✅ Decided
`memory-sync.sh` probes: project `machine.env` → global `machine.env` → exit 0.
Project tamago.conf always wins if present. See "Global agents × memory-sync" section.

### Q10: `~/.claude/agent-memory/` permission behavior ✅ Verified (empirical)
Empirically confirmed: reading from `~/.claude/agent-memory/` does NOT trigger permission prompts
in Claude Code. Agents can use this path for global-scope memory without approval friction.

### Q11: Hooks dedup rule ✅ Decided
Dedup compares `command` strings as written (no env-var expansion). tamago appends to the inner
`hooks[]` of the first existing matcher object, or creates `{"matcher":"","hooks":[...]}`.
See the ⚠️ note under "Change 1: Settings".

### Q1: TOML ✅ Decided
TOML for tamago.conf and machine.toml. More human-friendly than JSON or INI.

### Q2: Sidecar manifest ✅ Decided
`.settings-tamago-manifest.json` as a sidecar file (gitignored). Not inside settings.json.

### Q3: Global persona agents ✅ Decided
Fully supported via `scope = "global"` in tamago.conf. Use case: single-user lab machine.

### Q4: Per-agent scope override ✅ Resolved
Handled naturally by tamago.conf — each `[[agents]]` entry has its own `scope`.

### Q5: tamago update ✅ Decided
Alias for `tamago install` now. Future: `update` can mean "pull latest skill repos".

### Q6: Profile-level skill scope override ✅ Decided
Configurable in tamago.conf. If complex, postpone.
```toml
[[skills]]
name = "some-tamago-builtin"
source = "tamago"
scope = "project"    # override tamago's default scope
```

### Q7: Custom skills ✅ Decided
Users include only what they want in tamago.conf. Don't want a skill? Don't list it.
Want a custom skill? Point to their own git repo as source.

---

## What Doesn't Change

| Component | Status |
|-----------|--------|
| `memory-sync.sh` | Minor update: sources `.tamago/machine.env` instead of grep-parsing `tamago.conf`; discovery priority: project env → global env → exit 0 |
| `tamago-agent-base.md` merge logic | Unchanged |
| Profile repo structure | Unchanged (agents/, skills/, memory/) |
| `hatch` skill | Unchanged |
| `health-check.sh` | Unchanged |
| Templates | Unchanged |
| Agent `.md` / `.persona.md` frontmatter | No new fields added |

---

## Implementation Order

| Phase | Change | Scope |
|-------|--------|-------|
| 1 | TOML tamago.conf parser | Core: everything depends on this |
| 2 | Settings patch/merge + sidecar manifest | Core: biggest user pain point |
| 3 | `tamago install` reads tamago.conf | Core: replaces all old CLI flags |
| 4 | External skill repo support (git clone) | Feature |
| 5 | `tamago uninstall` rewrite | Cleanup |
| 6 | `tamago update` alias | Polish |
| 7 | `tamago prune` (clean skill cache) | Polish |
