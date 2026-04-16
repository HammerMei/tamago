# tamago — TODO / Ideas

## 🥚 Hatch Enhancements

### colleague-skill Integration
Integrate with [titanwings/colleague-skill](https://github.com/titanwings/colleague-skill) —
a skill that turns departing colleagues' knowledge (Slack, docs, chat logs) into AI personas.

Proposed directions:

- **A. Install as tamago skill** — clone colleague-skill into `skills/` so it's available
  in any tamago project
- **B. `hatch --from-colleague <skill.md>`** — import colleague-skill output to auto-populate
  the persona template (identity, communication style, decision patterns)
- **C. Colleague-as-tamago-agent** — place colleague-skill SKILL.md directly into a profile's
  `agents/` dir, giving the "colleague" a full tamago lifecycle with memory
- **D. `birth_certificate.md` 原型 field** — when hatched from a colleague-skill source,
  record `原型 | <person name>` in the birth certificate

Favourite combo: **C + D** 🔥

### Memory Seeding on Hatch
When hatching a new agent for the same user, let the running agent (e.g. hammer.mei)
read its own memory and synthesize a `user_profile.md` to gift to the new agent —
so the new agent knows the user from day one.

Two sub-options still to decide:
- Auto-populate from running agent's existing memory (seamless, no extra questions)
- Or ask the user a couple of targeted questions during hatch

---

_Last updated: 2026-04-16_
