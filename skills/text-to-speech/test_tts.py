#!/usr/bin/env python3
"""test_tts.py — Unit and integration tests for tts-cli.py.

Run with:
    python3 -m pytest test_tts.py -v
    # or without pytest:
    python3 test_tts.py
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make tts-cli importable from the same directory
sys.path.insert(0, str(Path(__file__).parent))
import tts_cli as tts  # tts-cli.py imported as tts_cli (hyphen → underscore via symlink or rename)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_voice(name: str, locale: str) -> dict:
    return {"name": name, "locale": locale}


def make_voices(*pairs) -> list:
    """make_voices(('Samantha', 'en-US'), ('Meijia', 'zh-TW'), ...)"""
    return [make_voice(name, locale) for name, locale in pairs]


# ---------------------------------------------------------------------------
# 1. Normalization helpers
# ---------------------------------------------------------------------------

class TestNormalizeLocale(unittest.TestCase):
    def test_plain_language(self):
        self.assertEqual(tts.normalize_locale("en"), "en")

    def test_language_region(self):
        self.assertEqual(tts.normalize_locale("en-US"), "en-US")

    def test_underscore_separator(self):
        self.assertEqual(tts.normalize_locale("zh_TW"), "zh-TW")

    def test_uppercase_input(self):
        self.assertEqual(tts.normalize_locale("ZH-tw"), "zh-TW")

    def test_empty_string(self):
        self.assertEqual(tts.normalize_locale(""), "")

    def test_none_value(self):
        self.assertEqual(tts.normalize_locale(None), "")

    def test_strips_whitespace(self):
        self.assertEqual(tts.normalize_locale("  en-US  "), "en-US")

    def test_three_letter_language(self):
        self.assertEqual(tts.normalize_locale("yue-HK"), "yue-HK")


class TestNormalizeVoiceName(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(tts.normalize_voice_name("Samantha"), "samantha")

    def test_collapses_spaces(self):
        self.assertEqual(tts.normalize_voice_name("Yue  (Premium)"), "yue (premium)")

    def test_strips_edges(self):
        self.assertEqual(tts.normalize_voice_name("  Meijia  "), "meijia")

    def test_none_returns_empty(self):
        self.assertEqual(tts.normalize_voice_name(None), "")


class TestNormalizeLanguageKey(unittest.TestCase):
    def test_locale_to_base(self):
        self.assertEqual(tts.normalize_language_key("en-US"), "en")

    def test_plain_already_base(self):
        self.assertEqual(tts.normalize_language_key("zh"), "zh")

    def test_empty(self):
        self.assertEqual(tts.normalize_language_key(""), "")


# ---------------------------------------------------------------------------
# 2. Voice tier helpers
# ---------------------------------------------------------------------------

class TestGetVoiceTierRank(unittest.TestCase):
    def test_premium_rank(self):
        self.assertEqual(tts.get_voice_tier_rank("Samantha (Premium)"), 2)

    def test_enhanced_rank(self):
        self.assertEqual(tts.get_voice_tier_rank("Samantha (Enhanced)"), 1)

    def test_regular_rank(self):
        self.assertEqual(tts.get_voice_tier_rank("Samantha"), 0)

    def test_case_insensitive(self):
        self.assertEqual(tts.get_voice_tier_rank("Yue (PREMIUM)"), 2)

    def test_empty(self):
        self.assertEqual(tts.get_voice_tier_rank(""), 0)


class TestGetVoiceBaseName(unittest.TestCase):
    def test_strips_premium(self):
        self.assertEqual(tts.get_voice_base_name("Samantha (Premium)"), "Samantha")

    def test_strips_enhanced(self):
        self.assertEqual(tts.get_voice_base_name("Yue (Enhanced)"), "Yue")

    def test_no_suffix_unchanged(self):
        self.assertEqual(tts.get_voice_base_name("Meijia"), "Meijia")

    def test_empty(self):
        self.assertEqual(tts.get_voice_base_name(""), "")


class TestHasExplicitTierSuffix(unittest.TestCase):
    def test_premium(self):
        self.assertTrue(tts.has_explicit_tier_suffix("Yue (Premium)"))

    def test_enhanced(self):
        self.assertTrue(tts.has_explicit_tier_suffix("Yue (Enhanced)"))

    def test_plain(self):
        self.assertFalse(tts.has_explicit_tier_suffix("Yue"))

    def test_empty(self):
        self.assertFalse(tts.has_explicit_tier_suffix(""))


class TestPickBestTierVoice(unittest.TestCase):
    def test_prefers_premium_over_enhanced(self):
        voices = make_voices(("Yue (Enhanced)", "zh-HK"), ("Yue (Premium)", "zh-HK"))
        result = tts.pick_best_tier_voice(voices)
        self.assertEqual(result["match"]["name"], "Yue (Premium)")
        self.assertEqual(result["tierDecision"], "upgraded_premium")

    def test_prefers_enhanced_over_regular(self):
        voices = make_voices(("Samantha", "en-US"), ("Samantha (Enhanced)", "en-US"))
        result = tts.pick_best_tier_voice(voices)
        self.assertEqual(result["match"]["name"], "Samantha (Enhanced)")

    def test_single_voice(self):
        voices = make_voices(("Meijia", "zh-TW"))
        result = tts.pick_best_tier_voice(voices)
        self.assertEqual(result["match"]["name"], "Meijia")

    def test_empty_list(self):
        result = tts.pick_best_tier_voice([])
        self.assertIsNone(result["match"])
        self.assertEqual(result["tierDecision"], "none")


class TestConvertRateForMac(unittest.TestCase):
    def test_default_rate(self):
        self.assertEqual(tts.convert_rate_for_mac(1.0), 175)

    def test_faster(self):
        self.assertEqual(tts.convert_rate_for_mac(1.1), 193)  # ceil(175*1.1)=193

    def test_slower(self):
        self.assertEqual(tts.convert_rate_for_mac(0.9), 158)  # ceil(175*0.9)=158

    def test_minimum_is_one(self):
        self.assertEqual(tts.convert_rate_for_mac(0.001), 1)


# ---------------------------------------------------------------------------
# 3. Voice resolution
# ---------------------------------------------------------------------------

class TestResolveVoiceName(unittest.TestCase):
    def setUp(self):
        self.voices = make_voices(
            ("Samantha", "en-US"),
            ("Samantha (Enhanced)", "en-US"),
            ("Samantha (Premium)", "en-US"),
            ("Meijia", "zh-TW"),
            ("Meijia (Premium)", "zh-TW"),
        )

    def test_exact_match_no_tier_pref_upgrades(self):
        res = tts.resolve_voice_name("Samantha", self.voices, prefer_premium=True)
        # Should upgrade to Premium
        self.assertEqual(res["match"]["name"], "Samantha (Premium)")
        self.assertEqual(res["resolutionPath"], "exact")

    def test_exact_tagged_keeps_exact(self):
        res = tts.resolve_voice_name("Samantha (Enhanced)", self.voices, prefer_premium=True)
        self.assertEqual(res["match"]["name"], "Samantha (Enhanced)")
        self.assertEqual(res["tierDecision"], "kept_exact")

    def test_base_fallback_when_no_exact(self):
        voices = make_voices(("Samantha (Premium)", "en-US"))
        res = tts.resolve_voice_name("Samantha", voices, prefer_premium=False)
        self.assertEqual(res["match"]["name"], "Samantha (Premium)")
        self.assertEqual(res["resolutionPath"], "base_fallback")

    def test_unknown_voice_returns_none(self):
        res = tts.resolve_voice_name("NoSuchVoice", self.voices, prefer_premium=True)
        self.assertIsNone(res["match"])

    def test_prefer_premium_false_keeps_exact(self):
        res = tts.resolve_voice_name("Samantha", self.voices, prefer_premium=False)
        self.assertEqual(res["match"]["name"], "Samantha")

    def test_case_insensitive(self):
        res = tts.resolve_voice_name("MEIJIA", self.voices, prefer_premium=False)
        self.assertEqual(res["match"]["name"], "Meijia")

    def test_empty_name(self):
        res = tts.resolve_voice_name("", self.voices, prefer_premium=True)
        self.assertIsNone(res["match"])


# ---------------------------------------------------------------------------
# 4. Locale and voice selection
# ---------------------------------------------------------------------------

class TestLocaleCandidatesForLang(unittest.TestCase):
    def setUp(self):
        self.voices = make_voices(
            ("Meijia", "zh-TW"),
            ("Tingting", "zh-CN"),
            ("Samantha", "en-US"),
        )
        self.config = {
            "localePreferenceByLanguage": {"zh": ["zh-TW", "zh-HK", "zh-CN"]},
            "voicePreferenceByLocale": {},
        }

    def test_lang_with_config_prefs(self):
        # locale_candidates_for_lang returns config prefs as-is (no installed filtering).
        # Filtering by installed voices is done by build_effective_config, not here.
        candidates = tts.locale_candidates_for_lang("zh", self.voices, self.config)
        self.assertIn("zh-TW", candidates)
        self.assertIn("zh-CN", candidates)
        # zh-HK is in prefs so it appears in candidates (filtering happens upstream)
        self.assertIn("zh-HK", candidates)

    def test_specific_locale_first(self):
        candidates = tts.locale_candidates_for_lang("zh-TW", self.voices, self.config)
        self.assertEqual(candidates[0], "zh-TW")

    def test_empty_tag_returns_empty(self):
        candidates = tts.locale_candidates_for_lang("", self.voices, self.config)
        self.assertEqual(candidates, [])

    def test_no_config_falls_back_to_installed(self):
        config = {"localePreferenceByLanguage": {}, "voicePreferenceByLocale": {}}
        candidates = tts.locale_candidates_for_lang("en", self.voices, config)
        self.assertIn("en-US", candidates)


class TestSelectVoiceForLocale(unittest.TestCase):
    def setUp(self):
        self.voices = make_voices(
            ("Meijia", "zh-TW"),
            ("Meijia (Premium)", "zh-TW"),
            ("Samantha", "en-US"),
        )

    def test_uses_preference(self):
        config = {
            "voicePreferenceByLocale": {"zh-TW": ["Meijia"]},
            "preferPremiumOrEnhancedVoice": True,
        }
        sel = tts.select_voice_for_locale("zh-TW", self.voices, config)
        self.assertEqual(sel["voice"], "Meijia (Premium)")
        self.assertTrue(sel["usedVoicePreference"])

    def test_no_pref_picks_best_tier(self):
        config = {"voicePreferenceByLocale": {}, "preferPremiumOrEnhancedVoice": True}
        sel = tts.select_voice_for_locale("zh-TW", self.voices, config)
        self.assertEqual(sel["voice"], "Meijia (Premium)")
        self.assertFalse(sel["usedVoicePreference"])

    def test_no_pref_no_premium_picks_first(self):
        config = {"voicePreferenceByLocale": {}, "preferPremiumOrEnhancedVoice": False}
        sel = tts.select_voice_for_locale("zh-TW", self.voices, config)
        self.assertEqual(sel["voice"], "Meijia")

    def test_locale_not_found_returns_none(self):
        config = {"voicePreferenceByLocale": {}, "preferPremiumOrEnhancedVoice": True}
        sel = tts.select_voice_for_locale("fr-FR", self.voices, config)
        self.assertIsNone(sel["voice"])


class TestResolveSelection(unittest.TestCase):
    def setUp(self):
        self.voices = make_voices(
            ("Samantha (Premium)", "en-US"),
            ("Meijia (Premium)", "zh-TW"),
        )
        self.config = {
            "defaultLang": "en-US",
            "preferPremiumOrEnhancedVoice": True,
            "localePreferenceByLanguage": {
                "zh": ["zh-TW", "zh-HK", "zh-CN"],
                "en": ["en-US"],
            },
            "voicePreferenceByLocale": {
                "zh-TW": ["Meijia"],
                "en-US": ["Samantha"],
            },
        }

    def _args(self, **kwargs):
        args = MagicMock()
        args.voice = kwargs.get("voice", None)
        args.lang = kwargs.get("lang", None)
        return args

    def test_explicit_voice(self):
        args = self._args(voice="Samantha")
        sel = tts.resolve_selection(args, self.voices, self.config)
        self.assertEqual(sel["voice"], "Samantha (Premium)")
        self.assertTrue(sel["explicitVoice"])

    def test_lang_zh_tw(self):
        args = self._args(lang="zh-TW")
        sel = tts.resolve_selection(args, self.voices, self.config)
        self.assertEqual(sel["voice"], "Meijia (Premium)")
        self.assertFalse(sel["explicitVoice"])

    def test_default_lang_fallback(self):
        args = self._args()  # no voice, no lang
        sel = tts.resolve_selection(args, self.voices, self.config)
        self.assertEqual(sel["voice"], "Samantha (Premium)")

    def test_invalid_voice_raises(self):
        args = self._args(voice="GhostVoice")
        with self.assertRaises(ValueError):
            tts.resolve_selection(args, self.voices, self.config)


# ---------------------------------------------------------------------------
# 5. Config loading
# ---------------------------------------------------------------------------

class TestLoadConfig(unittest.TestCase):
    """Patch tts.CONFIG_PATH at the module level (PosixPath methods are read-only)."""

    def _run_with_config(self, content: str | None) -> dict:
        """Write content to a temp file (or use a nonexistent path) and call load_config."""
        if content is None:
            fake_path = Path("/tmp/__nonexistent_tts_config_test__.json")
            with patch("tts_cli.CONFIG_PATH", fake_path):
                return tts.load_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(content)
            tmp_path = Path(f.name)
        try:
            with patch("tts_cli.CONFIG_PATH", tmp_path):
                return tts.load_config()
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_defaults_when_no_file(self):
        config = self._run_with_config(None)
        self.assertEqual(config["defaultLang"], "en-US")
        self.assertAlmostEqual(config["defaultRate"], 1.0)
        self.assertTrue(config["preferPremiumOrEnhancedVoice"])

    def test_user_overrides_default_lang(self):
        config = self._run_with_config(json.dumps({"defaultLang": "zh-TW"}))
        self.assertEqual(config["defaultLang"], "zh-TW")

    def test_invalid_rate_falls_back(self):
        config = self._run_with_config(json.dumps({"defaultRate": -1}))
        self.assertEqual(config["defaultRate"], 1.0)

    def test_malformed_json_uses_defaults(self):
        config = self._run_with_config("not json{{{")
        self.assertEqual(config["defaultLang"], "en-US")


# ---------------------------------------------------------------------------
# 6. Queue operations
# ---------------------------------------------------------------------------

class TestQueueOperations(unittest.TestCase):
    def setUp(self):
        # Redirect all queue paths to a temp dir
        self.tmp_dir = tempfile.mkdtemp()
        self._orig_queue   = tts.QUEUE_PATH
        self._orig_lock    = None  # derived from QUEUE_PATH inside functions
        tts.QUEUE_PATH = Path(self.tmp_dir) / "tts-queue.json"

    def tearDown(self):
        tts.QUEUE_PATH = self._orig_queue
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_read_empty_when_no_file(self):
        self.assertEqual(tts.read_queue(), [])

    def test_write_and_read(self):
        jobs = [{"id": "1", "text": "hello"}, {"id": "2", "text": "world"}]
        tts.write_queue(jobs)
        self.assertEqual(tts.read_queue(), jobs)

    def test_enqueue_appends(self):
        tts.enqueue_job({"id": "1", "text": "first"})
        tts.enqueue_job({"id": "2", "text": "second"})
        jobs = tts.read_queue()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["text"], "first")
        self.assertEqual(jobs[1]["text"], "second")

    def test_pop_returns_first(self):
        tts.write_queue([{"id": "1"}, {"id": "2"}])
        job = tts.pop_queue()
        self.assertEqual(job["id"], "1")
        self.assertEqual(tts.read_queue(), [{"id": "2"}])

    def test_pop_empty_returns_none(self):
        self.assertIsNone(tts.pop_queue())

    def test_pop_clears_single_item(self):
        tts.write_queue([{"id": "x"}])
        tts.pop_queue()
        self.assertEqual(tts.read_queue(), [])

    def test_write_is_atomic(self):
        """write_queue uses tmp+replace, so partial writes shouldn't corrupt."""
        tts.write_queue([{"id": "original"}])
        tts.write_queue([{"id": "replaced"}])
        self.assertEqual(tts.read_queue()[0]["id"], "replaced")

    def test_read_queue_recovers_from_corrupt_file(self):
        tts.QUEUE_PATH.write_text("not valid json")
        self.assertEqual(tts.read_queue(), [])


# ---------------------------------------------------------------------------
# 7. Daemon PID helpers
# ---------------------------------------------------------------------------

class TestDaemonHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._orig_pid = tts.DAEMON_PID_PATH
        tts.DAEMON_PID_PATH = Path(self.tmp_dir) / "tts-daemon.pid"

    def tearDown(self):
        tts.DAEMON_PID_PATH = self._orig_pid
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_not_running_when_no_file(self):
        self.assertFalse(tts.is_daemon_running())

    def test_not_running_when_stale_pid(self):
        # PID 99999999 almost certainly doesn't exist
        tts.DAEMON_PID_PATH.write_text("99999999")
        self.assertFalse(tts.is_daemon_running())
        # Stale file should be cleaned up
        self.assertFalse(tts.DAEMON_PID_PATH.exists())

    def test_running_when_current_pid(self):
        tts.DAEMON_PID_PATH.write_text(str(os.getpid()))
        self.assertTrue(tts.is_daemon_running())

    def test_not_running_when_corrupt_pid_file(self):
        tts.DAEMON_PID_PATH.write_text("not_a_pid")
        self.assertFalse(tts.is_daemon_running())


# ---------------------------------------------------------------------------
# 8. Force play
# ---------------------------------------------------------------------------

class TestForcePlay(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._orig_queue  = tts.QUEUE_PATH
        self._orig_daemon = tts.DAEMON_PID_PATH
        self._orig_say    = tts.SAY_PID_PATH
        tts.QUEUE_PATH      = Path(self.tmp_dir) / "tts-queue.json"
        tts.DAEMON_PID_PATH = Path(self.tmp_dir) / "tts-daemon.pid"
        tts.SAY_PID_PATH    = Path(self.tmp_dir) / "tts-say.pid"

    def tearDown(self):
        tts.QUEUE_PATH      = self._orig_queue
        tts.DAEMON_PID_PATH = self._orig_daemon
        tts.SAY_PID_PATH    = self._orig_say
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("tts_cli.start_daemon")
    @patch("os.kill")
    def test_force_clears_queue_and_restarts(self, mock_kill, mock_start):
        # Pre-populate queue and PID files
        tts.write_queue([{"id": "old1"}, {"id": "old2"}])
        tts.DAEMON_PID_PATH.write_text("12345")
        tts.SAY_PID_PATH.write_text("12346")

        new_job = {"id": "new", "text": "urgent"}
        tts.force_play(new_job)

        # Queue should contain only the new job
        self.assertEqual(tts.read_queue(), [new_job])
        # start_daemon should have been called
        mock_start.assert_called_once()

    @patch("tts_cli.start_daemon")
    @patch("os.kill")
    def test_force_with_no_pid_files(self, mock_kill, mock_start):
        """force_play should work even if no daemon/say is running."""
        tts.force_play({"id": "x", "text": "hi"})
        mock_start.assert_called_once()
        self.assertEqual(tts.read_queue(), [{"id": "x", "text": "hi"}])


# ---------------------------------------------------------------------------
# 9. Voice cache
# ---------------------------------------------------------------------------

class TestVoiceCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        self._orig = tts.VOICE_CACHE_PATH
        tts.VOICE_CACHE_PATH = Path(self.tmp.name)

    def tearDown(self):
        tts.VOICE_CACHE_PATH = self._orig
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_write_and_read_cache(self):
        voices = [{"name": "Samantha", "locale": "en-US"}]
        tts.write_voice_cache("key123", voices)
        cached = tts.read_voice_cache()
        self.assertIsNotNone(cached)
        self.assertEqual(cached["cacheKey"], "key123")
        self.assertEqual(cached["voices"], voices)

    def test_cache_hit_returns_voices(self):
        voices = [{"name": "Meijia", "locale": "zh-TW"}]
        tts.write_voice_cache("key-abc", voices)
        with patch("tts_cli.build_voice_cache_key", return_value="key-abc"):
            result = tts.read_mac_voices()
        self.assertEqual(result, voices)

    def test_cache_miss_calls_system(self):
        system_voices = [{"name": "Samantha (Premium)", "locale": "en-US"}]
        tts.write_voice_cache("old-key", [])
        with (
            patch("tts_cli.build_voice_cache_key", return_value="new-key"),
            patch("tts_cli.read_mac_voices_from_system", return_value=system_voices),
        ):
            result = tts.read_mac_voices()
        self.assertEqual(result, system_voices)

    def test_no_cache_skips_file(self):
        system_voices = [{"name": "Yue (Premium)", "locale": "zh-HK"}]
        with patch("tts_cli.read_mac_voices_from_system", return_value=system_voices):
            result = tts.read_mac_voices(no_cache=True)
        self.assertEqual(result, system_voices)


# ---------------------------------------------------------------------------
# 10. Voice discovery parsing
# ---------------------------------------------------------------------------

class TestReadMacVoicesFromSystem(unittest.TestCase):
    SAY_OUTPUT = """\
Meijia              zh_TW    # 你好，我叫美佳。
Samantha            en_US    # Hello, my name is Samantha.
Samantha (Enhanced) en_US    # Hello, my name is Samantha.
Yue (Premium)       zh_HK    # 你好，我叫玥。
BadLine
"""

    def test_parses_voices(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=self.SAY_OUTPUT, stderr="")
            voices = tts.read_mac_voices_from_system()

        names = [v["name"] for v in voices]
        self.assertIn("Meijia", names)
        self.assertIn("Samantha", names)
        self.assertIn("Samantha (Enhanced)", names)
        self.assertIn("Yue (Premium)", names)
        # Bad line should be skipped
        self.assertEqual(len(voices), 4)

    def test_locale_normalized(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=self.SAY_OUTPUT, stderr="")
            voices = tts.read_mac_voices_from_system()

        meijia = next(v for v in voices if v["name"] == "Meijia")
        self.assertEqual(meijia["locale"], "zh-TW")  # underscore normalized

    def test_raises_on_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="say error")
            with self.assertRaises(RuntimeError):
                tts.read_mac_voices_from_system()


# ---------------------------------------------------------------------------
# 11. Build effective config
# ---------------------------------------------------------------------------

class TestBuildEffectiveConfig(unittest.TestCase):
    def test_prunes_missing_locales(self):
        voices = make_voices(("Samantha", "en-US"))
        config = {
            "defaultLang": "en-US",
            "defaultRate": 1.0,
            "preferPremiumOrEnhancedVoice": True,
            "localePreferenceByLanguage": {"zh": ["zh-TW", "zh-HK"], "en": ["en-US"]},
            "voicePreferenceByLocale": {"zh-TW": ["Meijia"], "en-US": ["Samantha"]},
        }
        eff = tts.build_effective_config(config, voices)
        # zh locales not installed → pruned
        self.assertNotIn("zh", eff["localePreferenceByLanguage"])
        # en-US installed → kept
        self.assertIn("en", eff["localePreferenceByLanguage"])

    def test_prunes_missing_voice_prefs(self):
        voices = make_voices(("Samantha", "en-US"))
        config = {
            "defaultLang": "en-US",
            "defaultRate": 1.0,
            "preferPremiumOrEnhancedVoice": True,
            "localePreferenceByLanguage": {"en": ["en-US"]},
            "voicePreferenceByLocale": {"en-US": ["Samantha", "Alex"]},
        }
        eff = tts.build_effective_config(config, voices)
        # Alex not installed → pruned from preferences
        self.assertNotIn("Alex", eff["voicePreferenceByLocale"].get("en-US", []))
        self.assertIn("Samantha", eff["voicePreferenceByLocale"].get("en-US", []))


# ---------------------------------------------------------------------------
# 12. Invalid voice message
# ---------------------------------------------------------------------------

class TestBuildInvalidVoiceMessage(unittest.TestCase):
    def test_includes_voice_name(self):
        msg = tts.build_invalid_voice_message("Ghost", "zh-TW", "en-US")
        self.assertIn("Ghost", msg)

    def test_uses_lang_in_example(self):
        msg = tts.build_invalid_voice_message("Ghost", "zh-TW", "en-US")
        self.assertIn("zh-TW", msg)

    def test_falls_back_to_default_lang(self):
        msg = tts.build_invalid_voice_message("Ghost", "", "en-US")
        self.assertIn("en-US", msg)


# ---------------------------------------------------------------------------
# 13. Normalize lists and maps
# ---------------------------------------------------------------------------

class TestNormalizeLists(unittest.TestCase):
    def test_locale_list_deduplicates(self):
        result = tts.normalize_locale_list(["en-US", "EN_US", "en-US"])
        self.assertEqual(result, ["en-US"])

    def test_locale_list_filters_empty(self):
        result = tts.normalize_locale_list(["", None, "en-US"])
        self.assertEqual(result, ["en-US"])

    def test_locale_list_non_list_returns_empty(self):
        self.assertEqual(tts.normalize_locale_list("en-US"), [])

    def test_voice_list_deduplicates_case_insensitive(self):
        result = tts.normalize_voice_list(["Meijia", "MEIJIA", "meijia"])
        self.assertEqual(result, ["Meijia"])  # first original preserved

    def test_voice_list_non_list_returns_empty(self):
        self.assertEqual(tts.normalize_voice_list(None), [])


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
