---
name: daily-briefing
description: provide daily briefing summary
---
# Skill: daily-briefing

Create or overwrite `daily-briefing.md` with a structured daily briefing, then open it in MarkText.

## Scope
- Write exactly one output file: `daily-briefing.md` in workspace root.
- Always overwrite the full file.
- Use managed automation script: `.claude/skills/daily-briefing/daily_briefing_runner.py`.

## Automation-first lifecycle
- Script must contain `# SKILL_SHA256: <hash>` and matching `EMBEDDED_SKILL_SHA256`.
- Before run, compare current `SKILL.md` hash vs embedded hash.
- Full-regenerate script (entire file overwrite) if script is missing, hash changed, self-check fails, runtime fails, or output structure is invalid.
- Do not partial patch/hash-only sync runner.
- If first regeneration fails, regenerate once more and retry.
- If still failing, complete briefing by direct execution flow and clearly mark unavailable parts.

## How to run
- `python3 .claude/skills/daily-briefing/daily_briefing_runner.py`
- `python3 .claude/skills/daily-briefing/daily_briefing_runner.py --self-check`

## Required sections and order
1. Date/time title
2. Market Watch ("MSFT", "INTC", "NVDA", "PYPL", "BTC"])
3. Top 10 TechCrunch
4. Top 10 Hacker News
5. Top 10 GitHub Weekly Trending Projects
6. Top 5 US/Global News
7. 微博娛樂熱搜 Top 5

## Data sources (news sections use same format)
- TechCrunch (Top 10): `https://techcrunch.com/feed/`
- Hacker News (Top 10):
  - `https://hacker-news.firebaseio.com/v0/topstories.json`
  - `https://hacker-news.firebaseio.com/v0/item/<id>.json`
- GitHub Weekly Trending Projects (Top 10):
  - `https://github.com/trending?since=weekly`
- US/Global News (Top 5): public RSS/news source (BBC/Reuters/AP/Google News RSS or equivalent)
- 微博娛樂熱搜 (Top 5): public reachable source for entertainment hot-search items

## Market Watch requirements
- Read finance tickers from `AGENTS.md` user profile.
- Map BTC for quote provider if needed.
- Use reliable no-auth quotes source.
- For each symbol include:
  - symbol
  - latest price
  - currency (if available)
  - Up (▲) or Down (▼) (if available)

## Unified news item format (for all 5 news sections)
- `N. **<headline>**: <1-2 sentence summary>`
- `   - Link: <url>`
- Summary fallback:
  - Prefer article summary when fetchable.
  - Else summarize from title/metadata.
  - Else write `Summary unavailable from source metadata.`

## Compose markdown structure
```markdown
# Daily Briefing - YYYY-MM-DD HH:mm

## Market Watch
- **MSFT**: <price> <currency> **<▲ or ▼>**

## Top 10 TechCrunch
1. **<headline>**: <1-2 sentences>
   - Link: <url>

## Top 10 Hacker News
1. **<headline>**: <1-2 sentences>
   - Link: <url>

## Top 10 GitHub Weekly Trending Projects
1. **<github name>**: <1-2 sentences>
   - Link: <url>

## Top 5 US/Global News
1. **<headline>**: <1-2 sentences>
   - Link: <url>

## 微博娛樂熱搜 Top 5
1. **<headline>**: <1-2 sentences or fallback>
   - Link: <url>
```

## Final steps
1. Overwrite `daily-briefing.md`.
2. Open file: `open -a "MarkText" "daily-briefing.md"`.
3. Send post-run summary in response (one short paragraph each):
   - Market Watch
   - Top 10 TechCrunch
   - Top 10 Hacker News
   - Top 10 GitHub Weekly Trending Projects
   - Top 5 US/Global News
   - 微博娛樂熱搜 Top 5
4. Spoken delivery required: read a concise version of the section summaries via TTS using default profile voice/style.

## Quality bar
- Factual and concise.
- Preserve working links.
- No fabrication.
- If sources are unavailable, keep full structure and mark unavailable explicitly.
