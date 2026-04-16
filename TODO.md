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

### Memory Import on Hatch ✅ (implemented in SKILL.md)
During hatch Turn 2, ask the user for source files to import into the new agent's memory.
The running agent (LLM) reads the sources, synthesizes and organizes content into topic
files (`diary.md`, `user_profile.md`, `work_style.md`, etc.), writes them into the memory
dir, updates MEMORY.md, and commits.

- Input: directory path, file paths, or pasted content — all handled by the agent via Read/Glob
- colleague-skill output SKILL.md is also a valid import source

### Memory Seeding from Parent Agent
When hatching a new agent for the same user, let the running agent (e.g. hammer.mei)
read its own memory and synthesize a `user_profile.md` to gift to the new agent —
so the new agent knows the user from day one.

Decision: auto-populate from running agent's existing memory (no extra questions — seamless).

---

_Last updated: 2026-04-16_
