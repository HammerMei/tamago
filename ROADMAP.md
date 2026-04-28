# Roadmap

Ideas and planned improvements for tamago.

---

## In progress / near-term

### v2 installer — tamago.conf
Replace all `setup.py` CLI flags with a single `tamago.conf` (TOML) declaration file.
One file per project declares which agents/skills to install, at what scope, with what options.

See [`docs/design/v2-design.md`](docs/design/v2-design.md) for the full spec.

Key changes:
- **Settings as patches** — tamago injects only its hooks and permissions; user settings are not replaced
- **External skill repos** — any git repo can be a skill source (think: apt for Claude skills)
- **Multi-profile support** — multiple agents from different profile repos in one project
- **`scope` as a deployment decision** — declared in tamago.conf, not baked into persona files

---

## Ideas / backlog

### Hatch enhancements

**colleague-skill integration** — the [`colleague-skill`](https://github.com/titanwings/colleague-skill)
project converts departing colleagues' knowledge (Slack, docs, chat logs) into AI personas.
Potential tamago integration:

- `hatch --from-colleague <skill.md>` — import colleague-skill output to auto-populate persona
- Colleague-as-tamago-agent — place colleague-skill output directly into a profile's `agents/` dir,
  giving the "colleague" a full tamago lifecycle with persistent memory
- `birth_certificate.md` `原型` field — when hatched from a colleague source, record the origin

**Memory seeding from parent agent** — when hatching a new agent for the same user, let the
running agent read its own memory and synthesize a starter `user_profile.md` to gift the new
agent — so it knows the user from day one.

### External skill registry
A curated list of community skills installable via URL in `tamago.conf`:
```toml
[[skills]]
name = "some-skill"
source = "https://github.com/user/some-tamago-skill"
```

### `tamago prune`
Clean the external skill clone cache at `~/.tamago/repo-cache/` — remove repos no longer
referenced by any installed `tamago.conf`.

---

_The best features come from real use. Open an issue or PR if you have ideas._
