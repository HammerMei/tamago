---
name: text-to-speech
description: >
  Use this skill — not the raw say command — whenever speech output is needed on macOS. This skill
  reads from the user's voice preferences and config, auto-selects Premium voices, and safely
  handles long text. Trigger for any of: reading text aloud (read aloud, 朗讀, 唸出來, TTS, speak),
  playing back any assistant response as audio, listing installed voices, narrating daily briefings,
  recipe walkthroughs, news summaries, or any response from an assistant persona that always speaks.
  Supports explicit voice names (Yue, Meijia, Samantha), locale-based selection (zh-TW, en-US),
  speaking rate control, and file input for long content. Do not trigger for: speech-to-text
  transcription, AI voiceover MP3 production, or TTS on non-macOS platforms.
---

> **Agent integration**: Agent files declare only `Voice` and `Rate` config. This skill file is the single source of truth for the invocation command and all flag details.

Run TTS via:
```bash
python3 .claude/skills/text-to-speech/tts-cli.py --voice <name> --rate <rate> "text to speak"
```

> ⚠️ **For long text**, pipe via stdin using `--file -` with a heredoc — avoids shell length limits and does **not** create any temp files (no approval prompts).
> **Important:** the closing `EOF` delimiter **must be on its own line**.
> Do **not** place `EOF` at the end of the final content line, or the shell may pass `EOF`
> through as part of the text and TTS may literally speak “EOF”.
> If using a heredoc, always put the closing `EOF` on a new line by itself.
> 否則 `EOF` 可能被當成正文內容送進 TTS。
> ```bash
> python3 .claude/skills/text-to-speech/tts-cli.py --voice Meijia --file - <<'EOF'
> long text goes here...
> more lines...
> EOF
> ```

> ℹ️ **Non-blocking by default** — the CLI enqueues the job and returns immediately. Audio plays in
> the background via a persistent daemon. Use `--force` to interrupt current playback.

## Most common usage patterns

```bash
# Speak with explicit voice
python3 .claude/skills/text-to-speech/tts-cli.py --voice Yue "你好！"

# Speak by language/locale (auto-selects best installed voice)
python3 .claude/skills/text-to-speech/tts-cli.py --lang zh-TW "你好！"
python3 .claude/skills/text-to-speech/tts-cli.py --lang en-US "Hello world"

# Speak long text via stdin (preferred — no temp file, no approval prompt)
python3 .claude/skills/text-to-speech/tts-cli.py --voice Samantha --file - <<'EOF'
Long text goes here...
EOF

# Wrong: closing EOF is appended to content, so TTS may speak "EOF"
python3 .claude/skills/text-to-speech/tts-cli.py --voice Samantha --file - <<'EOF'
Long text goes here...EOF
EOF

# Interrupt current playback and speak immediately
python3 .claude/skills/text-to-speech/tts-cli.py --force --voice Yue "緊急訊息！"

# List voices for a language
python3 .claude/skills/text-to-speech/tts-cli.py --list-voices --lang zh --json
```

## Background queue behavior

All invocations are **non-blocking** by default:

1. The CLI resolves the voice, writes a job to `/tmp/tts-queue.json`, then **exits immediately**.
2. A background daemon (`tts-cli.py --daemon`) is auto-started if not already running.
3. The daemon processes jobs **sequentially** — each message plays fully before the next starts.
4. When the queue is empty, the daemon exits automatically.
5. If a daemon is already running, it is woken via **SIGUSR1** (< 1 ms latency) to pick up the new job.

### `--force` flag

```bash
python3 .claude/skills/text-to-speech/tts-cli.py --force "urgent override"
```

`--force` / `-F`:
- Sends **SIGKILL** to the running daemon and the active `say` process
- Clears the entire pending queue
- Enqueues the new job and starts a fresh daemon

Use for urgent interruptions, e.g. stopping a long briefing mid-way.

### Queue and PID file locations

| File | Purpose |
|------|---------|
| `/tmp/tts-queue.json` | Pending job list |
| `/tmp/tts-daemon.pid` | Running daemon PID |
| `/tmp/tts-say.pid` | Active `say` process PID |
| `/tmp/tts-daemon.log` | Daemon stdout/stderr log |

To inspect or clear the queue manually:
```bash
cat /tmp/tts-queue.json        # view pending jobs
echo "[]" > /tmp/tts-queue.json  # clear queue
kill $(cat /tmp/tts-daemon.pid)  # stop daemon
```

## Voice selection logic

1. If `--voice` is given → use that voice (validates against installed voices)
2. If `--lang` is given → pick best installed voice for that locale via config preferences
3. Otherwise → fall back to `defaultLang` in config

When `preferPremiumOrEnhancedVoice=true` (default), the CLI auto-upgrades to the Premium or Enhanced tier of the same voice if available.

## Options

| Option | Short | Description |
|--------|-------|-------------|
| `--voice <name>` | `-v` | Explicit voice name |
| `--lang <tag>` | `-l` | Locale/language (`en-US`, `zh-TW`, `en`) |
| `--rate <n>` | `-r` | Speed multiplier (default from config, 1 = normal) |
| `--file <path\|->` | `-f` | Read from file or stdin (`-`) |
| `--force` | `-F` | Kill current playback, clear queue, play immediately |
| `--list-voices` | | List installed voices |
| `--json` | | Machine-readable output (use with `--list-voices`) |
| `--dry-run` | | Print resolved voice/lang without speaking |
| `--refresh-voices` | | Force refresh voice cache from system |

## Config

Path: `.claude/skills/text-to-speech/config.json` (or `.claude/skills/text-to-speech/config.json`)

| Key | Description |
|-----|-------------|
| `defaultLang` | Fallback language/locale |
| `defaultRate` | Default speaking rate multiplier |
| `preferPremiumOrEnhancedVoice` | Auto-upgrade to Premium/Enhanced tier (default: `true`) |
| `localePreferenceByLanguage` | Ordered locale list per language (e.g. `zh` → `["zh-TW", "zh-HK"]`) |
| `voicePreferenceByLocale` | Preferred voice names per locale (e.g. `zh-TW` → `["Meijia"]`) |

## Text preprocessing before speaking

Some text needs cleanup before passing to TTS to ensure natural-sounding speech:

- **acronym or abbreviations** — spell out as individual letters: `yyds` → `Y-Y-D-S`, `wtf` → `W-T-F`
- **Mixed-script text** — no special handling needed; the voice engine handles language switching automatically
- **Markdown formatting** — strip markdown syntax before speaking. Examples:
  - `**老妹** 說` → `老妹 說`
  - `# 標題` → `標題`
  - `` `code snippet` `` → `code snippet`
  - `[link text](url)` → `link text`
  - `- bullet item` → `bullet item`
  - `| table | cell |` → skip table rows entirely, or read header + values only

## Rate guidance by context

Use `--rate` to adjust speaking speed for different content types:

| Context | Suggested rate | Reason |
|---------|---------------|--------|
| Casual conversation / chat | `1.0` | Natural pace |
| News / briefing summary | `1.1` | Slightly faster, keeps attention |
| Recipe steps / instructions | `0.9` | Slower for clarity while cooking |
| Long articles / reading aloud | `0.95` | Comfortable for sustained listening |

> Default rate is read from `config.json → defaultRate`. Override per-call with `--rate <n>`.

## Language detection fallback

If voice or language resolution fails (e.g. unknown voice name, locale not installed):

1. The CLI exits with a non-zero code and prints an error to stderr
2. **Do NOT silently retry** with a random voice — tell the user what failed
3. Suggested fallback: use `--lang en-US` (English) or `--lang zh-TW` (Traditional Chinese) which are the most commonly installed locales
4. Run `--list-voices` to help the user see what voices are available on their system

## Running tests

```bash
# Run all unit tests
python3 -m pytest .claude/skills/text-to-speech/test_tts.py -v

# Or without pytest
python3 .claude/skills/text-to-speech/test_tts.py
```

Tests cover: locale normalization, voice tier ranking, voice resolution, locale selection,
config loading, queue read/write/pop, daemon PID detection, force-play, voice cache,
voice discovery parsing, effective config pruning, and preference list normalization.

## Error handling

If the command exits with a non-zero code, tell the user explicitly — do not silently skip TTS. Example: "TTS failed: [error message]. Please check that Python 3 is installed and the voice is available."

## Notes

- macOS only — script exits with error on other platforms
- Requires Python 3.9+ (uses `Path.unlink(missing_ok=True)`, `|` union type hints)
- `--lang zh` matches all `zh-*` locales; `--lang zh-TW` matches only `zh-TW`
- Voice names are case-insensitive and matched by base name (e.g. `"Yue"` matches `"Yue (Premium)"`)
- Voice list is cached in `/tmp` and auto-invalidated when system voices change
- Daemon log is at `/tmp/tts-daemon.log` — check here if audio stops unexpectedly
