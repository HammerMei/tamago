# text-to-speech skill

macOS text-to-speech helper built on `say`, focused on playback-only usage.

## Requirements

- macOS
- Node.js

## Quick start

```bash
node .opencode/skills/text-to-speech/tts-cli.js --lang en-US "Hello world"
```

## Usage

```bash
node .opencode/skills/text-to-speech/tts-cli.js [options] "text"
```

### Input options

- `--text, -t <text>` explicit inline text
- positional text (no `--text` needed)
- `--file, -f <path|->` text file or stdin (`-`)

Examples:

```bash
node .opencode/skills/text-to-speech/tts-cli.js -t "Read this"
node .opencode/skills/text-to-speech/tts-cli.js "Positional text input"
node .opencode/skills/text-to-speech/tts-cli.js --file notes.txt
pbpaste | node .opencode/skills/text-to-speech/tts-cli.js --file -
```

### Voice and language

- `--lang, -l <tag>` language/locale, for example `en`, `en-US`, `zh-TW`
- `--voice, -v <name>` explicit voice override
- `--rate, -r <n>` positive rate multiplier (default from config)

Resolution flow when `--voice` is not set:

1. Resolve requested language (`--lang` or `defaultLang`)
2. Build locale candidates from config and installed voices
3. Prefer configured voice names per locale when present
4. When `preferPremiumOrEnhancedVoice` is `true`, untagged names (for example `Samantha`) prefer `(Premium)` then `(Enhanced)` then regular for the same base name
5. Fallback to `defaultLang` when requested locale/language is unavailable

Behavior note:

- `--help` skips voice discovery
- Explicit `--voice` still performs voice discovery/validation against installed voices

### Voice listing

- `--list-voices` list installed voices
- `--list-voices --lang <tag>` filter list by language/locale
- `--json` output machine-readable JSON (only valid with `--list-voices`)

JSON schema (`--list-voices --json`):

- Array of objects with fields:
  - `name` (string): installed voice name
  - `locale` (string): normalized locale (for example `en-US`)

Filter behavior:

- `--lang en` => all `en-*` locales
- `--lang en-US` => exact `en-US`

Examples:

```bash
node .opencode/skills/text-to-speech/tts-cli.js --list-voices
node .opencode/skills/text-to-speech/tts-cli.js --list-voices --lang en
node .opencode/skills/text-to-speech/tts-cli.js --list-voices --lang en-US --json
node .opencode/skills/text-to-speech/tts-cli.js --list-voices --lang en --json
```

### Dry run

Use `--dry-run` to inspect resolved language, locale, voice, and rate without playback.

```bash
node .opencode/skills/text-to-speech/tts-cli.js --lang zh-TW --dry-run
```

## Config

Path: `.opencode/skills/text-to-speech/config.json`

Supported keys:

- `defaultLang`
- `defaultRate`
- `preferPremiumOrEnhancedVoice`
- `localePreferenceByLanguage`
- `voicePreferenceByLocale`

Normalization behavior:

- Language-map keys normalize to base language (`EN`, `en`, `en_US` => `en`)
- Locale-map keys normalize to locale format (`en_us` => `en-US`)
- Locale and voice arrays are de-duplicated
- `preferPremiumOrEnhancedVoice` controls whether untagged names prefer tiered variants for the same base name: `(Premium)` -> `(Enhanced)` -> regular

Tier matching behavior:

- If an exact match exists:
  - `preferPremiumOrEnhancedVoice=true` and untagged name (for example `Samantha`) picks best same-base tier: `(Premium)` -> `(Enhanced)` -> regular
  - `preferPremiumOrEnhancedVoice=false` uses the exact match as-is
- If exact match does not exist, both modes attempt forgiving same-base fallback and pick best tier: `(Premium)` -> `(Enhanced)` -> regular
- If no same-base match exists, voice resolution fails with the existing invalid-voice error

## Cache controls

- `--refresh-voices` refresh voice inventory from system and rewrite cache
- `--no-cache` bypass cache read/write

## Dry-run debug fields

- `voiceRequested`: original voice request used for matching (or `none`)
- `voiceRequestTagged`: whether request contains `(Premium)` or `(Enhanced)`
- `voiceResolutionPath`: `exact`, `base_fallback`, `locale_best`, `locale_first`, or `none`
- `voiceTierDecision`: `kept_exact`, `upgraded_premium`, `upgraded_enhanced`, `regular`, or `none`

## Troubleshooting

- `Argument error: --json can only be used with --list-voices`
  - Add `--list-voices` or remove `--json`
- `This skill currently supports macOS only.`
  - Run on macOS
- Voice list/read failure
  - Retry with `--refresh-voices` or `--no-cache`
