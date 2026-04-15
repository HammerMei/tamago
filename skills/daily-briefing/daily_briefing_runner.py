#!/usr/bin/env python3
"""Generate daily-briefing.md and open it in MarkText."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import hashlib
import html
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


# SKILL_SHA256: dc4b7b0fce0fc8ae6c0e1c55dadb92dce15e6a739855966f446d9119a4aaf56f
EMBEDDED_SKILL_SHA256 = (
    "dc4b7b0fce0fc8ae6c0e1c55dadb92dce15e6a739855966f446d9119a4aaf56f"
)

TECHCRUNCH_RSS_URL = "https://techcrunch.com/feed/"
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
GITHUB_TRENDING_WEEKLY_URL = "https://github.com/trending?since=weekly"
GITHUB_REPO_API_URL = "https://api.github.com/repos/{repo}"
US_GLOBAL_RSS_URLS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
]
WEIBO_ENT_URL = "https://tophub.today/n/3QeLwJEd7k"


def _workspace_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _skill_path() -> pathlib.Path:
    return pathlib.Path(__file__).with_name("SKILL.md")


def _fetch_text(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_json(url: str, timeout: int = 12):
    return json.loads(_fetch_text(url, timeout=timeout))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _current_skill_hash() -> str:
    return _sha256_bytes(_skill_path().read_bytes())


def _strip_html(raw: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _truncate(text: str, limit: int = 220) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def read_tickers_from_profile(profile_path: pathlib.Path) -> list[str]:
    content = profile_path.read_text(encoding="utf-8")
    m = re.search(
        r"Finance interest:\s*Tracks prices of\s*(.+?)\.", content, flags=re.I
    )
    source = m.group(1) if m else "MSFT, INTC, NVDA, PYPL, BTC"
    normalized = re.sub(r"\band\b", ",", source, flags=re.I)
    parts = [p.strip().upper() for p in normalized.split(",") if p.strip()]
    found = [p for p in parts if re.fullmatch(r"[A-Z]{2,10}", p)]
    ordered: list[str] = []
    for ticker in found:
        if ticker not in ordered:
            ordered.append(ticker)
    return ordered or ["MSFT", "INTC", "NVDA", "PYPL", "BTC"]


def _stooq_symbol(ticker: str) -> str:
    if ticker == "BTC":
        return "btc.v"
    return f"{ticker.lower()}.us"


def fetch_market_quotes(tickers: list[str]) -> list[dict[str, str]]:
    quotes: list[dict[str, str]] = []
    for ticker in tickers:
        symbol = _stooq_symbol(ticker)
        url = f"https://stooq.com/q/l/?s={urllib.parse.quote(symbol)}&f=sd2t2ohlcv&h&e=csv"
        try:
            csv_text = _fetch_text(url)
            row = next(csv.DictReader(csv_text.splitlines()))
            close = row.get("Close", "N/A")
            opened = row.get("Open", "N/A")
            if close in {"N/D", "", None}:
                raise ValueError("No quote data")
            trend = "-"
            try:
                if float(close) > float(opened):
                    trend = "▲"
                elif float(close) < float(opened):
                    trend = "▼"
            except Exception:
                trend = "-"
            quotes.append(
                {"symbol": ticker, "price": close, "currency": "USD", "trend": trend}
            )
        except Exception:
            quotes.append(
                {
                    "symbol": ticker,
                    "price": "Unavailable",
                    "currency": "N/A",
                    "trend": "-",
                }
            )
    return quotes


def _article_summary(url: str) -> str | None:
    try:
        html_text = _fetch_text(url, timeout=10)
    except Exception:
        return None
    patterns = [
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, flags=re.I)
        if m:
            text = _truncate(_strip_html(m.group(1)))
            if text:
                return text
    return None


def fetch_techcrunch_top10() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        xml_text = _fetch_text(TECHCRUNCH_RSS_URL)
        root = ET.fromstring(xml_text)
        nodes = root.findall("./channel/item")[:10]
    except Exception:
        nodes = []

    for node in nodes:
        title = _strip_html(node.findtext("title", default="Untitled"))
        link = _strip_html(node.findtext("link", default=""))
        desc = _truncate(_strip_html(node.findtext("description", default="")))
        if not desc and link:
            desc = _article_summary(link) or "Summary unavailable from source metadata."
        if not desc:
            desc = "Summary unavailable from source metadata."
        items.append(
            {"headline": title, "summary": desc, "link": link or "Unavailable"}
        )

    while len(items) < 10:
        idx = len(items) + 1
        items.append(
            {
                "headline": f"Unavailable TechCrunch item {idx}",
                "summary": "Summary unavailable from source metadata.",
                "link": "Unavailable",
            }
        )
    return items[:10]


def fetch_hn_top10() -> list[dict[str, str]]:
    stories: list[dict[str, str]] = []
    try:
        ids = _fetch_json(HN_TOP_URL)[:10]
    except Exception:
        ids = []

    for story_id in ids:
        try:
            item = _fetch_json(HN_ITEM_URL.format(id=story_id))
            title = item.get("title", "Untitled")
            link = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            text = _strip_html(item.get("text", ""))
            summary = _article_summary(link) if item.get("url") else None
            if not summary and text:
                summary = _truncate(text)
            if not summary:
                summary = "Summary unavailable from source metadata."
            stories.append({"headline": title, "summary": summary, "link": link})
        except Exception:
            stories.append(
                {
                    "headline": f"HN story {story_id}",
                    "summary": "Summary unavailable from source metadata.",
                    "link": f"https://news.ycombinator.com/item?id={story_id}",
                }
            )

    while len(stories) < 10:
        idx = len(stories) + 1
        stories.append(
            {
                "headline": f"Unavailable Hacker News item {idx}",
                "summary": "Summary unavailable from source metadata.",
                "link": "https://news.ycombinator.com/",
            }
        )
    return stories[:10]


def _github_repo_summary(repo_name: str) -> str | None:
    try:
        payload = _fetch_json(GITHUB_REPO_API_URL.format(repo=repo_name), timeout=12)
    except Exception:
        return None
    desc = _truncate(_strip_html(str(payload.get("description") or "")))
    return desc or None


def fetch_github_weekly_top10() -> list[dict[str, str]]:
    projects: list[dict[str, str]] = []
    try:
        page = _fetch_text(GITHUB_TRENDING_WEEKLY_URL)
    except Exception:
        page = ""

    if page:
        blocks = re.findall(
            r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
            page,
            flags=re.I | re.S,
        )
        for block in blocks:
            link_match = re.search(
                r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"', block, flags=re.I | re.S
            )
            if not link_match:
                continue
            repo_path = html.unescape(link_match.group(1)).strip().strip("/")
            repo_name = repo_path.replace(" ", "")
            if "/" not in repo_name:
                continue
            repo_link = f"https://github.com/{repo_name}"

            summary = ""
            for candidate in re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S):
                cleaned = _truncate(_strip_html(candidate))
                if not cleaned:
                    continue
                if re.search(
                    r"\b(sponsor|star|stars today|fork|built by)\b", cleaned, flags=re.I
                ):
                    continue
                if len(cleaned) < 24:
                    continue
                summary = cleaned
                break
            if not summary:
                summary = (
                    _github_repo_summary(repo_name)
                    or "Summary unavailable from source metadata."
                )

            projects.append(
                {"headline": repo_name, "summary": summary, "link": repo_link}
            )
            if len(projects) == 10:
                break

    while len(projects) < 10:
        idx = len(projects) + 1
        projects.append(
            {
                "headline": f"Unavailable GitHub project {idx}",
                "summary": "Summary unavailable from source metadata.",
                "link": "https://github.com/trending?since=weekly",
            }
        )
    return projects[:10]


def fetch_us_global_top5() -> list[dict[str, str]]:
    candidates: list[tuple[dt.datetime | None, dict[str, str]]] = []
    for rss_url in US_GLOBAL_RSS_URLS:
        try:
            xml_text = _fetch_text(rss_url)
            root = ET.fromstring(xml_text)
            nodes = root.findall("./channel/item")
        except Exception:
            nodes = []

        for node in nodes:
            title = _strip_html(node.findtext("title", default="Untitled"))
            link = _strip_html(node.findtext("link", default=""))
            desc = _truncate(_strip_html(node.findtext("description", default="")))
            if not desc and link:
                desc = (
                    _article_summary(link)
                    or "Summary unavailable from source metadata."
                )
            if not desc:
                desc = "Summary unavailable from source metadata."
            pub = node.findtext("pubDate", default="")
            pub_dt = None
            try:
                pub_dt = email.utils.parsedate_to_datetime(pub)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=dt.timezone.utc)
            except Exception:
                pass
            candidates.append(
                (
                    pub_dt,
                    {"headline": title, "summary": desc, "link": link or "Unavailable"},
                )
            )

    seen: set[str] = set()
    selected: list[tuple[dt.datetime | None, dict[str, str]]] = []
    for pub_dt, row in sorted(
        candidates,
        key=lambda item: item[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    ):
        key = row["headline"].strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append((pub_dt, row))
        if len(selected) == 5:
            break

    rows = [row for _, row in selected]
    while len(rows) < 5:
        idx = len(rows) + 1
        rows.append(
            {
                "headline": f"Unavailable US/Global news item {idx}",
                "summary": "Summary unavailable from source metadata.",
                "link": "Unavailable",
            }
        )
    return rows


def fetch_weibo_ent_top5() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        page = _fetch_text(WEIBO_ENT_URL)
        pairs = re.findall(
            r'<a[^>]+href="(https://s\.weibo\.com/weibo\?q=[^"]+?Refer=top)"[^>]*>([^<]+)</a>',
            page,
            flags=re.I,
        )
        seen = set()
        for raw_link, raw_title in pairs:
            title = _strip_html(raw_title).replace("#", "").strip()
            link = html.unescape(raw_link)
            if not title or "查看详细" in title or title in seen:
                continue
            seen.add(title)
            entries.append(
                {
                    "headline": title,
                    "summary": f"该话题位于微博文娱热搜，围绕“{title}”引发讨论。",
                    "link": link,
                }
            )
            if len(entries) == 5:
                break
    except Exception:
        pass

    while len(entries) < 5:
        idx = len(entries) + 1
        entries.append(
            {
                "headline": f"Unavailable entertainment topic {idx}",
                "summary": "Summary unavailable from source metadata.",
                "link": "Unavailable",
            }
        )
    return entries


def compose_markdown(
    now_local: dt.datetime,
    quotes: list[dict[str, str]],
    techcrunch_items: list[dict[str, str]],
    hn_items: list[dict[str, str]],
    github_items: list[dict[str, str]],
    us_global_items: list[dict[str, str]],
    weibo_items: list[dict[str, str]],
) -> str:
    lines: list[str] = []
    lines.append(f"# Daily Briefing - {now_local.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("## Market Watch")
    for q in quotes:
        lines.append(
            f"- **{q['symbol']}**: {q['price']} {q['currency']} **{q['trend']}**"
        )
    lines.append("")

    sections = [
        ("## Top 10 TechCrunch", techcrunch_items),
        ("## Top 10 Hacker News", hn_items),
        ("## Top 10 GitHub Weekly Trending Projects", github_items),
        ("## Top 5 US/Global News", us_global_items),
        ("## 微博娛樂熱搜 Top 5", weibo_items),
    ]
    for heading, items in sections:
        lines.append(heading)
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. **{item['headline']}**: {item['summary']}")
            lines.append(f"   - Link: {item['link']}")
        lines.append("")
    return "\n".join(lines)


def _validate_output(text: str) -> None:
    for token in [
        "# Daily Briefing - ",
        "## Market Watch",
        "## Top 10 TechCrunch",
        "## Top 10 Hacker News",
        "## Top 10 GitHub Weekly Trending Projects",
        "## Top 5 US/Global News",
        "## 微博娛樂熱搜 Top 5",
    ]:
        if token not in text:
            raise RuntimeError(f"Output missing required section: {token}")


def run_self_check() -> int:
    try:
        current_hash = _current_skill_hash()
    except Exception as exc:
        print(f"self-check: failed to read SKILL.md: {exc}")
        return 2
    if current_hash != EMBEDDED_SKILL_SHA256:
        print("self-check: SKILL hash mismatch; full regeneration required")
        return 3

    sample = compose_markdown(
        now_local=dt.datetime.now(),
        quotes=[{"symbol": "MSFT", "price": "1", "currency": "USD", "trend": "▲"}],
        techcrunch_items=[{"headline": "h", "summary": "s", "link": "u"}] * 10,
        hn_items=[{"headline": "h", "summary": "s", "link": "u"}] * 10,
        github_items=[{"headline": "h", "summary": "s", "link": "u"}] * 10,
        us_global_items=[{"headline": "h", "summary": "s", "link": "u"}] * 5,
        weibo_items=[{"headline": "h", "summary": "s", "link": "u"}] * 5,
    )
    try:
        _validate_output(sample)
    except Exception as exc:
        print(f"self-check: output template invalid: {exc}")
        return 4

    print("self-check: ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily briefing markdown")
    parser.add_argument(
        "--self-check", action="store_true", help="Run script health check"
    )
    args = parser.parse_args()

    if args.self_check:
        return run_self_check()
    if _current_skill_hash() != EMBEDDED_SKILL_SHA256:
        raise RuntimeError("SKILL.md changed. Agent must fully regenerate this script.")

    root = _workspace_root()
    profile_path = root / "CLAUDE.md"
    if not profile_path.exists():
        profile_path = root / "AGENTS.md"
    output_path = root / "daily-briefing.md"

    tickers = read_tickers_from_profile(profile_path)
    quotes = fetch_market_quotes(tickers)
    techcrunch_items = fetch_techcrunch_top10()
    hn_items = fetch_hn_top10()
    github_items = fetch_github_weekly_top10()
    us_global_items = fetch_us_global_top5()
    weibo_items = fetch_weibo_ent_top5()

    markdown = compose_markdown(
        dt.datetime.now(),
        quotes,
        techcrunch_items,
        hn_items,
        github_items,
        us_global_items,
        weibo_items,
    )
    _validate_output(markdown)
    output_path.write_text(markdown, encoding="utf-8")

    try:
        subprocess.run(["open", "-a", "MarkText", str(output_path)], check=False)
    except Exception:
        pass

    print(f"Generated: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
