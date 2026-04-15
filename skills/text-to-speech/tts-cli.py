#!/usr/bin/env python3
"""tts-cli.py — macOS TTS command-line tool with background queue.

Wraps the macOS `say` command with smart voice selection and a background
queue for sequential, non-blocking playback.

Usage:
    python3 tts-cli.py [options] "text to speak"
    python3 tts-cli.py --force "urgent message"  # interrupt current playback
    python3 tts-cli.py --list-voices --lang zh --json
"""

import argparse
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR       = Path(__file__).parent
CONFIG_PATH      = SCRIPT_DIR / "config.json"
TMP              = Path(tempfile.gettempdir())

VOICE_CACHE_PATH = TMP / "opencode-tts-voices-cache-v1.json"
QUEUE_PATH       = TMP / "tts-queue.json"
DAEMON_PID_PATH  = TMP / "tts-daemon.pid"
SAY_PID_PATH     = TMP / "tts-say.pid"
DAEMON_LOG_PATH  = TMP / "tts-daemon.log"

VOICE_SCAN_DIRS = [
    Path("/System/Library/Speech/Voices"),
    Path("/Library/Speech/Voices"),
    Path.home() / "Library" / "Speech" / "Voices",
]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "defaultLang": "en-US",
    "defaultRate": 1.0,
    "preferPremiumOrEnhancedVoice": True,
    "localePreferenceByLanguage": {
        "zh": ["zh-TW", "zh-HK", "zh-CN"],
        "en": ["en-US", "en-GB", "en-AU", "en-IN"],
    },
    "voicePreferenceByLocale": {
        "zh-TW": ["Meijia"],
        "en-US": ["Samantha"],
    },
}

BASELINE_WPM = 175  # macOS `say` default speed; rate=1.0 maps to this value

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_locale(value: str) -> str:
    """Normalize locale to 'en' or 'en-US' form."""
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return ""
    parts = [p for p in raw.split("-") if p]
    if not parts:
        return ""
    lang = parts[0].lower()
    region = parts[1].upper() if len(parts) > 1 else ""
    return f"{lang}-{region}" if region else lang


def normalize_voice_name(value: str) -> str:
    """Lowercase + collapse whitespace for case-insensitive comparison."""
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def normalize_language_key(value: str) -> str:
    """Extract base language code from locale (en-US → en)."""
    locale = normalize_locale(value)
    return locale.split("-")[0] if locale else ""


def normalize_locale_list(values) -> list:
    """Normalize and deduplicate a list of locale strings."""
    if not isinstance(values, list):
        return []
    seen, result = set(), []
    for v in values:
        n = normalize_locale(v)
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    return result


def normalize_voice_list(values) -> list:
    """Normalize and deduplicate a list of voice name strings."""
    if not isinstance(values, list):
        return []
    seen, result = set(), []
    for v in values:
        original = str(v or "").strip()
        normalized = normalize_voice_name(original)
        if original and normalized and normalized not in seen:
            seen.add(normalized)
            result.append(original)
    return result


def normalize_preference_map(raw_map, key_fn, val_fn) -> dict:
    """Apply key and value normalizers to a preference mapping dict."""
    result = {}
    for k, v in (raw_map or {}).items():
        nk = key_fn(k)
        if nk:
            result[nk] = val_fn(v)
    return result


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load config.json and merge with hardcoded defaults."""
    user: dict = {}
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            print(
                f"Warning: could not parse {CONFIG_PATH}. Using defaults.",
                file=sys.stderr,
            )

    def pos_num(v, fallback: float) -> float:
        try:
            n = float(v)
            return n if n > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    def norm_bool(v, fallback: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return {"true": True, "false": False}.get(v.strip().lower(), fallback)
        return fallback

    d = DEFAULT_CONFIG
    locale_prefs = {
        **normalize_preference_map(
            d["localePreferenceByLanguage"], normalize_language_key, normalize_locale_list
        ),
        **normalize_preference_map(
            user.get("localePreferenceByLanguage", {}),
            normalize_language_key,
            normalize_locale_list,
        ),
    }
    voice_prefs = {
        **normalize_preference_map(
            d["voicePreferenceByLocale"], normalize_locale, normalize_voice_list
        ),
        **normalize_preference_map(
            user.get("voicePreferenceByLocale", {}), normalize_locale, normalize_voice_list
        ),
    }

    return {
        "defaultLang": normalize_locale(user.get("defaultLang", d["defaultLang"])),
        "defaultRate": pos_num(user.get("defaultRate"), d["defaultRate"]),
        "preferPremiumOrEnhancedVoice": norm_bool(
            user.get("preferPremiumOrEnhancedVoice"), d["preferPremiumOrEnhancedVoice"]
        ),
        "localePreferenceByLanguage": locale_prefs,
        "voicePreferenceByLocale": voice_prefs,
    }


# ---------------------------------------------------------------------------
# Voice cache and discovery
# ---------------------------------------------------------------------------


def get_macos_version() -> str:
    try:
        r = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True)
        return r.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def get_voice_dir_snapshot(dir_path: Path) -> dict:
    try:
        stat = dir_path.stat()
        if not dir_path.is_dir():
            return {"path": str(dir_path), "exists": False, "mtimeMs": 0, "entryCount": 0}
        count = sum(1 for _ in dir_path.iterdir())
        return {
            "path": str(dir_path),
            "exists": True,
            "mtimeMs": int(stat.st_mtime * 1000),
            "entryCount": count,
        }
    except OSError:
        return {"path": str(dir_path), "exists": False, "mtimeMs": 0, "entryCount": 0}


def build_voice_cache_key() -> str:
    return json.dumps(
        {
            "macOSVersion": get_macos_version(),
            "directories": [get_voice_dir_snapshot(d) for d in VOICE_SCAN_DIRS],
        }
    )


def read_voice_cache() -> Optional[dict]:
    try:
        if not VOICE_CACHE_PATH.exists():
            return None
        data = json.loads(VOICE_CACHE_PATH.read_text())
        if not isinstance(data.get("voices"), list):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def write_voice_cache(cache_key: str, voices: list) -> None:
    payload = json.dumps(
        {"createdAt": int(time.time() * 1000), "cacheKey": cache_key, "voices": voices}
    )
    tmp = VOICE_CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(payload)
        tmp.replace(VOICE_CACHE_PATH)  # atomic on POSIX
    except OSError:
        tmp.unlink(missing_ok=True)


_VOICE_LINE_RE = re.compile(r"^(.*\S)\s+([a-z]{2,3}_[A-Z0-9]{2,3})\s*$")


def read_mac_voices_from_system() -> list:
    """Query installed voices from the macOS `say -v ?` command."""
    r = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "Failed to list voices from say -v ?")
    voices = []
    for line in r.stdout.splitlines():
        left = line.split("#")[0].strip()
        m = _VOICE_LINE_RE.match(left)
        if m:
            voices.append({"name": m.group(1).strip(), "locale": normalize_locale(m.group(2))})
    return voices


def read_mac_voices(*, no_cache: bool = False, refresh_voices: bool = False) -> list:
    """Return installed voices, using cache when valid."""
    if no_cache:
        return read_mac_voices_from_system()
    cached = read_voice_cache()
    cache_key = build_voice_cache_key()
    if not refresh_voices and cached and cached.get("cacheKey") == cache_key:
        return cached["voices"]
    try:
        voices = read_mac_voices_from_system()
        write_voice_cache(cache_key, voices)
        return voices
    except RuntimeError:
        if cached and cached.get("voices"):
            print("Warning: failed to refresh voices. Using cached voices.", file=sys.stderr)
            return cached["voices"]
        raise


def build_installed_voice_index(voices: list) -> dict:
    """Build locale→voices and language→locales indexes from discovered voices."""
    locales_by_lang: dict = {}
    voices_by_locale: dict = {}
    for v in voices:
        locale = normalize_locale(v.get("locale", ""))
        name = str(v.get("name", "")).strip()
        if not locale or not name:
            continue
        lang = locale.split("-")[0]
        if locale not in locales_by_lang.setdefault(lang, []):
            locales_by_lang[lang].append(locale)
        if normalize_voice_name(name) not in [
            normalize_voice_name(x) for x in voices_by_locale.setdefault(locale, [])
        ]:
            voices_by_locale[locale].append(name)
    return {"localesByLanguage": locales_by_lang, "voicesByLocale": voices_by_locale}


def build_effective_config(config: dict, voices: list) -> dict:
    """Prune config preferences to only include voices/locales actually installed."""
    installed = build_installed_voice_index(voices)

    eff_locale_prefs = {}
    for lang, prefs in config["localePreferenceByLanguage"].items():
        installed_locales = installed["localesByLanguage"].get(lang, [])
        filtered = [l for l in prefs if normalize_locale(l) in installed_locales]
        if filtered:
            eff_locale_prefs[lang] = filtered

    eff_voice_prefs = {}
    for locale_key, prefs in config["voicePreferenceByLocale"].items():
        locale = normalize_locale(locale_key)
        installed_voices = installed["voicesByLocale"].get(locale, [])
        if not installed_voices:
            continue
        installed_normalized = {normalize_voice_name(n) for n in installed_voices}
        seen: set = set()
        filtered = []
        for name in prefs:
            n = normalize_voice_name(name)
            if n and n not in seen and n in installed_normalized:
                seen.add(n)
                filtered.append(name)
        if filtered:
            eff_voice_prefs[locale] = filtered

    return {**config, "localePreferenceByLanguage": eff_locale_prefs, "voicePreferenceByLocale": eff_voice_prefs}


# ---------------------------------------------------------------------------
# Voice selection
# ---------------------------------------------------------------------------

_TIER_RE = re.compile(r"\s+\((Premium|Enhanced)\)\s*$", re.IGNORECASE)


def get_voice_tier_rank(name: str) -> int:
    """Return 2=Premium, 1=Enhanced, 0=regular."""
    m = _TIER_RE.search(str(name or ""))
    if not m:
        return 0
    return 2 if m.group(1).lower() == "premium" else 1


def get_voice_base_name(name: str) -> str:
    """Strip Premium/Enhanced tier suffix from a voice name."""
    return _TIER_RE.sub("", str(name or "")).strip()


def has_explicit_tier_suffix(name: str) -> bool:
    return bool(_TIER_RE.search(str(name or "")))


def tier_decision_from_rank(rank: int) -> str:
    return {2: "upgraded_premium", 1: "upgraded_enhanced"}.get(rank, "regular")


def pick_best_tier_voice(voices: list) -> dict:
    """Return the highest-tier voice from a list."""
    if not voices:
        return {"match": None, "tierDecision": "none"}
    best, best_rank = None, -1
    for v in voices:
        rank = get_voice_tier_rank(v["name"])
        if rank > best_rank:
            best_rank, best = rank, v
    return {"match": best, "tierDecision": tier_decision_from_rank(best_rank)}


def find_best_voice_by_base_name(normalized_base: str, voices: list) -> dict:
    """Find highest-tier voice whose base name matches normalized_base."""
    candidates = [
        v for v in voices
        if normalize_voice_name(get_voice_base_name(v["name"])) == normalized_base
    ]
    return pick_best_tier_voice(candidates)


def resolve_voice_name(voice_name: str, voices: list, prefer_premium: bool) -> dict:
    """Resolve a requested voice name to an installed canonical voice."""
    requested = normalize_voice_name(voice_name)
    if not requested:
        return {"match": None, "requestTagged": False, "resolutionPath": "none", "tierDecision": "none"}

    request_tagged = has_explicit_tier_suffix(voice_name)
    exact = next((v for v in voices if normalize_voice_name(v["name"]) == requested), None)
    requested_base = normalize_voice_name(get_voice_base_name(voice_name))

    if exact:
        if not request_tagged and prefer_premium:
            best = find_best_voice_by_base_name(requested_base, voices)
            if best["match"] and normalize_voice_name(best["match"]["name"]) != normalize_voice_name(exact["name"]):
                return {"match": best["match"], "requestTagged": request_tagged,
                        "resolutionPath": "exact", "tierDecision": best["tierDecision"]}
        return {"match": exact, "requestTagged": request_tagged,
                "resolutionPath": "exact", "tierDecision": "kept_exact"}

    fallback = find_best_voice_by_base_name(requested_base, voices)
    if not fallback["match"]:
        return {"match": None, "requestTagged": request_tagged,
                "resolutionPath": "none", "tierDecision": "none"}
    return {"match": fallback["match"], "requestTagged": request_tagged,
            "resolutionPath": "base_fallback", "tierDecision": fallback["tierDecision"]}


def locales_for_language(voices: list, language: str) -> list:
    """Return distinct locales for a base language from the voice list."""
    seen: set = set()
    result = []
    for v in voices:
        loc = v["locale"]
        if loc.split("-")[0] == language and loc not in seen:
            seen.add(loc)
            result.append(loc)
    return result


def locale_candidates_for_lang(lang_tag: str, voices: list, config: dict) -> list:
    """Build ordered locale candidates for a requested language/locale tag."""
    normalized = normalize_locale(lang_tag)
    if not normalized:
        return []
    seen: set = set()
    candidates = []

    def add(loc: str):
        if loc and loc not in seen:
            seen.add(loc)
            candidates.append(loc)

    has_region = "-" in normalized
    language = normalized.split("-")[0]
    if has_region:
        add(normalized)
    configured = config["localePreferenceByLanguage"].get(language, [])
    if configured:
        for loc in configured:
            add(normalize_locale(loc))
        return candidates
    for loc in locales_for_language(voices, language):
        add(loc)
    return candidates


def select_voice_for_locale(locale: str, voices: list, config: dict) -> dict:
    """Select the best voice for a given locale, honoring config preferences."""
    locale_voices = [v for v in voices if v["locale"] == locale]
    if not locale_voices:
        return {"voice": None}

    prefs = config["voicePreferenceByLocale"].get(locale, [])
    for pref_name in prefs:
        res = resolve_voice_name(pref_name, locale_voices, config["preferPremiumOrEnhancedVoice"])
        if res["match"]:
            return {
                "voice": res["match"]["name"],
                "usedVoicePreference": True,
                "voiceRequested": pref_name,
                "voiceRequestTagged": res["requestTagged"],
                "voiceResolutionPath": res["resolutionPath"],
                "voiceTierDecision": res["tierDecision"],
            }

    if config["preferPremiumOrEnhancedVoice"]:
        best = pick_best_tier_voice(locale_voices)
        return {
            "voice": best["match"]["name"] if best["match"] else None,
            "usedVoicePreference": False,
            "voiceRequested": "",
            "voiceRequestTagged": False,
            "voiceResolutionPath": "locale_best",
            "voiceTierDecision": best["tierDecision"],
        }

    first = locale_voices[0]
    return {
        "voice": first["name"],
        "usedVoicePreference": False,
        "voiceRequested": "",
        "voiceRequestTagged": False,
        "voiceResolutionPath": "locale_first",
        "voiceTierDecision": tier_decision_from_rank(get_voice_tier_rank(first["name"])),
    }


def build_invalid_voice_message(voice_name: str, lang_tag: str, default_lang: str) -> str:
    effective = normalize_locale(lang_tag) or default_lang or "en-US"
    return (
        f'Invalid voice: "{voice_name}". '
        f"Use --lang <locale> to auto-resolve an installed voice. "
        f'Example: --lang {effective} "Hello world"'
    )


def resolve_selection(args, voices: list, config: dict) -> dict:
    """Resolve final voice/locale/language selection with full debug metadata."""
    default_lang = normalize_locale(config["defaultLang"])
    requested_lang = normalize_locale(args.lang or "")
    effective_lang = requested_lang or default_lang

    if args.voice:
        res = resolve_voice_name(args.voice, voices, config["preferPremiumOrEnhancedVoice"])
        if not res["match"]:
            raise ValueError(build_invalid_voice_message(args.voice, requested_lang, default_lang))
        return {
            "requestedLang": requested_lang or default_lang,
            "effectiveLang": effective_lang,
            "locale": None,
            "voice": res["match"]["name"],
            "fallbackToDefaultLang": effective_lang == default_lang and requested_lang != default_lang,
            "usedVoicePreference": False,
            "explicitVoice": True,
            "voiceRequested": args.voice,
            "voiceRequestTagged": res["requestTagged"],
            "voiceResolutionPath": res["resolutionPath"],
            "voiceTierDecision": res["tierDecision"],
            "triedLocales": [],
        }

    tried: list = []

    def try_resolve(lang: str) -> Optional[dict]:
        candidates = locale_candidates_for_lang(lang, voices, config)
        for locale in candidates:
            if locale not in tried:
                tried.append(locale)
            sel = select_voice_for_locale(locale, voices, config)
            if sel.get("voice"):
                return {**sel, "locale": locale}
        return None

    resolved = try_resolve(effective_lang)
    fallback = False
    if not resolved and effective_lang != default_lang:
        resolved = try_resolve(default_lang)
        fallback = True

    return {
        "requestedLang": requested_lang or default_lang,
        "effectiveLang": effective_lang,
        "locale": resolved.get("locale") if resolved else None,
        "voice": resolved.get("voice") if resolved else None,
        "fallbackToDefaultLang": fallback,
        "usedVoicePreference": resolved.get("usedVoicePreference", False) if resolved else False,
        "explicitVoice": False,
        "voiceRequested": resolved.get("voiceRequested", "") if resolved else "",
        "voiceRequestTagged": resolved.get("voiceRequestTagged", False) if resolved else False,
        "voiceResolutionPath": resolved.get("voiceResolutionPath", "none") if resolved else "none",
        "voiceTierDecision": resolved.get("voiceTierDecision", "none") if resolved else "none",
        "triedLocales": tried,
    }


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------


@contextmanager
def _queue_lock():
    """Exclusive file lock for safe concurrent queue access."""
    lock_path = str(QUEUE_PATH) + ".lock"
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_queue() -> list:
    """Read queue file, returning an empty list on any error."""
    try:
        if QUEUE_PATH.exists():
            return json.loads(QUEUE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return []


def write_queue(jobs: list) -> None:
    """Atomically replace queue file contents."""
    tmp = QUEUE_PATH.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(jobs))
    tmp.replace(QUEUE_PATH)


def enqueue_job(job: dict) -> None:
    """Append a job to the queue (thread-safe)."""
    with _queue_lock():
        jobs = read_queue()
        jobs.append(job)
        write_queue(jobs)


def pop_queue() -> Optional[dict]:
    """Remove and return the first job from the queue, or None if empty."""
    with _queue_lock():
        jobs = read_queue()
        if not jobs:
            return None
        job, rest = jobs[0], jobs[1:]
        write_queue(rest)
        return job


def is_daemon_running() -> bool:
    """Return True if a daemon process is alive with the recorded PID."""
    try:
        if not DAEMON_PID_PATH.exists():
            return False
        pid = int(DAEMON_PID_PATH.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        DAEMON_PID_PATH.unlink(missing_ok=True)
        return False


def start_daemon() -> None:
    """Spawn a detached background daemon to drain the queue."""
    log = open(DAEMON_LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--daemon"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
    )
    log.close()


def wake_daemon() -> None:
    """Send SIGUSR1 to the running daemon so it processes new queue items."""
    try:
        if DAEMON_PID_PATH.exists():
            pid = int(DAEMON_PID_PATH.read_text().strip())
            os.kill(pid, signal.SIGUSR1)
    except (ValueError, ProcessLookupError, OSError):
        pass


# ---------------------------------------------------------------------------
# Say execution
# ---------------------------------------------------------------------------


def convert_rate_for_mac(rate: float) -> int:
    """Convert a rate multiplier to macOS say WPM value (baseline=175 WPM)."""
    return max(1, math.ceil(BASELINE_WPM * rate))


def run_say(job: dict) -> None:
    """Execute the macOS `say` command for a job, tracking its PID."""
    say_args = ["say"]
    if job.get("voice"):
        say_args += ["-v", job["voice"]]
    if job.get("rate"):
        say_args += ["-r", str(convert_rate_for_mac(job["rate"]))]

    if job.get("file_path"):
        say_args += ["-f", job["file_path"]]
        proc = subprocess.Popen(say_args)
    else:
        say_args.append(str(job.get("text") or ""))
        proc = subprocess.Popen(say_args)

    SAY_PID_PATH.write_text(str(proc.pid))
    try:
        proc.wait()
    finally:
        SAY_PID_PATH.unlink(missing_ok=True)
        # Clean up temporary stdin-capture files
        if job.get("owns_file") and job.get("file_path"):
            try:
                Path(job["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

_daemon_wakeup = False


def run_daemon() -> None:
    """Daemon entry point: drain queue sequentially then exit."""
    DAEMON_PID_PATH.write_text(str(os.getpid()))

    def on_sigusr1(sig, frame):
        global _daemon_wakeup
        _daemon_wakeup = True

    def on_sigterm(sig, frame):
        DAEMON_PID_PATH.unlink(missing_ok=True)
        SAY_PID_PATH.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGUSR1, on_sigusr1)
    signal.signal(signal.SIGTERM, on_sigterm)

    try:
        while True:
            job = pop_queue()
            if job is None:
                # Wait up to 5 s for a new item via SIGUSR1 (100 ms polls)
                global _daemon_wakeup
                _daemon_wakeup = False
                for _ in range(50):
                    if _daemon_wakeup:
                        break
                    time.sleep(0.1)
                job = pop_queue()
                if job is None:
                    break  # queue still empty → exit daemon
            run_say(job)
    finally:
        DAEMON_PID_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Force play
# ---------------------------------------------------------------------------


def force_play(job: dict) -> None:
    """Kill current playback and queue, then play this job immediately."""
    # Kill daemon process
    if DAEMON_PID_PATH.exists():
        try:
            pid = int(DAEMON_PID_PATH.read_text().strip())
            os.kill(pid, signal.SIGKILL)
        except (ValueError, ProcessLookupError, OSError):
            pass
        DAEMON_PID_PATH.unlink(missing_ok=True)

    # Kill currently speaking say process
    if SAY_PID_PATH.exists():
        try:
            pid = int(SAY_PID_PATH.read_text().strip())
            os.kill(pid, signal.SIGKILL)
        except (ValueError, ProcessLookupError, OSError):
            pass
        SAY_PID_PATH.unlink(missing_ok=True)

    # Replace queue with a single job and launch a fresh daemon
    write_queue([job])
    start_daemon()


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tts-cli.py",
        description="macOS TTS command-line tool with background queue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python3 tts-cli.py --voice Yue "你好！"\n'
            '  python3 tts-cli.py --lang zh-TW "你好！"\n'
            "  python3 tts-cli.py --file /tmp/speech.txt\n"
            '  python3 tts-cli.py --force "urgent message"\n'
            "  python3 tts-cli.py --list-voices --lang zh --json"
        ),
    )
    p.add_argument("words", nargs="*", metavar="TEXT", help="Text to speak")
    p.add_argument("-t", "--text", dest="text_opt", metavar="TEXT", help="Text input (option form)")
    p.add_argument("-f", "--file", metavar="PATH|-", help="Read from file or stdin (-)")
    p.add_argument("-v", "--voice", metavar="NAME", help="Explicit voice name")
    p.add_argument("-l", "--lang", metavar="TAG", help="Language or locale (en-US, zh-TW, zh)")
    p.add_argument("-r", "--rate", type=float, metavar="N", help="Speed multiplier (1.0 = normal)")
    p.add_argument("-F", "--force", action="store_true",
                   help="Stop current playback, clear queue, play immediately")
    p.add_argument("--list-voices", action="store_true", help="List installed voices")
    p.add_argument("--no-cache", action="store_true", help="Skip voice cache read/write")
    p.add_argument("--refresh-voices", action="store_true", help="Force refresh voice cache")
    p.add_argument("--dry-run", action="store_true", help="Print resolved voice/lang without speaking")
    p.add_argument("--json", action="store_true", help="JSON output (use with --list-voices)")
    p.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)  # internal
    return p


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------


def resolve_input(args) -> dict:
    """Determine text source from args. Stdin is eagerly read and temp-filed."""
    text = args.text_opt or (" ".join(args.words) if args.words else "")

    if args.file:
        if args.file == "-":
            # Read stdin now before daemon spawns (daemon has no stdin)
            data = sys.stdin.buffer.read()
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=".txt", prefix="tts_stdin_"
            )
            tmp.write(data)
            tmp.close()
            return {"mode": "file", "file_path": tmp.name, "owns_file": True, "text": None}
        return {"mode": "file", "file_path": args.file, "owns_file": False, "text": None}

    if text:
        return {"mode": "text", "text": text, "file_path": None, "owns_file": False}

    return {"mode": "none", "text": None, "file_path": None, "owns_file": False}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def filter_voices_by_lang(voices: list, lang_tag: str) -> list:
    requested = normalize_locale(lang_tag)
    if not requested:
        return voices
    has_region = "-" in requested
    language = requested.split("-")[0]
    return [
        v for v in voices
        if (v["locale"] == requested if has_region else v["locale"].split("-")[0] == language)
    ]


def print_voices(voices: list, json_output: bool) -> None:
    if json_output:
        print(json.dumps(voices, indent=2))
    else:
        for v in voices:
            print(f"{v['name']}\t{v['locale']}")


def print_dry_run(selection: dict, config: dict, rate: float) -> None:
    default_lang = normalize_locale(config["defaultLang"])
    fields = [
        ("langRequested",              selection["requestedLang"]),
        ("langEffective",              selection["effectiveLang"]),
        ("langDefault",                default_lang),
        ("rateDefault",                config["defaultRate"]),
        ("rateEffective",              rate),
        ("preferPremiumOrEnhancedVoice", "yes" if config["preferPremiumOrEnhancedVoice"] else "no"),
        ("voiceRequested",             selection.get("voiceRequested") or "none"),
        ("voiceRequestTagged",         "yes" if selection.get("voiceRequestTagged") else "no"),
        ("voiceResolutionPath",        selection.get("voiceResolutionPath") or "none"),
        ("voiceTierDecision",          selection.get("voiceTierDecision") or "none"),
        ("localeResolved",             selection.get("locale") or "system-default"),
        ("voiceResolved",              selection.get("voice") or "system-default"),
        ("voiceExplicit",              "yes" if selection.get("explicitVoice") else "no"),
        ("voiceFromPreference",        "yes" if selection.get("usedVoicePreference") else "no"),
        ("langFallbackToDefault",      "yes" if selection.get("fallbackToDefaultLang") else "no"),
    ]
    for k, v in fields:
        print(f"{k}={v}")
    if selection.get("triedLocales"):
        print(f"triedLocales={','.join(selection['triedLocales'])}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if sys.platform != "darwin":
        print("This skill currently supports macOS only.", file=sys.stderr)
        sys.exit(1)

    parser = build_parser()
    args = parser.parse_args()

    # ── Internal daemon mode ────────────────────────────────────────────────
    if args.daemon:
        run_daemon()
        return

    # ── Validation ──────────────────────────────────────────────────────────
    if args.json and not args.list_voices:
        parser.error("--json can only be used with --list-voices")

    # ── Config + voices ─────────────────────────────────────────────────────
    config = load_config()
    effective_rate = args.rate if args.rate is not None else config["defaultRate"]
    if not (isinstance(effective_rate, (int, float)) and effective_rate > 0):
        print("Invalid rate. Use a positive number such as 1 or 0.9.", file=sys.stderr)
        sys.exit(1)

    try:
        voices = read_mac_voices(no_cache=args.no_cache, refresh_voices=args.refresh_voices)
    except RuntimeError as e:
        print(f"TTS voice discovery error: {e}", file=sys.stderr)
        sys.exit(1)

    # ── List voices ─────────────────────────────────────────────────────────
    if args.list_voices:
        filtered = filter_voices_by_lang(voices, args.lang or "") if args.lang else voices
        print_voices(filtered, args.json)
        return

    # ── Resolve input ───────────────────────────────────────────────────────
    inp = resolve_input(args)
    if inp["mode"] == "none" and not args.dry_run:
        parser.print_help()
        sys.exit(1)

    # ── Resolve voice/lang ──────────────────────────────────────────────────
    effective_config = build_effective_config(config, voices)
    try:
        selection = resolve_selection(args, voices, effective_config)
    except ValueError as e:
        print(f"TTS voice selection error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print_dry_run(selection, config, effective_rate)
        return

    # ── Build job ───────────────────────────────────────────────────────────
    job = {
        "id":        f"{int(time.time() * 1000)}-{os.getpid()}",
        "text":      inp["text"],
        "file_path": inp["file_path"],
        "owns_file": inp["owns_file"],
        "voice":     selection.get("voice"),
        "rate":      effective_rate,
    }

    # ── Enqueue or force-play ───────────────────────────────────────────────
    if args.force:
        force_play(job)
    else:
        enqueue_job(job)
        if is_daemon_running():
            wake_daemon()
        else:
            start_daemon()
    # Client exits immediately; daemon handles playback in the background.


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"TTS error: {e}", file=sys.stderr)
        sys.exit(1)
