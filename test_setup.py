import atexit
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("setup.py")
SPEC = importlib.util.spec_from_file_location("assistant_setup", MODULE_PATH)
setup_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
# Register in sys.modules before exec_module so @dataclass can resolve type hints
# via sys.modules.get(cls.__module__) — without this the import crashes.
sys.modules["assistant_setup"] = setup_module
SPEC.loader.exec_module(setup_module)

# ---------------------------------------------------------------------------
# Registry isolation — redirect project-registry writes to a temp file so that
# test runs never pollute ~/.tamago/known-projects.json.
#
# add_project_to_registry and remove_project_from_registry both have
#   registry_path: Path = KNOWN_PROJECTS_FILE
# as a default argument (baked in at function-definition time).  We monkey-
# patch the two functions so that any call that uses the real default path is
# silently redirected to a temporary file that is deleted at process exit.
# Tests that explicitly pass their own registry_path are unaffected.
# ---------------------------------------------------------------------------

_REAL_REGISTRY = setup_module.KNOWN_PROJECTS_FILE
_REGISTRY_TMPDIR = tempfile.mkdtemp(prefix="tamago_test_registry_")
_TEST_REGISTRY = Path(_REGISTRY_TMPDIR) / "known-projects.json"
atexit.register(shutil.rmtree, _REGISTRY_TMPDIR, True)

_orig_add_project = setup_module.add_project_to_registry
_orig_remove_project = setup_module.remove_project_from_registry


def _isolated_add(project_root: Path, registry_path: Path = _REAL_REGISTRY) -> None:
    if registry_path == _REAL_REGISTRY:
        registry_path = _TEST_REGISTRY
    return _orig_add_project(project_root, registry_path)


def _isolated_remove(project_root: Path, registry_path: Path = _REAL_REGISTRY) -> None:
    if registry_path == _REAL_REGISTRY:
        registry_path = _TEST_REGISTRY
    return _orig_remove_project(project_root, registry_path)


setup_module.add_project_to_registry = _isolated_add
setup_module.remove_project_from_registry = _isolated_remove


class UnlinkPathsTests(unittest.TestCase):
    def test_uninstall_removes_symlink_even_when_pointing_elsewhere(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            expected_source = source / "agent.md"
            expected_source.write_text("expected")

            elsewhere = root / "elsewhere.md"
            elsewhere.write_text("elsewhere")

            target_root = root / "target"
            target_root.mkdir()
            target = target_root / expected_source.name
            target.symlink_to(elsewhere)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                setup_module.unlink_paths([expected_source], target_root)

            self.assertFalse(target.exists())
            self.assertFalse(target.is_symlink())
            self.assertIn(f"removed {target}", output.getvalue())

    def test_uninstall_warns_when_target_is_not_symlink_and_keeps_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            expected_source = source / "settings.json"
            expected_source.write_text("{}")

            target_root = root / "target"
            target_root.mkdir()
            target = target_root / expected_source.name
            target.write_text("real file")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                setup_module.unlink_paths([expected_source], target_root)

            self.assertTrue(target.exists())
            self.assertFalse(target.is_symlink())
            self.assertIn(f"warning {target} is not a symlink", output.getvalue())


class ResolveSourceRootTests(unittest.TestCase):
    def test_prefers_cli_source_override(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.dict(
                os.environ, {"ASSISTANT_SETUP_REPO": "/should/not/use"}, clear=False
            ),
        ):
            resolved = setup_module.resolve_source_root(temp_dir)

        self.assertEqual(resolved, Path(temp_dir))

    def test_falls_back_to_environment_variable(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.dict(
                os.environ, {"ASSISTANT_SETUP_REPO": temp_dir}, clear=False
            ),
        ):
            resolved = setup_module.resolve_source_root(None)

        self.assertEqual(resolved, Path(temp_dir))

    def test_falls_back_to_default_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            default_root = Path(temp_dir)
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(setup_module, "DEFAULT_SOURCE_ROOT", default_root),
            ):
                resolved = setup_module.resolve_source_root(None)

        self.assertEqual(resolved, default_root)

    def test_raises_when_source_root_does_not_exist(self):
        missing_root = Path("/path/that/does/not/exist")

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(setup_module, "DEFAULT_SOURCE_ROOT", missing_root),
        ):
            with self.assertRaisesRegex(
                ValueError, f"Source directory not found: {missing_root}"
            ):
                setup_module.resolve_source_root(None)


class ResolveProfileRootTests(unittest.TestCase):
    def test_profile_dir_returns_resolved_path(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()
            profile = Path(td) / "my.agent-profile"
            profile.mkdir()

            result = setup_module.resolve_profile_root(source, str(profile), None, None)
            self.assertEqual(result, profile.resolve())

    def test_profile_dir_missing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "not found"):
                setup_module.resolve_profile_root(source, "/does/not/exist", None, None)

    def test_profile_name_resolves_to_sibling(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()
            profile = source / "hammer.mei-profile"
            profile.mkdir()

            result = setup_module.resolve_profile_root(source, None, None, "hammer.mei")
            self.assertEqual(result, profile)

    def test_profile_name_missing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "not found"):
                setup_module.resolve_profile_root(source, None, None, "nobody")

    def test_profile_repo_rejects_non_profile_name(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "must end with '-profile'"):
                setup_module.resolve_profile_root(
                    source, None, "https://github.com/user/my-agent.git", None
                )

    def test_repo_name_from_url_strips_git_suffix(self):
        cases = [
            ("https://github.com/user/hammer.mei-profile.git", "hammer.mei-profile"),
            ("https://github.com/user/hammer.mei-profile",     "hammer.mei-profile"),
            ("git@github.com:user/xiao.mei-profile.git",       "xiao.mei-profile"),
            ("ssh://git@host/path/my-profile.git",             "my-profile"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(setup_module._repo_name_from_url(url), expected)

    def test_none_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()
            result = setup_module.resolve_profile_root(source, None, None, None)
            self.assertIsNone(result)


class SetupGitignoreTests(unittest.TestCase):
    def test_install_adds_missing_entries_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text("node_modules\n.claude\n")

            setup_module.setup_gitignore(setup_module.Operation.INSTALL, project_root)

            self.assertEqual(
                gitignore.read_text(),
                "node_modules\n.claude\n.opencode\n.tamago/machine.env\n.tamago/machine.toml\n",
            )

    def test_install_does_not_duplicate_entries_with_trailing_slash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            # File entries with trailing slash (unusual but valid gitignore syntax)
            gitignore.write_text(".claude/\n.opencode/\n.tamago/machine.env/\n.tamago/machine.toml/\n")

            setup_module.setup_gitignore(setup_module.Operation.INSTALL, project_root)

            self.assertEqual(
                gitignore.read_text(),
                ".claude/\n.opencode/\n.tamago/machine.env/\n.tamago/machine.toml/\n",
            )

    def test_install_creates_gitignore_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            setup_module.setup_gitignore(setup_module.Operation.INSTALL, project_root)

            self.assertEqual(
                (project_root / ".gitignore").read_text(),
                ".claude\n.opencode\n.tamago/machine.env\n.tamago/machine.toml\n",
            )

    def test_uninstall_removes_managed_entries_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text(
                "node_modules\n.claude\n.opencode\n.tamago/machine.env\n.tamago/machine.toml\ndist\n"
            )

            setup_module.setup_gitignore(setup_module.Operation.UNINSTALL, project_root)

            self.assertEqual(gitignore.read_text(), "node_modules\ndist\n")

    def test_uninstall_removes_entries_with_trailing_slash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text(".claude/\n.opencode/\n.tamago/machine.env/\n.tamago/machine.toml/\n")

            setup_module.setup_gitignore(setup_module.Operation.UNINSTALL, project_root)

            self.assertEqual(gitignore.read_text(), "")


class RunHealthCheckTests(unittest.TestCase):
    def test_skips_when_script_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            project_root = Path(temp_dir)
            # scripts/health-check.sh does not exist in temp_dir
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                mock.patch("subprocess.run") as run_mock,
            ):
                setup_module.run_health_check(source_root, project_root)

            run_mock.assert_not_called()
            self.assertIn("skip", output.getvalue())

    def test_runs_health_check_when_script_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            scripts_dir = source_root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "health-check.sh").write_text("#!/bin/bash\necho ok")

            project_root = Path(temp_dir)
            with mock.patch("subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                setup_module.run_health_check(source_root, project_root)

            run_mock.assert_called_once_with(
                ["bash", str(source_root / "scripts" / "health-check.sh"),
                 "--project", str(project_root)],
                check=False,
            )

    def test_does_not_raise_when_health_check_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            scripts_dir = source_root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "health-check.sh").write_text("#!/bin/bash\nexit 1")

            project_root = Path(temp_dir)
            with mock.patch("subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=1)
                # Should not raise
                setup_module.run_health_check(source_root, project_root)


class SetupHealthCheckIntegrationTests(unittest.TestCase):
    def _make_minimal_source(self, temp_dir: str) -> tuple[Path, Path]:
        """Create a minimal tamago source and project root for setup() calls."""
        source_root = Path(temp_dir) / "tamago"
        project_root = Path(temp_dir) / "project"
        project_root.mkdir(parents=True)

        # Minimal source structure setup() needs
        for subdir in ["skills", "agents", "settings/claude", "settings/opencode",
                       "settings/opencode/plugins"]:
            (source_root / subdir).mkdir(parents=True)
        (source_root / "settings" / "claude" / "settings.json").write_text("{}")
        (source_root / "settings" / "opencode" / "opencode.json").write_text("{}")

        return source_root, project_root

    def test_setup_does_not_call_health_check(self):
        """setup() no longer calls run_health_check directly — it was moved to
        install_from_conf() so the check runs after machine.toml is written."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root, project_root = self._make_minimal_source(temp_dir)

            with mock.patch.object(setup_module, "run_health_check") as hc_mock:
                result = setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root
                )

            self.assertEqual(result, 0)
            hc_mock.assert_not_called()

    def test_setup_does_not_call_health_check_on_uninstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root, project_root = self._make_minimal_source(temp_dir)

            with mock.patch.object(setup_module, "run_health_check") as hc_mock:
                setup_module.setup(
                    setup_module.Operation.UNINSTALL, source_root, project_root
                )

            hc_mock.assert_not_called()


class ProfileReplaceTests(unittest.TestCase):
    """When install is run twice with different profiles, stale artifacts from
    the first profile must be cleaned up automatically before installing the new one."""

    def _make_minimal_source(self, temp_dir: str) -> tuple[Path, Path]:
        """Create a minimal tamago source and project root for setup() calls."""
        source_root = Path(temp_dir) / "tamago"
        project_root = Path(temp_dir) / "project"
        project_root.mkdir(parents=True)

        for subdir in [
            "skills", "agents", "settings/claude", "settings/opencode",
            "settings/opencode/plugins",
        ]:
            (source_root / subdir).mkdir(parents=True)
        (source_root / "settings" / "claude" / "settings.json").write_text("{}")
        (source_root / "settings" / "opencode" / "opencode.json").write_text("{}")
        return source_root, project_root

    def _make_profile(self, temp_dir: str, name: str) -> Path:
        """Create a minimal profile with one persona agent and a memory dir."""
        profile = Path(temp_dir) / f"{name}-profile"
        (profile / "agents").mkdir(parents=True)
        (profile / "agents" / f"{name}.persona.md").write_text(
            f"---\nagent: {name}\n---\nHello from {name}"
        )
        (profile / "agents" / "memory" / name).mkdir(parents=True)
        (profile / "settings" / "claude").mkdir(parents=True)
        (profile / "settings" / "claude" / "settings.json").write_text(
            f'{{"agent": "{name}"}}'
        )
        (profile / "settings" / "opencode").mkdir(parents=True)
        (profile / "settings" / "opencode" / "opencode.json").write_text("{}")
        return profile

    def test_reinstall_with_different_profile_leaves_old_agent_files(self):
        """Switching profiles via setup() does NOT auto-remove old profile files.

        Tamago no longer reads the old profile path from tamago.conf to clean up
        stale agents — profile swaps require manual cleanup of old .md files.
        install_from_conf() handles cross-profile detection via machine.toml,
        but setup() itself is profile-unaware beyond what it's given.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root, project_root = self._make_minimal_source(temp_dir)

            # We need a minimal tamago-agent-base.md so _merge_agent works
            docs_dir = source_root / "docs"
            docs_dir.mkdir()
            (docs_dir / "tamago-agent-base.md").write_text("base {{AGENT_NAME}}")

            profile_a = self._make_profile(temp_dir, "hammer.mei")
            profile_b = self._make_profile(temp_dir, "little.mei")

            with mock.patch.object(setup_module, "run_health_check"):
                setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root, profile_a
                )

            # After first install: hammer.mei agent file and memory symlink exist
            claude_agents = project_root / ".claude" / "agents"
            claude_mem = project_root / ".claude" / "agent-memory"
            self.assertTrue((claude_agents / "hammer.mei.md").exists())
            self.assertTrue((claude_mem / "hammer.mei").is_symlink())

            with mock.patch.object(setup_module, "run_health_check"):
                setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root, profile_b
                )

            # Old profile files remain — profile swap cleanup is manual.
            self.assertTrue((claude_agents / "hammer.mei.md").exists(),
                            "old agent .md is left behind; user must clean up manually")
            self.assertTrue((claude_mem / "hammer.mei").is_symlink(),
                            "old memory symlink is left behind; user must clean up manually")

            # New profile's artifacts are present alongside the old ones.
            self.assertTrue((claude_agents / "little.mei.md").exists())
            self.assertTrue((claude_mem / "little.mei").is_symlink())

    def test_reinstall_with_same_profile_does_not_double_remove(self):
        """Installing the same profile twice should be idempotent — no removal."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root, project_root = self._make_minimal_source(temp_dir)

            docs_dir = source_root / "docs"
            docs_dir.mkdir()
            (docs_dir / "tamago-agent-base.md").write_text("base {{AGENT_NAME}}")

            profile_a = self._make_profile(temp_dir, "hammer.mei")

            with mock.patch.object(setup_module, "run_health_check"):
                setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root, profile_a
                )
            with mock.patch.object(setup_module, "run_health_check"):
                result = setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root, profile_a
                )

            self.assertEqual(result, 0)
            # File should still be present (idempotent install)
            self.assertTrue(
                (project_root / ".claude" / "agents" / "hammer.mei.md").exists()
            )

    def test_reinstall_without_profile_does_not_touch_existing_artifacts(self):
        """If no profile is given on second install, skip the cleanup step entirely."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root, project_root = self._make_minimal_source(temp_dir)

            docs_dir = source_root / "docs"
            docs_dir.mkdir()
            (docs_dir / "tamago-agent-base.md").write_text("base {{AGENT_NAME}}")

            profile_a = self._make_profile(temp_dir, "hammer.mei")

            with mock.patch.object(setup_module, "run_health_check"):
                setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root, profile_a
                )

            # Second install with no profile (profile_root=None) — cleanup must NOT run
            with (
                mock.patch.object(setup_module, "run_health_check"),
                mock.patch.object(setup_module, "setup_agents") as agents_mock,
            ):
                setup_module.setup(
                    setup_module.Operation.INSTALL, source_root, project_root, None
                )

            # setup_agents should have been called once for INSTALL, not for UNINSTALL
            calls = agents_mock.call_args_list
            for call in calls:
                self.assertNotEqual(
                    call.args[0], setup_module.Operation.UNINSTALL,
                    "setup_agents(UNINSTALL) must not be called when profile_root is None"
                )


class MainTests(unittest.TestCase):
    def test_main_passes_cli_source_to_install_from_conf(self):
        """--source flag is forwarded to install_from_conf as source_root."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            # Create a tamago.conf so auto-detect picks it up.
            tamago_dir = project / ".tamago"
            tamago_dir.mkdir()
            (tamago_dir / "tamago.conf").write_text("[settings]\n")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as ifc_mock,
                mock.patch("sys.argv", ["setup.py", "install", "--source", temp_dir]),
                mock.patch.object(setup_module.Path, "cwd", return_value=project),
            ):
                result = setup_module.main()

        self.assertEqual(result, 0)
        call_args = ifc_mock.call_args
        # source_root should be Path(temp_dir) — the value passed via --source
        self.assertEqual(call_args.args[2], Path(temp_dir))

    def test_main_accepts_source_before_subcommand(self):
        """--source may appear before the subcommand — global flag position."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            tamago_dir = project / ".tamago"
            tamago_dir.mkdir()
            (tamago_dir / "tamago.conf").write_text("[settings]\n")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as ifc_mock,
                mock.patch("sys.argv", ["setup.py", "--source", temp_dir, "install"]),
                mock.patch.object(setup_module.Path, "cwd", return_value=project),
            ):
                result = setup_module.main()

        self.assertEqual(result, 0)
        call_args = ifc_mock.call_args
        self.assertEqual(call_args.args[2], Path(temp_dir))

    def test_main_returns_error_when_source_root_is_invalid(self):
        missing_root = "/path/that/does/not/exist"
        stderr = io.StringIO()

        with (
            mock.patch("sys.argv", ["setup.py", "install", "--source", missing_root]),
            mock.patch.object(setup_module.sys, "stderr", stderr),
        ):
            result = setup_module.main()

        self.assertEqual(result, 1)
        self.assertIn(f"Source directory not found: {missing_root}", stderr.getvalue())


class TamagoConfTests(unittest.TestCase):
    """Tests for load_tamago_conf (TOML parser)."""

    def _write_toml(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_returns_none_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = setup_module.load_tamago_conf(Path(td) / "nonexistent.conf")
            self.assertIsNone(result)

    def test_returns_none_on_invalid_toml(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.conf"
            p.write_text("this is not [ valid toml !!!")
            result = setup_module.load_tamago_conf(p)
            self.assertIsNone(result)

    def test_empty_toml_gives_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text("")
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertEqual(conf.profiles, [])
            self.assertEqual(conf.agents, [])
            self.assertEqual(conf.skills, [])
            self.assertTrue(conf.memory_sync)

    def test_parses_agents(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            self._write_toml(p, """
[[agents]]
name = "hammer.mei"
tts = false
""")
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertEqual(len(conf.agents), 1)
            self.assertEqual(conf.agents[0].name, "hammer.mei")
            self.assertFalse(conf.agents[0].tts)
            self.assertEqual(conf.agents[0].source, "tamago")   # default

    def test_parses_memory_sync_false(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            # memory_sync lives under [settings] per the v2 spec
            self._write_toml(p, "[settings]\nmemory_sync = false\n")
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertFalse(conf.memory_sync)

    def test_skips_agent_entries_without_name(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            self._write_toml(p, """
[[agents]]
source = "tamago"
""")
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertEqual(len(conf.agents), 0)

    def test_returns_none_on_valid_toml_wrong_schema(self):
        """Valid TOML but agents is a table (not array of tables) — must not crash."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            # [agents] as a table, not [[agents]] array — causes TypeError on iteration
            p.write_text("[agents]\nname = \"hammer.mei\"\n")
            result = setup_module.load_tamago_conf(p)
            self.assertIsNone(result)

    def test_returns_none_when_memory_sync_is_string(self):
        """[settings] memory_sync = \"false\" (string) must return None, not silently coerce to True."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            # Under [settings] block — string value should still be rejected
            p.write_text('[settings]\nmemory_sync = "false"\n')
            result = setup_module.load_tamago_conf(p)
            self.assertIsNone(result)

    def test_parses_profiles_and_skills(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            self._write_toml(p, """
[[profiles]]
name = "hammer.mei"
repo = "git@github.com:example/hammer.mei-profile.git"

[[skills]]
name = "text-to-speech"
scope = "project"
""")
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertEqual(len(conf.profiles), 1)
            self.assertEqual(conf.profiles[0].name, "hammer.mei")
            self.assertEqual(len(conf.skills), 1)
            self.assertEqual(conf.skills[0].name, "text-to-speech")
            self.assertEqual(conf.skills[0].scope, "project")


class MachineEnvTests(unittest.TestCase):
    """Tests for write_machine_env (shell bridge file)."""

    def test_writes_machine_env(self):
        with tempfile.TemporaryDirectory() as td:
            profile_repo = Path(td) / "my-profile"
            profile_repo.mkdir()
            env_path = Path(td) / "project" / ".tamago" / "machine.env"

            setup_module.write_machine_env(env_path, profile_repo, "hammer.mei", True)

            self.assertTrue(env_path.exists())
            content = env_path.read_text()
            # Values are single-quoted for safe shell sourcing
            self.assertIn(str(profile_repo.resolve()), content)
            self.assertIn("PROFILE_REPO=", content)
            self.assertIn("AGENT_NAME=", content)
            self.assertIn("hammer.mei", content)
            self.assertIn("MEMORY_SYNC=1", content)
            self.assertIn("TTS_ENABLED=1", content)

    def test_writes_memory_sync_0_when_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            profile_repo = Path(td) / "my-profile"
            profile_repo.mkdir()
            env_path = Path(td) / ".tamago" / "machine.env"

            setup_module.write_machine_env(env_path, profile_repo, "hammer.mei", False)

            content = env_path.read_text()
            self.assertIn("MEMORY_SYNC=0", content)

    def test_removes_file_when_profile_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".tamago" / "machine.env"
            env_path.parent.mkdir(parents=True)
            env_path.write_text("PROFILE_REPO=/some/path\n")

            setup_module.write_machine_env(env_path, None, "")

            self.assertFalse(env_path.exists())

    def test_idempotent_when_content_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            profile_repo = Path(td) / "my-profile"
            profile_repo.mkdir()
            env_path = Path(td) / ".tamago" / "machine.env"

            output1 = io.StringIO()
            with contextlib.redirect_stdout(output1):
                setup_module.write_machine_env(env_path, profile_repo, "hammer.mei")

            output2 = io.StringIO()
            with contextlib.redirect_stdout(output2):
                setup_module.write_machine_env(env_path, profile_repo, "hammer.mei")

            self.assertIn("updated", output1.getvalue())
            self.assertIn("exists", output2.getvalue())

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as td:
            profile_repo = Path(td) / "my-profile"
            profile_repo.mkdir()
            # Deeply nested path — parent dir does not exist yet
            env_path = Path(td) / "deep" / "nested" / ".tamago" / "machine.env"

            setup_module.write_machine_env(env_path, profile_repo, "test-agent")

            self.assertTrue(env_path.exists())

    def test_machine_env_values_are_shell_quoted(self):
        """Values in machine.env must be single-quoted so paths with spaces are safe."""
        with tempfile.TemporaryDirectory() as td:
            profile_repo = Path(td) / "my-profile"
            profile_repo.mkdir()
            env_path = Path(td) / ".tamago" / "machine.env"

            setup_module.write_machine_env(env_path, profile_repo, "hammer.mei")

            content = env_path.read_text()
            # Both PROFILE_REPO and AGENT_NAME must use single-quoted values
            self.assertRegex(content, r"PROFILE_REPO='[^']*'")
            self.assertRegex(content, r"AGENT_NAME='[^']*'")


class PatchSettingsTests(unittest.TestCase):
    """Tests for patch_settings and unpatch_settings."""

    def test_injects_hooks_into_empty_file(self):
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"

            setup_module.patch_settings(
                path, {"SessionStart": ["memory-sync.sh --init"]}, [], None, manifest
            )

            result = json.loads(path.read_text())
            all_cmds = [
                h["command"]
                for m in result["hooks"]["SessionStart"]
                for h in m.get("hooks", [])
                if "command" in h
            ]
            self.assertIn("memory-sync.sh --init", all_cmds)

    def test_deduplicates_existing_hooks(self):
        """A second patch with the same command must not duplicate it."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "memory-sync.sh --init"}]}
                    ]
                }
            }))
            manifest = Path(td) / "manifest.json"

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.patch_settings(
                    path, {"SessionStart": ["memory-sync.sh --init"]}, [], None, manifest
                )

            self.assertIn("exists", out.getvalue())
            result = json.loads(path.read_text())
            cmds = [
                h["command"]
                for m in result["hooks"]["SessionStart"]
                for h in m.get("hooks", [])
            ]
            self.assertEqual(cmds.count("memory-sync.sh --init"), 1)

    def test_user_hooks_in_same_default_block_preserved(self):
        """User hooks in the same default matcher block survive patch/unpatch."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            # Start with only the user's nagori hook in the default block
            path.write_text(json.dumps({
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "nagori inject-context"}]}
                    ]
                }
            }))
            manifest = Path(td) / "manifest.json"

            # Patch in tamago's memory-sync hook
            setup_module.patch_settings(
                path, {"UserPromptSubmit": ["memory-sync.sh --pull"]}, [], None, manifest
            )

            result = json.loads(path.read_text())
            all_cmds = [
                h["command"]
                for m in result["hooks"]["UserPromptSubmit"]
                for h in m.get("hooks", [])
            ]
            self.assertIn("nagori inject-context", all_cmds)
            self.assertIn("memory-sync.sh --pull", all_cmds)

    def test_permissions_union(self):
        """Tamago perms are appended; existing user perms are kept."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({"permissions": {"allow": ["Bash(nagori *)"]}}))
            manifest = Path(td) / "manifest.json"

            setup_module.patch_settings(
                path, {}, ["Bash(python3 *.claude/skills/*.py *)"], None, manifest
            )

            result = json.loads(path.read_text())
            perms = result["permissions"]["allow"]
            self.assertIn("Bash(nagori *)", perms)
            self.assertIn("Bash(python3 *.claude/skills/*.py *)", perms)

    def test_permissions_not_duplicated(self):
        """A perm already in the file must not be added twice."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            perm = "Bash(python3 *.claude/skills/*.py *)"
            path.write_text(json.dumps({"permissions": {"allow": [perm]}}))
            manifest = Path(td) / "manifest.json"

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.patch_settings(path, {}, [perm], None, manifest)

            self.assertIn("exists", out.getvalue())
            result = json.loads(path.read_text())
            self.assertEqual(result["permissions"]["allow"].count(perm), 1)

    def test_status_line_not_overwritten_if_present(self):
        with tempfile.TemporaryDirectory() as td:
            import json
            user_status = {"type": "command", "command": "echo user-status"}
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({"statusLine": user_status}))
            manifest = Path(td) / "manifest.json"

            tamago_status = {"type": "command", "command": "echo tamago-status"}
            setup_module.patch_settings(path, {}, [], tamago_status, manifest)

            result = json.loads(path.read_text())
            self.assertEqual(result["statusLine"], user_status)
            # Manifest records that status line was NOT injected
            man = json.loads(manifest.read_text())
            self.assertFalse(man["injected_status_line"])

    def test_status_line_injected_if_absent(self):
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            path.write_text("{}")
            manifest = Path(td) / "manifest.json"

            tamago_status = {"type": "command", "command": "echo tamago-status"}
            setup_module.patch_settings(path, {}, [], tamago_status, manifest)

            result = json.loads(path.read_text())
            self.assertEqual(result["statusLine"]["command"], "echo tamago-status")
            man = json.loads(manifest.read_text())
            self.assertTrue(man["injected_status_line"])

    def test_idempotent_second_call(self):
        """Calling patch_settings twice with the same args must not change the file."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"

            hook_commands = {"Stop": ["memory-sync.sh --push"]}
            perms = ["Bash(python3 *.claude/skills/*.py *)"]
            setup_module.patch_settings(path, hook_commands, perms, None, manifest)
            content_after_first = path.read_text()

            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                setup_module.patch_settings(path, hook_commands, perms, None, manifest)

            self.assertIn("exists", out2.getvalue())
            self.assertEqual(path.read_text(), content_after_first)

    def test_symlink_migrated_to_real_file(self):
        """If settings.json is a symlink, patch_settings converts it to a real file."""
        with tempfile.TemporaryDirectory() as td:
            import json
            source = Path(td) / "source.json"
            source.write_text('{"autoDreamEnabled": true}')
            path = Path(td) / "settings.json"
            path.symlink_to(source)
            manifest = Path(td) / "manifest.json"

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.patch_settings(path, {}, [], None, manifest)

            self.assertTrue(path.exists())
            self.assertFalse(path.is_symlink())
            self.assertIn("migrated", out.getvalue())
            # Original content preserved
            self.assertEqual(json.loads(path.read_text()).get("autoDreamEnabled"), True)

    def test_unpatch_removes_injected_entries_preserves_user_entries(self):
        """After patch then unpatch, user's existing entries survive; tamago's are gone."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"
            # Start with only user's nagori hook
            path.write_text(json.dumps({
                "permissions": {"allow": ["Bash(nagori *)"]},
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "nagori inject-context"}]}
                    ]
                }
            }))

            # Patch in tamago entries
            setup_module.patch_settings(
                path,
                {"UserPromptSubmit": ["memory-sync.sh --pull"]},
                ["Bash(python3 *.claude/skills/*.py *)"],
                None,
                manifest,
            )

            # Verify both sets are present
            mid = json.loads(path.read_text())
            all_cmds = [
                h["command"]
                for m in mid["hooks"]["UserPromptSubmit"]
                for h in m.get("hooks", [])
            ]
            self.assertIn("memory-sync.sh --pull", all_cmds)
            self.assertIn("nagori inject-context", all_cmds)

            # Unpatch
            setup_module.unpatch_settings(path, manifest)

            after = json.loads(path.read_text())
            remaining_cmds = [
                h["command"]
                for m in after["hooks"]["UserPromptSubmit"]
                for h in m.get("hooks", [])
            ]
            self.assertNotIn("memory-sync.sh --pull", remaining_cmds)
            self.assertIn("nagori inject-context", remaining_cmds)
            self.assertIn("Bash(nagori *)", after["permissions"]["allow"])
            self.assertNotIn(
                "Bash(python3 *.claude/skills/*.py *)", after["permissions"]["allow"]
            )
            self.assertFalse(manifest.exists())

    def test_unpatch_no_op_when_manifest_missing(self):
        """unpatch_settings must not touch the settings file if manifest is absent."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text('{"hooks": {}}')
            manifest = Path(td) / "nonexistent.json"

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.unpatch_settings(path, manifest)

            self.assertIn("skip", out.getvalue())
            self.assertEqual(path.read_text(), '{"hooks": {}}')

    def test_unpatch_skips_command_user_modified_after_install(self):
        """If user edited an injected command after install, unpatch must leave it alone."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"

            # Patch with the original command
            setup_module.patch_settings(
                path, {"Stop": ["memory-sync.sh --push"]}, [], None, manifest
            )

            # User edits the injected command (adds --verbose)
            settings = json.loads(path.read_text())
            for m in settings["hooks"]["Stop"]:
                for h in m.get("hooks", []):
                    if h.get("command") == "memory-sync.sh --push":
                        h["command"] = "memory-sync.sh --push --verbose"
            path.write_text(json.dumps(settings, indent=2))

            # Unpatch — manifest has original string, won't match the edited command
            setup_module.unpatch_settings(path, manifest)

            after = json.loads(path.read_text())
            cmds = [
                h["command"]
                for m in after["hooks"]["Stop"]
                for h in m.get("hooks", [])
            ]
            # Modified command is treated as user-owned and stays
            self.assertIn("memory-sync.sh --push --verbose", cmds)

    def test_manifest_records_only_newly_injected_entries(self):
        """If an entry already exists when patch runs, manifest must NOT include it."""
        with tempfile.TemporaryDirectory() as td:
            import json
            existing_perm = "Bash(python3 *.claude/skills/*.py *)"
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({"permissions": {"allow": [existing_perm]}}))
            manifest = Path(td) / "manifest.json"

            setup_module.patch_settings(path, {}, [existing_perm], None, manifest)

            man = json.loads(manifest.read_text())
            self.assertNotIn(existing_perm, man["injected_perms"])

    def test_unpatch_after_two_installs_removes_all_injected_entries(self):
        """Manifest must survive idempotent re-install so uninstall still works.

        This is a regression test for the cumulative-manifest bug: if patch_settings
        initialises the in-memory manifest to {} on each call (instead of loading the
        existing sidecar), a second call overwrites the sidecar with an empty manifest,
        and subsequent unpatch_settings silently removes nothing.
        """
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"

            hook_commands = {"Stop": ["memory-sync.sh --push"]}
            perms = ["Bash(python3 *.claude/skills/*.py *)"]
            status = {"type": "command", "command": "echo tamago-status"}

            # First install
            setup_module.patch_settings(path, hook_commands, perms, status, manifest)
            # Second install (idempotent re-run — simulates `tamago install` being run again)
            setup_module.patch_settings(path, hook_commands, perms, status, manifest)

            # Manifest must still record what was injected
            man = json.loads(manifest.read_text())
            self.assertIn("Bash(python3 *.claude/skills/*.py *)", man["injected_perms"])
            self.assertIn("memory-sync.sh --push", man["injected_hooks"].get("Stop", []))
            self.assertTrue(man["injected_status_line"])

            # Unpatch must remove tamago entries
            setup_module.unpatch_settings(path, manifest)

            after = json.loads(path.read_text())
            # Hook gone
            all_cmds = [
                h["command"]
                for m in after.get("hooks", {}).get("Stop", [])
                for h in m.get("hooks", [])
            ]
            self.assertNotIn("memory-sync.sh --push", all_cmds)
            # Perm gone
            remaining_perms = after.get("permissions", {}).get("allow", [])
            self.assertNotIn("Bash(python3 *.claude/skills/*.py *)", remaining_perms)
            # statusLine gone
            self.assertNotIn("statusLine", after)
            # Manifest cleaned up
            self.assertFalse(manifest.exists())

    # ── Bug 1 regression ─────────────────────────────────────────────────────

    def test_symlink_migration_with_tamago_content_uninstalls_cleanly(self):
        """Symlink migration must record tamago entries in manifest so uninstall works.

        Regression for Bug 1: when settings.json is a symlink to tamago's own file
        (which already contains all tamago hooks/perms), dedup finds nothing to add
        and changed=False. If we don't pre-populate the manifest during migration,
        unpatch_settings sees an empty manifest and removes nothing — entries stuck.
        """
        with tempfile.TemporaryDirectory() as td:
            import json

            # Simulate tamago's own settings file (the symlink target)
            source = Path(td) / "tamago-settings.json"
            source.write_text(json.dumps({
                "permissions": {"allow": ["Bash(python3 *.claude/skills/*.py *)"]},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "memory-sync.sh --push"}]}
                    ]
                },
                "statusLine": {"type": "command", "command": "echo tamago-status"},
            }))

            path = Path(td) / "settings.json"
            path.symlink_to(source)
            manifest = Path(td) / "manifest.json"

            hook_commands = {"Stop": ["memory-sync.sh --push"]}
            perms = ["Bash(python3 *.claude/skills/*.py *)"]
            status_line = {"type": "command", "command": "echo tamago-status"}

            setup_module.patch_settings(path, hook_commands, perms, status_line, manifest)

            # Manifest must record tamago's entries even though dedup skipped them
            man = json.loads(manifest.read_text())
            self.assertIn("Bash(python3 *.claude/skills/*.py *)", man["injected_perms"])
            self.assertIn("memory-sync.sh --push", man["injected_hooks"].get("Stop", []))
            self.assertTrue(man["injected_status_line"])

            # Uninstall must remove tamago entries
            setup_module.unpatch_settings(path, manifest)
            after = json.loads(path.read_text())
            self.assertNotIn("Bash(python3 *.claude/skills/*.py *)",
                             after.get("permissions", {}).get("allow", []))
            all_cmds = [
                h["command"]
                for m in after.get("hooks", {}).get("Stop", [])
                for h in m.get("hooks", [])
            ]
            self.assertNotIn("memory-sync.sh --push", all_cmds)
            self.assertNotIn("statusLine", after)

    # ── Bug 2 regression ─────────────────────────────────────────────────────

    def test_unpatch_handles_corrupt_manifest_gracefully(self):
        """unpatch_settings must not crash on a corrupt manifest file."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text('{"hooks": {}}')
            manifest = Path(td) / "manifest.json"
            manifest.write_text("not json {{{")  # corrupt

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.unpatch_settings(path, manifest)

            self.assertIn("warn", out.getvalue())
            # Settings file must be untouched
            self.assertEqual(path.read_text(), '{"hooks": {}}')

    def test_unpatch_handles_corrupt_settings_gracefully(self):
        """unpatch_settings must not crash when settings.json is corrupt."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            path.write_text("not json {{{")  # corrupt
            manifest = Path(td) / "manifest.json"
            manifest.write_text(json.dumps({
                "injected_perms": ["Bash(python3 *)"],
                "injected_hooks": {},
                "injected_status_line": False,
            }))

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.unpatch_settings(path, manifest)

            self.assertIn("warn", out.getvalue())
            # Manifest should still exist (we couldn't clean up)
            self.assertTrue(manifest.exists())

    # ── Bug 3 regression ─────────────────────────────────────────────────────

    def test_unpatch_removes_empty_default_block_and_event_key(self):
        """After unpatch removes all hooks from a default block, the empty block and
        event key must be removed — not left behind as residue."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"

            # Fresh install — tamago creates the default block
            setup_module.patch_settings(
                path, {"Stop": ["memory-sync.sh --push"]}, [], None, manifest
            )

            setup_module.unpatch_settings(path, manifest)

            after = json.loads(path.read_text())
            # No "Stop" key at all — not even an empty list
            self.assertNotIn("Stop", after.get("hooks", {}))

    def test_unpatch_drops_empty_event_but_keeps_non_default_matchers(self):
        """Empty tamago-created default block is dropped; non-default matcher blocks survive."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            # User has a named-matcher block for the same event
            path.write_text(json.dumps({
                "hooks": {
                    "Stop": [
                        {"matcher": "Bash(*)", "hooks": [
                            {"type": "command", "command": "user-special"}
                        ]}
                    ]
                }
            }))
            manifest = Path(td) / "manifest.json"

            setup_module.patch_settings(
                path, {"Stop": ["memory-sync.sh --push"]}, [], None, manifest
            )
            setup_module.unpatch_settings(path, manifest)

            after = json.loads(path.read_text())
            # Named-matcher block stays
            stop_matchers = after.get("hooks", {}).get("Stop", [])
            self.assertEqual(len(stop_matchers), 1)
            self.assertEqual(stop_matchers[0]["matcher"], "Bash(*)")

    # ── dedup correctness ─────────────────────────────────────────────────────

    def test_non_command_type_hook_does_not_suppress_injection(self):
        """A hook with type != 'command' but same command string must not fool dedup."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            # User has a hook with the same command string but different type
            path.write_text(json.dumps({
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "other", "command": "memory-sync.sh --push"}]}
                    ]
                }
            }))
            manifest = Path(td) / "manifest.json"

            setup_module.patch_settings(
                path, {"Stop": ["memory-sync.sh --push"]}, [], None, manifest
            )

            after = json.loads(path.read_text())
            cmds_with_type_command = [
                h["command"]
                for m in after["hooks"]["Stop"]
                for h in m.get("hooks", [])
                if h.get("type") == "command"
            ]
            # Tamago's type=command entry must have been injected
            self.assertIn("memory-sync.sh --push", cmds_with_type_command)

    # ── _read_tamago_source_hooks ──────────────────────────────────────────────

    def test_read_tamago_source_hooks_skips_non_default_matchers(self):
        """_read_tamago_source_hooks must not collect commands from named-matcher blocks."""
        source = {
            "hooks": {
                "Stop": [
                    {"matcher": "Bash(*)", "hooks": [
                        {"type": "command", "command": "should-be-skipped"}
                    ]},
                    {"hooks": [
                        {"type": "command", "command": "should-be-included"}
                    ]},
                ]
            }
        }
        result = setup_module._read_tamago_source_hooks(source)
        self.assertEqual(result["Stop"], ["should-be-included"])
        self.assertNotIn("should-be-skipped", result["Stop"])

    # ── incremental install ────────────────────────────────────────────────────

    def test_incremental_install_accumulates_manifest_across_versions(self):
        """v1 install injects perm A; v2 also injects perm B. Both must be in the
        manifest and both must be removed by a single unpatch call."""
        with tempfile.TemporaryDirectory() as td:
            import json
            path = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"

            perm_a = "Bash(python3 *.claude/skills/*.py *)"
            perm_b = "Bash(keepassxc-cli *)"

            # v1 install — only perm_a
            setup_module.patch_settings(path, {}, [perm_a], None, manifest)
            # v2 install — adds perm_b
            setup_module.patch_settings(path, {}, [perm_a, perm_b], None, manifest)

            man = json.loads(manifest.read_text())
            self.assertIn(perm_a, man["injected_perms"])
            self.assertIn(perm_b, man["injected_perms"])

            setup_module.unpatch_settings(path, manifest)

            after = json.loads(path.read_text())
            remaining = after.get("permissions", {}).get("allow", [])
            self.assertNotIn(perm_a, remaining)
            self.assertNotIn(perm_b, remaining)


class PatchOpencodeGlobalSettingsTests(unittest.TestCase):
    """Tests for patch_opencode_global_settings — mirrors Claude Code's patch approach."""

    def _run(self, operation, source_root, target, manifest):
        with (
            mock.patch.object(
                setup_module, "patch_settings",
                wraps=setup_module.patch_settings,
            ),
            mock.patch.object(
                setup_module, "unpatch_settings",
                wraps=setup_module.unpatch_settings,
            ),
        ):
            # Override the hardcoded paths inside patch_opencode_global_settings
            # by patching Path so that ~/.opencode/... resolves to our temp dirs.
            pass  # we call directly with mocked internals below

    def test_install_migrates_symlink_to_real_file(self):
        """~/.opencode/opencode.json symlink is replaced with a real file on install."""
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            source_root = Path(td) / "tamago"
            opencode_settings_dir = source_root / "settings" / "opencode"
            opencode_settings_dir.mkdir(parents=True)
            source_json = opencode_settings_dir / "opencode.json"
            source_json.write_text(_json.dumps({"$schema": "https://opencode.ai/config.json"}))

            target = Path(td) / "opencode.json"
            manifest = Path(td) / "manifest.json"
            # Simulate existing symlink
            target.symlink_to(source_json)
            self.assertTrue(target.is_symlink())

            setup_module.patch_settings(target, {}, [], None, manifest)

            self.assertFalse(target.is_symlink())
            self.assertTrue(target.is_file())
            content = _json.loads(target.read_text())
            self.assertEqual(content.get("$schema"), "https://opencode.ai/config.json")

    def test_install_writes_manifest(self):
        """patch_opencode_global_settings writes a sidecar manifest."""
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            source_root = Path(td) / "tamago"
            opencode_dir = source_root / "settings" / "opencode"
            opencode_dir.mkdir(parents=True)
            (opencode_dir / "opencode.json").write_text('{"$schema": "x"}')

            target = Path(td) / "opencode.json"
            manifest = Path(td) / ".tamago-manifest.json"

            with (
                mock.patch(
                    "builtins.open", side_effect=open,
                ),
            ):
                setup_module.patch_settings(target, {}, [], None, manifest)

            self.assertTrue(manifest.exists())
            data = _json.loads(manifest.read_text())
            self.assertIn("injected_perms", data)
            self.assertEqual(data["injected_perms"], [])
            self.assertEqual(data["injected_hooks"], {})
            self.assertFalse(data["injected_status_line"])

    def test_install_preserves_existing_user_content(self):
        """User-added keys in opencode.json survive a tamago install."""
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            target = Path(td) / "opencode.json"
            manifest = Path(td) / "manifest.json"
            # User has their own config
            target.write_text(_json.dumps({
                "$schema": "https://opencode.ai/config.json",
                "model": "anthropic/claude-sonnet-4-5",
            }))

            setup_module.patch_settings(target, {}, [], None, manifest)

            after = _json.loads(target.read_text())
            self.assertEqual(after.get("model"), "anthropic/claude-sonnet-4-5")

    def test_install_on_missing_file_writes_manifest_and_does_not_create_config(self):
        """When no injections are needed, patch_settings skips creating opencode.json
        (no-op on the settings file) but still writes the sidecar manifest."""
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            target = Path(td) / "opencode.json"
            manifest = Path(td) / "manifest.json"
            self.assertFalse(target.exists())

            setup_module.patch_settings(target, {}, [], None, manifest)

            # No injections → settings file is NOT created (nothing to write)
            self.assertFalse(target.exists())
            # Manifest IS always written so uninstall knows what to clean up
            self.assertTrue(manifest.exists())
            data = _json.loads(manifest.read_text())
            self.assertEqual(data["injected_perms"], [])

    def test_uninstall_removes_manifest(self):
        """unpatch_settings (the uninstall path) removes the sidecar manifest."""
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            target = Path(td) / "opencode.json"
            manifest = Path(td) / "manifest.json"
            target.write_text('{"$schema": "x", "model": "claude"}')
            manifest.write_text(_json.dumps({
                "injected_perms": [],
                "injected_hooks": {},
                "injected_status_line": False,
            }))

            setup_module.unpatch_settings(target, manifest)

            self.assertFalse(manifest.exists())
            # User content must survive
            after = _json.loads(target.read_text())
            self.assertEqual(after.get("model"), "claude")

    def test_uninstall_without_manifest_is_noop(self):
        """unpatch_settings is silent when no manifest exists."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "opencode.json"
            manifest = Path(td) / "manifest.json"
            target.write_text('{"model": "claude"}')
            # No manifest
            setup_module.unpatch_settings(target, manifest)  # must not raise

    def test_install_is_idempotent(self):
        """A second install with the same source does not change the settings file."""
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            target = Path(td) / "opencode.json"
            manifest = Path(td) / "manifest.json"
            target.write_text('{"$schema": "x"}')

            setup_module.patch_settings(target, {}, [], None, manifest)
            mtime_after_first = target.stat().st_mtime

            setup_module.patch_settings(target, {}, [], None, manifest)
            mtime_after_second = target.stat().st_mtime

            self.assertEqual(mtime_after_first, mtime_after_second)

    def test_setup_global_calls_patch_opencode(self):
        """setup_global uses patch_opencode_global_settings, not the old symlink helper."""
        with tempfile.TemporaryDirectory() as td:
            source_root = Path(td) / "tamago"
            (source_root / "settings" / "claude").mkdir(parents=True)
            (source_root / "settings" / "opencode").mkdir(parents=True)
            (source_root / "settings" / "claude" / "settings.json").write_text("{}")
            (source_root / "settings" / "opencode" / "opencode.json").write_text("{}")

            called = []

            with (
                mock.patch.object(
                    setup_module, "patch_opencode_global_settings",
                    side_effect=lambda *a, **kw: called.append("opencode"),
                ),
                mock.patch.object(
                    setup_module, "patch_global_settings",
                    side_effect=lambda *a, **kw: called.append("claude"),
                ),
                mock.patch.object(setup_module, "setup_shell_env"),
                mock.patch.object(setup_module, "setup_git_hooks"),
                mock.patch.object(setup_module, "setup_local_bin"),
            ):
                setup_module.setup_global(setup_module.Operation.INSTALL, source_root)

            self.assertIn("opencode", called)
            self.assertIn("claude", called)


class TamagoConfSettingsBlockTests(unittest.TestCase):
    """Tests specifically covering the [settings] block migration in load_tamago_conf."""

    def test_memory_sync_read_from_settings_block(self):
        """[settings] memory_sync = false must be parsed correctly."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text("[settings]\nmemory_sync = false\n")
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertFalse(conf.memory_sync)

    def test_top_level_memory_sync_is_ignored(self):
        """Top-level memory_sync = false (outside [settings]) must NOT be parsed.

        This is a regression guard: before the v2 fix, the parser read memory_sync
        from the TOML root.  Now it lives under [settings]; a top-level key should
        be silently ignored and the default (True) used instead.
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text("memory_sync = false\n")  # top-level — not under [settings]
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            # Top-level key is ignored; default True should apply
            self.assertTrue(conf.memory_sync)

    def test_settings_block_not_dict_returns_none(self):
        """If [settings] is somehow not a dict (TOML edge case), return None safely."""
        # TOML doesn't allow a key and a table of the same name in valid TOML.
        # We simulate by patching after load — direct coverage of the isinstance guard.
        # The guard runs when settings_block is not a dict.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            # Patch the raw dict to simulate a corrupt/unexpected type
            with mock.patch("tomllib.load", return_value={"settings": "not-a-dict"}):
                result = setup_module.load_tamago_conf(p)
            # settings is a string, not a dict — should return None
            self.assertIsNone(result)


class InstallFromConfTests(unittest.TestCase):
    """Tests for install_from_conf (Slice C — TOML-driven install)."""

    def _write_toml(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_returns_error_on_missing_conf(self):
        with tempfile.TemporaryDirectory() as td:
            result = setup_module.install_from_conf(
                Path(td) / "nonexistent.conf",
                setup_module.Operation.INSTALL,
                Path(td) / "source",
                Path(td) / "project",
            )
            self.assertEqual(result, 1)

    def test_returns_error_on_corrupt_conf(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("NOT VALID TOML [[[[")
            result = setup_module.install_from_conf(
                conf_path,
                setup_module.Operation.INSTALL,
                Path(td) / "source",
                Path(td) / "project",
            )
            self.assertEqual(result, 1)

    def test_no_profiles_calls_setup_with_no_profile_root(self):
        """When tamago.conf has no [[profiles]], profile_root should be None."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")  # empty = valid, no profiles

            with (
                mock.patch.object(setup_module, "setup", return_value=0) as mock_setup,
                mock.patch.object(setup_module, "pull_repo"),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            self.assertEqual(result, 0)
            # profile_root is the 4th keyword arg
            self.assertIsNone(mock_setup.call_args.kwargs["profile_root"])

    def test_memory_sync_false_propagates_to_setup(self):
        """[settings] memory_sync = false → setup receives memory_sync=False."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("[settings]\nmemory_sync = false\n")

            with (
                mock.patch.object(setup_module, "setup", return_value=0) as mock_setup,
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            self.assertFalse(mock_setup.call_args.kwargs["memory_sync"])

    def test_agent_tts_false_propagates_to_setup(self):
        """First profile agent with tts=false → setup receives tts_enabled=False."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            self._write_toml(conf_path, """
[[agents]]
name = "hammer.mei"
source = "profile"
tts = false
""")
            with (
                mock.patch.object(setup_module, "setup", return_value=0) as mock_setup,
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            self.assertFalse(mock_setup.call_args.kwargs["tts_enabled"])

    def test_tamago_source_agent_tts_does_not_affect_tts_default(self):
        """Only source='profile' agents drive tts_enabled; tamago-source is ignored."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            self._write_toml(conf_path, """
[[agents]]
name = "code-reviewer"
source = "tamago"
tts = false
""")
            with (
                mock.patch.object(setup_module, "setup", return_value=0) as mock_setup,
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            # tts_enabled defaults to True because no profile agent was specified
            self.assertTrue(mock_setup.call_args.kwargs["tts_enabled"])

    def test_profile_name_resolved_and_passed_to_setup(self):
        """[[profiles]] name= → resolve_profile_root called → profile_root passed to setup."""
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()
            profile = source / "hammer.mei-profile"
            profile.mkdir()

            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text('[[profiles]]\nname = "hammer.mei"\n')

            with (
                mock.patch.object(setup_module, "setup", return_value=0) as mock_setup,
                mock.patch.object(setup_module, "pull_repo"),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    source,
                    Path(td) / "project",
                )

            self.assertEqual(result, 0)
            self.assertEqual(mock_setup.call_args.kwargs["profile_root"], profile)

    def test_multiple_profiles_returns_error(self):
        """More than one [[profiles]] entry is unsupported — must return 1."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            self._write_toml(conf_path, """
[[profiles]]
name = "hammer.mei"

[[profiles]]
name = "edm_mei"
""")
            stderr = io.StringIO()
            with mock.patch.object(setup_module.sys, "stderr", stderr):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )
            self.assertEqual(result, 1)
            self.assertIn("2 [[profiles]]", stderr.getvalue())

    def test_no_pull_on_uninstall(self):
        """pull_repo must NOT be called during uninstall."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo") as mock_pull,
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.UNINSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            mock_pull.assert_not_called()

    def test_pull_called_for_source_on_install(self):
        """pull_repo(source_root, 'tamago') must be called during install."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo") as mock_pull,
            ):
                source = Path(td) / "source"
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    source,
                    Path(td) / "project",
                )

            mock_pull.assert_called_once_with(source, "tamago")


class SkillRepoCacheTests(unittest.TestCase):
    """Tests for _skill_repo_cache_dir and _clone_or_reuse_skill_repo."""

    def test_cache_dir_stable_for_same_url(self):
        """Same URL → same cache dir every time."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td)
            url = "https://github.com/example/my-skill.git"
            d1 = setup_module._skill_repo_cache_dir(url, cache_root)
            d2 = setup_module._skill_repo_cache_dir(url, cache_root)
            self.assertEqual(d1, d2)

    def test_different_urls_hash_differently(self):
        """Different URLs → different cache dirs."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td)
            d1 = setup_module._skill_repo_cache_dir("https://github.com/a/repo.git", cache_root)
            d2 = setup_module._skill_repo_cache_dir("https://github.com/b/repo.git", cache_root)
            self.assertNotEqual(d1, d2)

    def test_no_url_normalization_dot_git_differs(self):
        """https://x.git and https://x hash to different directories (no normalization)."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td)
            d1 = setup_module._skill_repo_cache_dir("https://github.com/x/repo.git", cache_root)
            d2 = setup_module._skill_repo_cache_dir("https://github.com/x/repo", cache_root)
            self.assertNotEqual(d1, d2)

    def test_cache_dir_is_16_hex_chars(self):
        """Cache dir name is exactly 16 hex characters."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td)
            d = setup_module._skill_repo_cache_dir("https://example.com/skill.git", cache_root)
            self.assertRegex(d.name, r"^[0-9a-f]{16}$")

    def test_clone_or_reuse_reuses_existing_git_dir(self):
        """Existing .git dir → no subprocess call, returns cache_dir."""
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "cache"
            cache_dir.mkdir()
            (cache_dir / ".git").mkdir()

            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(setup_module.subprocess, "run") as mock_run,
            ):
                result = setup_module._clone_or_reuse_skill_repo(
                    "https://example.com/skill.git", cache_dir
                )

            mock_run.assert_not_called()
            self.assertEqual(result, cache_dir)
            self.assertIn("cached", out.getvalue())

    def test_clone_or_reuse_raises_when_non_git_dir_exists(self):
        """cache_dir exists but has no .git → raises Exception."""
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "cache"
            cache_dir.mkdir()
            # No .git inside — not a git repo

            with self.assertRaises(Exception) as ctx:
                setup_module._clone_or_reuse_skill_repo(
                    "https://example.com/skill.git", cache_dir
                )
            self.assertIn("not a git repo", str(ctx.exception))

    def test_clone_or_reuse_clones_on_miss(self):
        """On cache miss: calls git clone; raises on non-zero exit."""
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "cache"
            url = "https://example.com/skill.git"

            mock_result = mock.Mock()
            mock_result.returncode = 0
            with mock.patch.object(
                setup_module.subprocess, "run", return_value=mock_result
            ) as mock_run:
                result = setup_module._clone_or_reuse_skill_repo(url, cache_dir)

            self.assertEqual(result, cache_dir)
            call_args = mock_run.call_args[0][0]
            self.assertIn("clone", call_args)
            self.assertIn(url, call_args)

    def test_clone_failure_raises(self):
        """git clone exit != 0 → raises Exception with stderr."""
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "cache"
            mock_result = mock.Mock()
            mock_result.returncode = 128
            mock_result.stderr = "fatal: repository not found"
            with mock.patch.object(setup_module.subprocess, "run", return_value=mock_result):
                with self.assertRaises(Exception) as ctx:
                    setup_module._clone_or_reuse_skill_repo(
                        "https://example.com/bad.git", cache_dir
                    )
            self.assertIn("git clone failed", str(ctx.exception))


class SetupExternalSkillsTests(unittest.TestCase):
    """Tests for setup_external_skills."""

    def _make_skill(self, name, source, scope="project", path=None):
        return setup_module.SkillEntry(name=name, source=source, scope=scope, path=path)

    def test_tamago_source_skipped(self):
        """source='tamago' entries are ignored — no symlinks, no clones."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill = self._make_skill("some-skill", "tamago")
            with mock.patch.object(
                setup_module, "_clone_or_reuse_skill_repo"
            ) as mock_clone:
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )
            self.assertEqual(rc, 0)
            mock_clone.assert_not_called()

    def test_profile_source_skipped(self):
        """source='profile' entries are ignored."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill = self._make_skill("some-skill", "profile")
            with mock.patch.object(
                setup_module, "_clone_or_reuse_skill_repo"
            ) as mock_clone:
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )
            self.assertEqual(rc, 0)
            mock_clone.assert_not_called()

    def test_global_scope_warns_and_skips(self):
        """scope='global' emits warning and skips — returns 0 (not an error)."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill = self._make_skill(
                "my-skill", "https://example.com/skill.git", scope="global"
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(setup_module.sys, "stderr", stderr),
                mock.patch.object(
                    setup_module, "_clone_or_reuse_skill_repo"
                ) as mock_clone,
            ):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )
            self.assertEqual(rc, 0)
            self.assertIn("global", stderr.getvalue())
            mock_clone.assert_not_called()

    def test_url_skill_linked_with_skill_name(self):
        """URL skill is cloned and symlinked under skill.name (not the hash dir name)."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill_dir = Path(td) / "cloned-skill-dir"
            skill_dir.mkdir()
            url = "https://example.com/my-skill.git"
            skill = self._make_skill("my-skill", url)

            with mock.patch.object(
                setup_module, "_resolve_external_skill_dir", return_value=skill_dir
            ):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )

            self.assertEqual(rc, 0)
            claude_target = project / ".claude" / "skills" / "my-skill"
            opencode_target = project / ".opencode" / "skills" / "my-skill"
            self.assertTrue(claude_target.is_symlink())
            self.assertTrue(opencode_target.is_symlink())
            self.assertEqual(claude_target.resolve(), skill_dir.resolve())

    def test_url_skill_idempotent_when_already_linked(self):
        """Second install with same target symlink prints 'exists' and returns 0."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill_dir = Path(td) / "cloned-skill-dir"
            skill_dir.mkdir()
            url = "https://example.com/my-skill.git"
            skill = self._make_skill("my-skill", url)

            # Pre-create the symlinks
            for skills_root_rel in (".claude/skills", ".opencode/skills"):
                sr = project / skills_root_rel
                sr.mkdir(parents=True)
                (sr / "my-skill").symlink_to(skill_dir)

            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(
                    setup_module, "_resolve_external_skill_dir", return_value=skill_dir
                ),
            ):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )

            self.assertEqual(rc, 0)
            self.assertIn("exists", out.getvalue())

    def test_clone_failure_returns_1(self):
        """If _resolve_external_skill_dir raises, returns 1."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            url = "https://example.com/bad-skill.git"
            skill = self._make_skill("bad-skill", url)

            stderr = io.StringIO()
            with (
                mock.patch.object(setup_module.sys, "stderr", stderr),
                mock.patch.object(
                    setup_module,
                    "_resolve_external_skill_dir",
                    side_effect=Exception("git clone failed"),
                ),
            ):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )

            self.assertEqual(rc, 1)
            self.assertIn("error", stderr.getvalue())

    def test_skill_path_not_found_returns_error(self):
        """skill.path subdir missing in repo → _resolve_external_skill_dir raises → error."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            cache_root = Path(td) / "cache"
            cache_root.mkdir()
            url = "https://example.com/monorepo.git"
            skill = self._make_skill("my-skill", url, path="skills/my-skill")

            # Simulate a cloned repo with no 'skills/my-skill' subdir
            cache_dir = setup_module._skill_repo_cache_dir(url, cache_root)
            cache_dir.mkdir(parents=True)
            (cache_dir / ".git").mkdir()
            # skills/my-skill does NOT exist in the repo

            stderr = io.StringIO()
            with mock.patch.object(setup_module.sys, "stderr", stderr):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project,
                    cache_root=cache_root,
                )

            self.assertEqual(rc, 1)
            self.assertIn("error", stderr.getvalue())

    def test_uninstall_removes_symlink(self):
        """UNINSTALL removes existing skill symlinks from both .claude and .opencode."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill_dir = Path(td) / "cloned-skill-dir"
            skill_dir.mkdir()
            url = "https://example.com/my-skill.git"
            skill = self._make_skill("my-skill", url)

            # Pre-create symlinks (as if previously installed)
            for skills_root_rel in (".claude/skills", ".opencode/skills"):
                sr = project / skills_root_rel
                sr.mkdir(parents=True)
                (sr / "my-skill").symlink_to(skill_dir)

            rc = setup_module.setup_external_skills(
                setup_module.Operation.UNINSTALL, [skill], project
            )

            self.assertEqual(rc, 0)
            self.assertFalse((project / ".claude" / "skills" / "my-skill").exists())
            self.assertFalse((project / ".opencode" / "skills" / "my-skill").exists())

    def test_uninstall_with_url_skill_not_present_is_silent(self):
        """UNINSTALL when skill symlink doesn't exist is a no-op (no error)."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill = self._make_skill("missing-skill", "https://example.com/skill.git")

            rc = setup_module.setup_external_skills(
                setup_module.Operation.UNINSTALL, [skill], project
            )
            self.assertEqual(rc, 0)

    def test_multiple_errors_counted(self):
        """Two failing URL skills → returns 1 (errors > 0)."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skills = [
                self._make_skill("skill-a", "https://example.com/a.git"),
                self._make_skill("skill-b", "https://example.com/b.git"),
            ]
            stderr = io.StringIO()
            with (
                mock.patch.object(setup_module.sys, "stderr", stderr),
                mock.patch.object(
                    setup_module,
                    "_resolve_external_skill_dir",
                    side_effect=Exception("clone failed"),
                ),
            ):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, skills, project
                )
            self.assertEqual(rc, 1)


class PullSkillReposTests(unittest.TestCase):
    """Tests for _pull_skill_repos."""

    def test_deduplicates_same_url(self):
        """Monorepo with two skills at different paths is pulled only once."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "cache"
            url = "https://example.com/monorepo.git"
            cache_dir = setup_module._skill_repo_cache_dir(url, cache_root)
            cache_dir.mkdir(parents=True)
            (cache_dir / ".git").mkdir()

            skills = [
                setup_module.SkillEntry(name="skill-a", source=url, path="skills/a"),
                setup_module.SkillEntry(name="skill-b", source=url, path="skills/b"),
            ]
            with mock.patch.object(setup_module, "pull_repo") as mock_pull:
                setup_module._pull_skill_repos(skills, cache_root)

            mock_pull.assert_called_once()  # pulled exactly once

    def test_tamago_and_profile_sources_skipped(self):
        """source='tamago' and source='profile' are not treated as URLs."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "cache"
            skills = [
                setup_module.SkillEntry(name="a", source="tamago"),
                setup_module.SkillEntry(name="b", source="profile"),
            ]
            with mock.patch.object(setup_module, "pull_repo") as mock_pull:
                setup_module._pull_skill_repos(skills, cache_root)
            mock_pull.assert_not_called()

    def test_not_yet_cloned_repo_skipped(self):
        """Repo not yet in cache is silently skipped (will be cloned on install)."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "cache"
            url = "https://example.com/new-skill.git"
            skills = [setup_module.SkillEntry(name="new-skill", source=url)]

            with mock.patch.object(setup_module, "pull_repo") as mock_pull:
                setup_module._pull_skill_repos(skills, cache_root)

            mock_pull.assert_not_called()  # cache_dir doesn't exist yet — skip


class MainUpdateAndConfigTests(unittest.TestCase):
    """Tests for 'tamago update' alias and --config flag routing."""

    def test_update_is_alias_for_install(self):
        """'tamago update' routes to install_from_conf with pull_cached_skills=True."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            tamago_dir = project / ".tamago"
            tamago_dir.mkdir()
            (tamago_dir / "tamago.conf").write_text("[settings]\n")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch("sys.argv", ["setup.py", "update", "--source", td]),
                mock.patch.object(setup_module.Path, "cwd", return_value=project),
            ):
                result = setup_module.main()

        self.assertEqual(result, 0)
        call_kwargs = mock_ifc.call_args.kwargs
        # update alias sets pull_cached_skills=True to pull URL-sourced skill repos.
        self.assertTrue(call_kwargs.get("pull_cached_skills", False))

    def test_install_config_flag_routes_to_install_from_conf(self):
        """'tamago install --config path/to/tamago.conf' calls install_from_conf."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch("sys.argv", [
                    "setup.py", "install",
                    "--source", td,
                    "--config", str(conf_path),
                ]),
            ):
                result = setup_module.main()

        self.assertEqual(result, 0)
        call_args = mock_ifc.call_args
        self.assertEqual(call_args.args[0], conf_path.expanduser())  # conf_path
        self.assertEqual(call_args.args[1], setup_module.Operation.INSTALL)

    def test_uninstall_config_flag_routes_to_install_from_conf(self):
        """'tamago uninstall --config path/to/tamago.conf' calls install_from_conf with UNINSTALL."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch("sys.argv", [
                    "setup.py", "uninstall",
                    "--source", td,
                    "--config", str(conf_path),
                ]),
            ):
                result = setup_module.main()

        self.assertEqual(result, 0)
        call_args = mock_ifc.call_args
        self.assertEqual(call_args.args[1], setup_module.Operation.UNINSTALL)

    def test_install_config_passes_pull_cached_false(self):
        """'tamago install --config ...' passes pull_cached_skills=False."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch("sys.argv", [
                    "setup.py", "install",
                    "--source", td,
                    "--config", str(conf_path),
                ]),
            ):
                setup_module.main()

        self.assertFalse(mock_ifc.call_args.kwargs["pull_cached_skills"])

    def test_update_config_passes_pull_cached_true(self):
        """'tamago update --config ...' passes pull_cached_skills=True."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch("sys.argv", [
                    "setup.py", "update",
                    "--source", td,
                    "--config", str(conf_path),
                ]),
            ):
                setup_module.main()

        self.assertTrue(mock_ifc.call_args.kwargs["pull_cached_skills"])

    def test_install_auto_detects_tamago_conf(self):
        """'tamago install' without --config auto-detects .tamago/tamago.conf in CWD."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            tamago_dir = project / ".tamago"
            tamago_dir.mkdir(parents=True)
            conf_path = tamago_dir / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch("sys.argv", ["setup.py", "install", "--source", td]),
                mock.patch.object(setup_module.Path, "cwd", return_value=project),
            ):
                result = setup_module.main()

        self.assertEqual(result, 0)
        self.assertTrue(mock_ifc.called, "install_from_conf should be called via auto-detect")
        self.assertEqual(mock_ifc.call_args.args[0], conf_path)

    def test_install_no_auto_detect_when_conf_absent(self):
        """'tamago install' without --config falls through to legacy path when no tamago.conf."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()

            with (
                mock.patch.object(
                    setup_module, "install_from_conf", return_value=0
                ) as mock_ifc,
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch("sys.argv", ["setup.py", "install", "--source", td]),
                mock.patch.object(setup_module.Path, "cwd", return_value=project),
            ):
                setup_module.main()

        self.assertFalse(mock_ifc.called, "install_from_conf should NOT be called — no tamago.conf")


class InstallFromConfExternalSkillsTests(unittest.TestCase):
    """Tests for install_from_conf interaction with external skills (Slice D)."""

    def _write_toml(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_setup_failure_short_circuits_external_skills(self):
        """If setup() returns 1, setup_external_skills is NOT called."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            self._write_toml(conf_path, """
[[skills]]
name = "my-skill"
source = "https://example.com/skill.git"
scope = "project"
""")
            with (
                mock.patch.object(setup_module, "setup", return_value=1),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(
                    setup_module, "setup_external_skills"
                ) as mock_ext,
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            self.assertEqual(result, 1)
            mock_ext.assert_not_called()

    def test_external_skills_called_when_setup_succeeds(self):
        """If setup() returns 0, setup_external_skills is called."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(
                    setup_module, "setup_external_skills", return_value=0
                ) as mock_ext,
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            self.assertEqual(result, 0)
            mock_ext.assert_called_once()

    def test_pull_cached_false_does_not_call_pull_skill_repos(self):
        """pull_cached_skills=False → _pull_skill_repos is NOT called."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            self._write_toml(conf_path, """
[[skills]]
name = "my-skill"
source = "https://example.com/skill.git"
scope = "project"
""")
            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "setup_external_skills", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(setup_module, "_pull_skill_repos") as mock_pull_skills,
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                    pull_cached_skills=False,
                )

            mock_pull_skills.assert_not_called()

    def test_pull_cached_true_calls_pull_skill_repos(self):
        """pull_cached_skills=True → _pull_skill_repos is called."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            self._write_toml(conf_path, """
[[skills]]
name = "my-skill"
source = "https://example.com/skill.git"
scope = "project"
""")
            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "setup_external_skills", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(setup_module, "_pull_skill_repos") as mock_pull_skills,
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                    pull_cached_skills=True,
                )

            mock_pull_skills.assert_called_once()

    def test_external_skills_failure_propagates(self):
        """setup_external_skills returning 1 → install_from_conf returns 1."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(
                    setup_module, "setup_external_skills", return_value=1
                ),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    Path(td) / "project",
                )

            self.assertEqual(result, 1)


class MachineTomlTests(unittest.TestCase):
    """Tests for write_machine_toml / load_machine_toml (Slice E)."""

    def test_roundtrip_write_and_load(self):
        """write then load returns identical MachineToml."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".tamago" / "machine.toml"
            data = setup_module.MachineToml(
                profiles={"hammer.mei": "/Users/glin/.tamago/hammer.mei-profile"},
                skill_cache={"my-skill": "/Users/glin/.tamago/repo-cache/abc123"},
            )
            setup_module.write_machine_toml(path, data)
            loaded = setup_module.load_machine_toml(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.profiles, data.profiles)
            self.assertEqual(loaded.skill_cache, data.skill_cache)

    def test_creates_parent_dirs(self):
        """write_machine_toml creates .tamago/ if it does not exist."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".tamago" / "machine.toml"
            self.assertFalse(path.parent.exists())
            setup_module.write_machine_toml(
                path,
                setup_module.MachineToml(profiles={"x": "/some/path"}),
            )
            self.assertTrue(path.exists())

    def test_load_returns_none_on_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "machine.toml"
            self.assertIsNone(setup_module.load_machine_toml(path))

    def test_load_returns_none_on_corrupt_toml(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "machine.toml"
            path.write_text("NOT VALID TOML [[[[")
            self.assertIsNone(setup_module.load_machine_toml(path))

    def test_write_is_idempotent(self):
        """Second write with same content prints 'exists' and doesn't change mtime."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".tamago" / "machine.toml"
            data = setup_module.MachineToml(
                profiles={"hammer.mei": "/some/profile"},
            )
            setup_module.write_machine_toml(path, data)
            mtime_before = path.stat().st_mtime
            setup_module.write_machine_toml(path, data)
            mtime_after = path.stat().st_mtime
            self.assertEqual(mtime_before, mtime_after)

    def test_toml_escaping_backslash(self):
        """Backslash in path values is properly escaped in TOML output."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "machine.toml"
            data = setup_module.MachineToml(
                profiles={"agent": r"C:\Users\glin\profile"}
            )
            setup_module.write_machine_toml(path, data)
            content = path.read_text()
            # TOML basic string: backslash → \\
            self.assertIn(r"C:\\Users\\glin\\profile", content)
            # Roundtrip must recover original
            loaded = setup_module.load_machine_toml(path)
            self.assertEqual(loaded.profiles["agent"], r"C:\Users\glin\profile")

    def test_toml_escaping_double_quote(self):
        """Double-quote in path values is properly escaped in TOML output."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "machine.toml"
            data = setup_module.MachineToml(
                profiles={"agent": '/weird/"quoted"/path'}
            )
            setup_module.write_machine_toml(path, data)
            loaded = setup_module.load_machine_toml(path)
            self.assertEqual(loaded.profiles["agent"], '/weird/"quoted"/path')

    def test_empty_data_writes_header_only(self):
        """Empty MachineToml writes successfully with just the header comment."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "machine.toml"
            setup_module.write_machine_toml(path, setup_module.MachineToml())
            self.assertTrue(path.exists())
            # Must be loadable
            loaded = setup_module.load_machine_toml(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.profiles, {})
            self.assertEqual(loaded.skill_cache, {})

    def test_dotted_key_name_roundtrips(self):
        """Keys like 'hammer.mei' (dotted) survive write/load via TOML quoting."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "machine.toml"
            data = setup_module.MachineToml(
                profiles={"hammer.mei": "/path/to/profile"}
            )
            setup_module.write_machine_toml(path, data)
            loaded = setup_module.load_machine_toml(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.profiles.get("hammer.mei"), "/path/to/profile")


class InstallFromConfMachineTomlTests(unittest.TestCase):
    """Tests for machine.toml lifecycle in install_from_conf (Slice E)."""

    def _make_conf(self, td: str, content: str = "") -> Path:
        conf_path = Path(td) / "tamago.conf"
        conf_path.write_text(content)
        return conf_path

    def test_machine_toml_written_on_install(self):
        """install_from_conf writes machine.toml when profile_root is set."""
        with tempfile.TemporaryDirectory() as td:
            profile_dir = Path(td) / "hammer.mei-profile"
            profile_dir.mkdir()
            conf_path = self._make_conf(
                td,
                f'[[profiles]]\nname = "hammer.mei"\n',
            )
            project_root = Path(td) / "project"

            written_data: list[setup_module.MachineToml] = []

            def capture_write(path, data):
                written_data.append(data)

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(setup_module, "resolve_profile_root",
                                  return_value=profile_dir),
                mock.patch.object(setup_module, "write_machine_toml",
                                  side_effect=capture_write),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(written_data), 1)
            data = written_data[0]
            # Profiles dict should have the resolved path
            self.assertEqual(list(data.profiles.values())[0], str(profile_dir.resolve()))

    def test_machine_toml_not_written_on_uninstall(self):
        """install_from_conf never calls write_machine_toml on UNINSTALL."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = self._make_conf(td)
            project_root = Path(td) / "project"

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "write_machine_toml") as mock_write,
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.UNINSTALL,
                    Path(td) / "source",
                    project_root,
                )

            mock_write.assert_not_called()

    def test_machine_toml_deleted_after_uninstall(self):
        """machine.toml is removed after a successful UNINSTALL."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = self._make_conf(td)
            project_root = Path(td) / "project"
            machine_toml = project_root / ".tamago" / "machine.toml"
            machine_toml.parent.mkdir(parents=True)
            machine_toml.write_text(
                "# header\n\n[profiles]\n"
            )

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "setup_external_skills", return_value=0),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.UNINSTALL,
                    Path(td) / "source",
                    project_root,
                )

            self.assertEqual(result, 0)
            self.assertFalse(machine_toml.exists())

    def test_machine_toml_written_before_external_skills(self):
        """write_machine_toml is called before setup_external_skills."""
        call_order: list[str] = []

        with tempfile.TemporaryDirectory() as td:
            conf_path = self._make_conf(td)
            project_root = Path(td) / "project"

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
                mock.patch.object(
                    setup_module, "write_machine_toml",
                    side_effect=lambda *a, **kw: call_order.append("write_machine_toml"),
                ),
                mock.patch.object(
                    setup_module, "setup_external_skills",
                    side_effect=lambda *a, **kw: call_order.append("setup_external_skills") or 0,
                ),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                )

        self.assertEqual(call_order, ["write_machine_toml", "setup_external_skills"])

    def test_uninstall_uses_machine_toml_when_conf_would_resolve_different_path(self):
        """The key Slice E test: uninstall reads profile path from machine.toml,
        not by re-resolving conf.profiles, so it uses the path from install time.
        """
        with tempfile.TemporaryDirectory() as td:
            source_root = Path(td) / "tamago"
            source_root.mkdir()

            # Profile A — the one installed originally
            profile_a = source_root / "original-profile"
            profile_a.mkdir()

            # Profile B — would be resolved if conf is re-parsed (different path)
            profile_b = source_root / "different-profile"
            profile_b.mkdir()

            # tamago.conf still says "original" by name
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text('[[profiles]]\nname = "original"\n')

            project_root = Path(td) / "project"
            machine_toml_path = project_root / ".tamago" / "machine.toml"
            machine_toml_path.parent.mkdir(parents=True)

            # machine.toml points to profile_a (installed path)
            setup_module.write_machine_toml(
                machine_toml_path,
                setup_module.MachineToml(profiles={"original": str(profile_a.resolve())}),
            )

            # Now change conf to point at profile_b so re-resolution would differ
            conf_path.write_text('[[profiles]]\nname = "different"\n')

            captured_profile_root: list = []

            def capture_setup(operation, source_root, project_root, **kwargs):
                captured_profile_root.append(kwargs.get("profile_root"))
                return 0

            with (
                mock.patch.object(setup_module, "setup", side_effect=capture_setup),
                mock.patch.object(setup_module, "setup_external_skills", return_value=0),
                # resolve_profile_root should NOT be called since machine.toml wins
                mock.patch.object(
                    setup_module, "resolve_profile_root",
                    return_value=profile_b,  # would return wrong path if called
                ) as mock_resolve,
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.UNINSTALL,
                    source_root,
                    project_root,
                )

            self.assertEqual(result, 0)
            # setup() must have received profile_a, not profile_b
            self.assertEqual(len(captured_profile_root), 1)
            self.assertEqual(captured_profile_root[0].resolve(), profile_a.resolve())
            # resolve_profile_root should not have been called (machine.toml was sufficient)
            mock_resolve.assert_not_called()
            # machine.toml should be gone after uninstall
            self.assertFalse(machine_toml_path.exists())

    def test_uninstall_falls_back_to_conf_when_machine_toml_absent(self):
        """Without machine.toml (pre-Slice-E install), conf.profiles is used."""
        with tempfile.TemporaryDirectory() as td:
            source_root = Path(td) / "tamago"
            source_root.mkdir()
            profile_dir = source_root / "hammer.mei-profile"
            profile_dir.mkdir()

            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text('[[profiles]]\nname = "hammer.mei"\n')

            project_root = Path(td) / "project"
            # No machine.toml present

            captured_profile_root: list = []

            def capture_setup(operation, source_root, project_root, **kwargs):
                captured_profile_root.append(kwargs.get("profile_root"))
                return 0

            with (
                mock.patch.object(setup_module, "setup", side_effect=capture_setup),
                mock.patch.object(setup_module, "setup_external_skills", return_value=0),
                mock.patch.object(
                    setup_module, "resolve_profile_root",
                    return_value=profile_dir,
                ),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.UNINSTALL,
                    source_root,
                    project_root,
                )

            self.assertEqual(result, 0)
            self.assertEqual(captured_profile_root[0], profile_dir)


class KnownProjectsRegistryTests(unittest.TestCase):
    """Tests for the JSON project registry (Slice F)."""

    def test_read_returns_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "known-projects.json"
            self.assertEqual(setup_module.read_known_projects(path), [])

    def test_read_returns_empty_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "known-projects.json"
            path.write_text("NOT JSON {{{")
            self.assertEqual(setup_module.read_known_projects(path), [])

    def test_read_returns_empty_on_missing_projects_key(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "known-projects.json"
            path.write_text('{"other": []}')
            self.assertEqual(setup_module.read_known_projects(path), [])

    def test_add_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "myproject"
            project.mkdir()
            setup_module.add_project_to_registry(project, registry)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["path"], str(project.resolve()))

    def test_add_is_idempotent(self):
        """Adding the same project twice results in one entry."""
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "myproject"
            project.mkdir()
            setup_module.add_project_to_registry(project, registry)
            setup_module.add_project_to_registry(project, registry)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 1)

    def test_add_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "subdir" / "known-projects.json"
            project = Path(td) / "myproject"
            project.mkdir()
            setup_module.add_project_to_registry(project, registry)
            self.assertTrue(registry.exists())

    def test_add_multiple_projects(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            for name in ("proj_a", "proj_b", "proj_c"):
                p = Path(td) / name
                p.mkdir()
                setup_module.add_project_to_registry(p, registry)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 3)

    def test_remove_existing_project(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "myproject"
            project.mkdir()
            setup_module.add_project_to_registry(project, registry)
            setup_module.remove_project_from_registry(project, registry)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(entries, [])

    def test_remove_is_noop_when_not_registered(self):
        """remove_project_from_registry doesn't error if project isn't in registry."""
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "myproject"
            project.mkdir()
            # Never added — remove should be silent
            setup_module.remove_project_from_registry(project, registry)

    def test_remove_is_noop_when_registry_missing(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "myproject"
            self.assertFalse(registry.exists())
            setup_module.remove_project_from_registry(project, registry)  # must not raise

    def test_remove_leaves_other_entries_intact(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            proj_a = Path(td) / "a"
            proj_b = Path(td) / "b"
            proj_a.mkdir(); proj_b.mkdir()
            setup_module.add_project_to_registry(proj_a, registry)
            setup_module.add_project_to_registry(proj_b, registry)
            setup_module.remove_project_from_registry(proj_a, registry)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["path"], str(proj_b.resolve()))

    def test_registry_json_is_extensible(self):
        """Extra keys in a project entry survive a remove/add cycle."""
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "known-projects.json"
            proj_a = Path(td) / "a"
            proj_b = Path(td) / "b"
            proj_a.mkdir(); proj_b.mkdir()
            # Write a registry with extra metadata on proj_a
            import json as _json
            registry.write_text(_json.dumps({
                "projects": [
                    {"path": str(proj_a.resolve()), "last_install": "2026-04-24"},
                    {"path": str(proj_b.resolve())},
                ]
            }) + "\n")
            # Remove proj_b — proj_a's extra metadata must survive
            setup_module.remove_project_from_registry(proj_b, registry)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].get("last_install"), "2026-04-24")


class PruneTests(unittest.TestCase):
    """Tests for tamago prune (Slice F)."""

    def _make_cache_dir(self, cache_root: Path, name: str) -> Path:
        """Create a fake cached skill repo dir."""
        d = cache_root / name
        d.mkdir(parents=True)
        return d

    def _make_machine_toml(
        self,
        project_root: Path,
        skill_cache: dict[str, str],
    ) -> None:
        machine_toml = project_root / ".tamago" / "machine.toml"
        machine_toml.parent.mkdir(parents=True, exist_ok=True)
        setup_module.write_machine_toml(
            machine_toml,
            setup_module.MachineToml(skill_cache=skill_cache),
        )

    def test_prune_removes_unreferenced_cache_dir(self):
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            orphan = self._make_cache_dir(cache_root, "orphan")

            result = setup_module.prune(
                cache_root=cache_root,
                registry_path=registry,
                dry_run=False,
            )
            self.assertEqual(result, 0)
            self.assertFalse(orphan.exists())

    def test_prune_keeps_referenced_cache_dir(self):
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "myproject"

            kept = self._make_cache_dir(cache_root, "kept")
            self._make_machine_toml(project, {"my-skill": str(kept)})
            setup_module.add_project_to_registry(project, registry)

            result = setup_module.prune(
                cache_root=cache_root,
                registry_path=registry,
                dry_run=False,
            )
            self.assertEqual(result, 0)
            self.assertTrue(kept.exists())

    def test_prune_multi_project_shared_cache(self):
        """A cache dir referenced by project B must survive even if project A doesn't use it."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            dir_x = self._make_cache_dir(cache_root, "x")  # used by proj A only
            dir_y = self._make_cache_dir(cache_root, "y")  # used by both A and B
            dir_z = self._make_cache_dir(cache_root, "z")  # used by proj B only
            self._make_cache_dir(cache_root, "orphan")    # used by nobody

            proj_a = Path(td) / "proj_a"
            proj_b = Path(td) / "proj_b"
            self._make_machine_toml(proj_a, {"skill1": str(dir_x), "skill2": str(dir_y)})
            self._make_machine_toml(proj_b, {"skill2": str(dir_y), "skill3": str(dir_z)})
            setup_module.add_project_to_registry(proj_a, registry)
            setup_module.add_project_to_registry(proj_b, registry)

            result = setup_module.prune(
                cache_root=cache_root,
                registry_path=registry,
                dry_run=False,
            )
            self.assertEqual(result, 0)
            self.assertTrue(dir_x.exists())
            self.assertTrue(dir_y.exists())
            self.assertTrue(dir_z.exists())
            self.assertFalse((cache_root / "orphan").exists())

    def test_dry_run_does_not_delete(self):
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            orphan = self._make_cache_dir(cache_root, "orphan")

            result = setup_module.prune(
                cache_root=cache_root,
                registry_path=registry,
                dry_run=True,
            )
            self.assertEqual(result, 0)
            self.assertTrue(orphan.exists())  # must still be there

    def test_empty_registry_prunes_all_cache_dirs(self):
        """With no known projects, every cache dir is orphaned."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            a = self._make_cache_dir(cache_root, "a")
            b = self._make_cache_dir(cache_root, "b")

            setup_module.prune(cache_root=cache_root, registry_path=registry)
            self.assertFalse(a.exists())
            self.assertFalse(b.exists())

    def test_absent_cache_root_returns_0(self):
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "nonexistent-cache"
            registry = Path(td) / "known-projects.json"
            result = setup_module.prune(cache_root=cache_root, registry_path=registry)
            self.assertEqual(result, 0)

    def test_stale_registry_entry_cleaned_after_prune(self):
        """Project dir gone → registry entry removed by prune."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            ghost_project = Path(td) / "ghost"
            ghost_project.mkdir()
            setup_module.add_project_to_registry(ghost_project, registry)
            ghost_project.rmdir()  # simulate deleted project

            self._make_cache_dir(cache_root, "orphan")

            setup_module.prune(cache_root=cache_root, registry_path=registry)

            entries = setup_module.read_known_projects(registry)
            self.assertEqual(entries, [])

    def test_stale_entry_not_cleaned_in_dry_run(self):
        """--dry-run must not modify the registry."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            ghost_project = Path(td) / "ghost"
            ghost_project.mkdir()
            setup_module.add_project_to_registry(ghost_project, registry)
            ghost_project.rmdir()

            setup_module.prune(cache_root=cache_root, registry_path=registry, dry_run=True)

            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 1)  # still there

    def test_project_with_no_machine_toml_aborts_prune(self):
        """A registered project that exists but has no machine.toml (pre-Slice-E install)
        causes prune to abort with rc=1 rather than risk deleting referenced caches."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"

            orphan = self._make_cache_dir(cache_root, "orphan")

            project = Path(td) / "project_no_toml"
            project.mkdir()
            setup_module.add_project_to_registry(project, registry)
            # No machine.toml written — simulates pre-Slice-E install

            result = setup_module.prune(cache_root=cache_root, registry_path=registry)

            # Must abort (rc=1) and NOT delete the cache dir
            self.assertEqual(result, 1)
            self.assertTrue(orphan.exists())

            # Registry entry must NOT be removed (project still exists)
            entries = setup_module.read_known_projects(registry)
            self.assertEqual(len(entries), 1)


class TomlStringTests(unittest.TestCase):
    """Tests for _toml_string — TOML basic string escaping."""

    def test_backslash_escaped(self):
        result = setup_module._toml_string(r"C:\Users\glin")
        self.assertIn("\\\\", result)

    def test_double_quote_escaped(self):
        result = setup_module._toml_string('say "hi"')
        self.assertIn('\\"', result)

    def test_newline_escaped(self):
        result = setup_module._toml_string("line1\nline2")
        self.assertIn("\\n", result)
        self.assertNotIn("\n", result)

    def test_carriage_return_escaped(self):
        result = setup_module._toml_string("a\rb")
        self.assertIn("\\r", result)

    def test_tab_escaped(self):
        result = setup_module._toml_string("a\tb")
        self.assertIn("\\t", result)

    def test_control_char_escaped_as_unicode(self):
        # U+0001 (SOH) — must become 
        result = setup_module._toml_string("\x01")
        self.assertIn("\\u0001", result)

    def test_normal_path_unchanged(self):
        s = "/Users/glin/.tamago/repo-cache/abc123"
        result = setup_module._toml_string(s)
        self.assertEqual(result, f'"{s}"')

    def test_dotted_agent_name_unchanged(self):
        result = setup_module._toml_string("hammer.mei")
        self.assertEqual(result, '"hammer.mei"')

    def test_roundtrip_via_tomllib(self):
        import tomllib as _toml
        tricky = 'path\\with "quotes"\nand newline'
        toml_text = f"key = {setup_module._toml_string(tricky)}\n"
        parsed = _toml.loads(toml_text)
        self.assertEqual(parsed["key"], tricky)


class PatchSettingsDanglingSymlinkTests(unittest.TestCase):
    """Tests for patch_settings dangling-symlink handling."""

    def test_dangling_symlink_is_replaced_gracefully(self):
        """A broken symlink at path is unlinked and treated as empty settings."""
        import json as _json
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "settings.json"
            manifest = Path(td) / "manifest.json"
            # Create a symlink pointing to a non-existent file
            target.symlink_to(Path(td) / "nonexistent.json")
            self.assertTrue(target.is_symlink())
            self.assertFalse(target.exists())  # dangling

            # Should not raise
            setup_module.patch_settings(
                target,
                {"SessionStart": ["cmd --init"]},
                [],
                None,
                manifest,
            )

            # Symlink must be gone; real file written with injected hook
            self.assertFalse(target.is_symlink())
            self.assertTrue(target.exists())
            data = _json.loads(target.read_text())
            cmds = [
                h["command"]
                for m in data["hooks"]["SessionStart"]
                for h in m.get("hooks", [])
            ]
            self.assertIn("cmd --init", cmds)


class PruneResolvePathTests(unittest.TestCase):
    """Tests that prune() and _write_install_machine_toml() agree on path normalization."""

    def test_cache_dir_stored_resolved_matches_prune_lookup(self):
        """cache_dir.resolve() stored in machine.toml matches prune()'s resolution."""
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "repo-cache"
            registry = Path(td) / "known-projects.json"
            project = Path(td) / "project"

            # Create a symlink-based path to the cache root to simulate macOS /private
            real_cache = Path(td) / "real-cache"
            real_cache.mkdir()
            cache_root.symlink_to(real_cache)

            kept = cache_root / "abc123"
            kept.mkdir()

            # Manually write a machine.toml using the resolved path (as
            # _write_install_machine_toml now does)
            machine_toml = project / ".tamago" / "machine.toml"
            machine_toml.parent.mkdir(parents=True)
            setup_module.write_machine_toml(
                machine_toml,
                setup_module.MachineToml(
                    skill_cache={"my-skill": str(kept.resolve())}
                ),
            )
            setup_module.add_project_to_registry(project, registry)

            result = setup_module.prune(
                cache_root=cache_root,
                registry_path=registry,
            )
            self.assertEqual(result, 0)
            # 'kept' must survive because it is referenced
            self.assertTrue(kept.exists() or real_cache.joinpath("abc123").exists())


class InstallFromConfRegistryTests(unittest.TestCase):
    """Tests that install_from_conf registers/unregisters projects (Slice F)."""

    def test_install_registers_project(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")
            project_root = Path(td) / "project"
            registry = Path(td) / "known-projects.json"

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                    registry_path=registry,
                )

            entries = setup_module.read_known_projects(registry)
            paths = [e["path"] for e in entries]
            self.assertIn(str(project_root.resolve()), paths)

    def test_uninstall_unregisters_project(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")
            project_root = Path(td) / "project"
            registry = Path(td) / "known-projects.json"

            # Pre-register
            setup_module.add_project_to_registry(project_root, registry)

            with (
                mock.patch.object(setup_module, "setup", return_value=0),
                mock.patch.object(setup_module, "setup_external_skills", return_value=0),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.UNINSTALL,
                    Path(td) / "source",
                    project_root,
                    registry_path=registry,
                )

            entries = setup_module.read_known_projects(registry)
            self.assertEqual(entries, [])

    def test_failed_setup_does_not_register(self):
        """If setup() fails, project must NOT be registered."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text("")
            project_root = Path(td) / "project"
            registry = Path(td) / "known-projects.json"

            with (
                mock.patch.object(setup_module, "setup", return_value=1),
                mock.patch.object(setup_module, "pull_repo"),
            ):
                result = setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                    registry_path=registry,
                )

            self.assertEqual(result, 1)
            self.assertFalse(registry.exists())


class GlobalAgentScopeTests(unittest.TestCase):
    """Tests for scope='global' in [[agents]] — agents installed to ~/.claude/agents/."""

    def _make_source(self, root: Path, agent_names: list[str]) -> Path:
        source = root / "tamago"
        (source / "agents").mkdir(parents=True)
        for name in agent_names:
            (source / "agents" / f"{name}.md").write_text(f"# {name}\n")
        (source / "skills").mkdir(parents=True)
        (source / "settings" / "opencode" / "plugins").mkdir(parents=True)
        return source

    def test_global_agent_installed_to_home_not_project(self):
        """scope='global' agent goes to ~/.claude/agents/, not the project dir."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["code-reviewer"])
            project = root / "project"
            home_agents = root / "home" / ".claude" / "agents"

            with mock.patch.object(
                setup_module, "Path",
                side_effect=lambda x: Path(str(x).replace("~", str(root / "home"))),
            ):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    global_agents={"code-reviewer"},
                )

            # Should be in home dir, NOT in project dir
            self.assertTrue((home_agents / "code-reviewer.md").is_symlink())
            self.assertFalse((project / ".claude" / "agents" / "code-reviewer.md").exists())

    def test_global_agent_memory_at_home_not_project(self):
        """Memory dir for a global agent goes to ~/.claude/agent-memory/, not project level."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])
            profile = root / "hammer.mei-profile"
            (profile / "agents" / "memory" / "hammer.mei").mkdir(parents=True)

            project = root / "project"
            home_root = root / "home"
            orig_expanduser = Path.expanduser

            def fake_expanduser(self):
                s = str(self)
                if s.startswith("~"):
                    return Path(str(home_root) + s[1:])
                return orig_expanduser(self)

            with mock.patch.object(Path, "expanduser", fake_expanduser):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    global_agents={"hammer.mei"},
                )

            home_mem = home_root / ".claude" / "agent-memory" / "hammer.mei"
            project_mem = project / ".claude" / "agent-memory" / "hammer.mei"

            # Memory should be at home (global), NOT at project level
            self.assertTrue(home_mem.is_symlink())
            self.assertFalse(project_mem.exists())

    def test_install_from_conf_builds_global_agents_set(self):
        """install_from_conf computes global_agents from scope='global' entries."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text(
                '[[agents]]\nname = "hammer.mei"\nsource = "profile"\nscope = "global"\n'
            )
            project_root = Path(td) / "project"
            registry = Path(td) / "registry.json"

            captured: dict = {}

            def fake_setup(*args, **kwargs):
                captured["global_agents"] = kwargs.get("global_agents")
                return 0

            with (
                mock.patch.object(setup_module, "setup", side_effect=fake_setup),
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                    registry_path=registry,
                )

            self.assertEqual(captured["global_agents"], {"hammer.mei"})

    def test_disabled_agent_not_in_global_agents(self):
        """A disabled agent (disable=true) must not appear in global_agents even if scope=global."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text(
                '[[agents]]\nname = "hammer.mei"\nscope = "global"\ndisable = true\n'
            )
            project_root = Path(td) / "project"
            registry = Path(td) / "registry.json"

            captured: dict = {}

            def fake_setup(*args, **kwargs):
                captured["global_agents"] = kwargs.get("global_agents")
                captured["disabled_agents"] = kwargs.get("disabled_agents")
                return 0

            with (
                mock.patch.object(setup_module, "setup", side_effect=fake_setup),
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                    registry_path=registry,
                )

            self.assertNotIn("hammer.mei", captured.get("global_agents", set()))
            self.assertIn("hammer.mei", captured.get("disabled_agents", set()))

    def test_global_persona_agent_merged_to_home_dir(self):
        """A persona agent with scope='global' is generated in ~/.claude/agents/."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])
            (source / "docs").mkdir(parents=True)
            (source / "docs" / "tamago-agent-base.md").write_text("# Base\n")

            profile = root / "hammer.mei-profile"
            (profile / "agents").mkdir(parents=True)
            (profile / "agents" / "hammer.mei.persona.md").write_text(
                "---\nname: hammer.mei\n---\n# Persona\n"
            )

            project = root / "project"
            home_claude_agents = root / "home" / ".claude" / "agents"
            home_opencode_agents = root / "home" / ".opencode" / "agents"

            # Patch the home dir expansion inside setup_agents
            orig_expanduser = Path.expanduser

            def fake_expanduser(self):
                s = str(self)
                if s.startswith("~"):
                    return Path(str(root / "home") + s[1:])
                return orig_expanduser(self)

            with mock.patch.object(Path, "expanduser", fake_expanduser):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    global_agents={"hammer.mei"},
                )

            # Generated in home dir
            self.assertTrue((home_claude_agents / "hammer.mei.md").exists())
            self.assertIn(
                setup_module.GENERATED_HEADER_MARKER,
                (home_claude_agents / "hammer.mei.md").read_text()[:300],
            )
            # NOT in project dir
            self.assertFalse((project / ".claude" / "agents" / "hammer.mei.md").exists())


HEALTH_CHECK_SH = Path(__file__).with_name("scripts") / "health-check.sh"


def _run_health_check(project_dir: Path, tamago_dir: Path, home_dir: Path,
                      profile_dir: Path | None = None,
                      extra_env: dict | None = None) -> dict:
    """Run health-check.sh --json in a subprocess with a faked environment.

    Returns the parsed JSON output dict.
    """
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["ASSISTANT_SETUP_REPO"] = str(tamago_dir)
    env.pop("PROFILE_REPO", None)
    env.pop("MEMORY_SYNC", None)
    if profile_dir:
        env["PROFILE_REPO"] = str(profile_dir)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        ["bash", str(HEALTH_CHECK_SH), "--json", "--project", str(project_dir)],
        capture_output=True, text=True, env=env,
    )
    import json
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"health-check.sh did not emit valid JSON.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )


def _skill_result(data: dict, name: str) -> str | None:
    """Return the status ('pass', 'fail', 'warn') for the named skill check, or None if not found."""
    for r in data.get("results", []):
        if name in r.get("name", ""):
            return r["status"]
    return None


class HealthCheckSkillScopeTests(unittest.TestCase):
    """Verify health-check.sh correctly locates skills based on source + agent scope.

    Scope rules under test:
      1. Tamago built-in skills always at ~/.claude/skills/ (even with project-scoped agent)
      2. Profile skills follow agent scope:
           global agent  → ~/.claude/skills/
           project agent → project/.claude/skills/
      3. explicit scope="project" in [[skills]] → project level regardless of source
    """

    def _setup_env(self, root: Path, tamago_skills: list[str], profile_skills: list[str],
                   agent_scope: str = "project", agent_name: str = "test-agent",
                   project_scoped_skills: list[str] | None = None) -> tuple[Path, Path, Path, Path]:
        """Build a fake tamago/profile/home/project tree and return (tamago, profile, home, project)."""
        tamago = root / "tamago"
        profile = root / "profile"
        home = root / "home"
        project = root / "project"

        # tamago skill dirs
        for name in tamago_skills:
            (tamago / "skills" / name).mkdir(parents=True)

        # profile skills + minimal settings.json with agent name
        (profile / "skills").mkdir(parents=True)
        for name in profile_skills:
            (profile / "skills" / name).mkdir(parents=True)
        (profile / "settings" / "claude").mkdir(parents=True)
        import json
        (profile / "settings" / "claude" / "settings.json").write_text(
            json.dumps({"agent": agent_name})
        )

        # tamago.conf in project
        (project / ".tamago").mkdir(parents=True)
        scoped_lines = ""
        if project_scoped_skills:
            for s in project_scoped_skills:
                scoped_lines += f'\n[[skills]]\nname = "{s}"\nscope = "project"\n'
        (project / ".tamago" / "tamago.conf").write_text(
            f'[[agents]]\nname = "{agent_name}"\nscope = "{agent_scope}"\n{scoped_lines}'
        )
        # machine.env so health check knows the profile
        (project / ".tamago" / "machine.env").write_text(
            f'PROFILE_REPO="{profile}"\n'
        )
        # machine.toml (avoid warn)
        (project / ".tamago" / "machine.toml").write_text("")

        # minimal project settings to avoid unrelated failures
        (project / ".claude").mkdir(parents=True)
        (project / ".opencode").mkdir(parents=True)
        (project / ".claude" / "settings.json").symlink_to(
            tamago / "settings" / "claude" / "settings.json"
        )
        (project / ".opencode" / "opencode.json").symlink_to(
            tamago / "settings" / "opencode" / "opencode.json"
        )
        (tamago / "settings" / "claude").mkdir(parents=True)
        (tamago / "settings" / "claude" / "settings.json").write_text("{}")
        (tamago / "settings" / "opencode").mkdir(parents=True)
        (tamago / "settings" / "opencode" / "opencode.json").write_text("{}")
        # fake tamago-manifest so global settings check passes
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".tamago-manifest.json").write_text("{}")
        (home / ".opencode").mkdir(parents=True)
        (home / ".opencode" / ".tamago-manifest.json").write_text("{}")
        # profile memory dir
        (profile / "agents" / "memory" / agent_name).mkdir(parents=True)
        (project / ".claude" / "agent-memory").mkdir(parents=True)

        return tamago, profile, home, project

    def _install_skill_global(self, home: Path, skill_name: str, source_dir: Path) -> None:
        skills_dir = home / ".claude" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / skill_name).symlink_to(source_dir)

    def _install_skill_project(self, project: Path, skill_name: str, source_dir: Path) -> None:
        skills_dir = project / ".claude" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / skill_name).symlink_to(source_dir)

    def test_tamago_builtin_checked_globally_with_project_scoped_agent(self):
        """Health check looks in ~/.claude/skills/ for tamago built-ins even when agent is project-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tamago, profile, home, project = self._setup_env(
                root, tamago_skills=["tts"], profile_skills=[], agent_scope="project"
            )
            # Correctly installed: tamago built-in at global scope
            self._install_skill_global(home, "tts", tamago / "skills" / "tts")

            data = _run_health_check(project, tamago, home, profile)
            self.assertEqual(_skill_result(data, "tts"), "pass",
                             f"Expected tts to pass; full results: {data['results']}")

    def test_tamago_builtin_fails_if_only_at_project_level(self):
        """Health check fails for tamago built-ins placed at project level (wrong location)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tamago, profile, home, project = self._setup_env(
                root, tamago_skills=["tts"], profile_skills=[], agent_scope="project"
            )
            # Wrongly installed at project level — should be at global
            self._install_skill_project(project, "tts", tamago / "skills" / "tts")

            data = _run_health_check(project, tamago, home, profile)
            self.assertEqual(_skill_result(data, "tts"), "fail",
                             f"Expected tts to fail (wrong location); full results: {data['results']}")

    def test_profile_skill_checked_at_project_level_with_project_scoped_agent(self):
        """Health check looks in project/.claude/skills/ for profile skills when agent is project-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tamago, profile, home, project = self._setup_env(
                root, tamago_skills=[], profile_skills=["agent-skill"], agent_scope="project"
            )
            # Correctly installed at project level
            self._install_skill_project(project, "agent-skill", profile / "skills" / "agent-skill")

            data = _run_health_check(project, tamago, home, profile)
            self.assertEqual(_skill_result(data, "agent-skill"), "pass",
                             f"Expected agent-skill to pass; full results: {data['results']}")

    def test_profile_skill_fails_if_at_global_level_with_project_scoped_agent(self):
        """Health check fails for profile skills at global when agent is project-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tamago, profile, home, project = self._setup_env(
                root, tamago_skills=[], profile_skills=["agent-skill"], agent_scope="project"
            )
            # Wrongly installed at global — should be at project
            self._install_skill_global(home, "agent-skill", profile / "skills" / "agent-skill")

            data = _run_health_check(project, tamago, home, profile)
            self.assertEqual(_skill_result(data, "agent-skill"), "fail",
                             f"Expected agent-skill to fail (wrong location); full results: {data['results']}")

    def test_profile_skill_checked_globally_with_global_agent(self):
        """Health check looks in ~/.claude/skills/ for profile skills when agent is global-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tamago, profile, home, project = self._setup_env(
                root, tamago_skills=[], profile_skills=["agent-skill"], agent_scope="global",
                agent_name="test-agent",
            )
            # Need agent file at global location for global agent check
            (home / ".claude" / "agents").mkdir(parents=True)
            agent_md = home / ".claude" / "agents" / "test-agent.md"
            agent_md.write_text("<!-- TAMAGO GENERATED -->\n")
            (home / ".opencode" / "agents").mkdir(parents=True)
            (home / ".opencode" / "agents" / "test-agent.md").write_text("<!-- TAMAGO GENERATED -->\n")
            (home / ".claude" / "agent-memory").mkdir(parents=True)
            (home / ".claude" / "agent-memory" / "test-agent").symlink_to(
                profile / "agents" / "memory" / "test-agent"
            )

            self._install_skill_global(home, "agent-skill", profile / "skills" / "agent-skill")

            data = _run_health_check(project, tamago, home, profile)
            self.assertEqual(_skill_result(data, "agent-skill"), "pass",
                             f"Expected agent-skill to pass; full results: {data['results']}")

    def test_explicit_project_scope_override_for_tamago_builtin(self):
        """scope='project' in [[skills]] makes health check look at project dir for tamago built-ins."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tamago, profile, home, project = self._setup_env(
                root, tamago_skills=["tts"], profile_skills=[], agent_scope="project",
                project_scoped_skills=["tts"],
            )
            # Correctly installed at project level due to explicit override
            self._install_skill_project(project, "tts", tamago / "skills" / "tts")

            data = _run_health_check(project, tamago, home, profile)
            self.assertEqual(_skill_result(data, "tts"), "pass",
                             f"Expected tts to pass at project level; full results: {data['results']}")


class SkillScopeRoutingTests(unittest.TestCase):
    """Tests for setup_skills scope routing: tamago built-ins vs profile skills vs agent scope."""

    def _make_source(self, root: Path, tamago_skills: list[str]) -> Path:
        source = root / "tamago"
        (source / "skills").mkdir(parents=True)  # always create skills dir
        for name in tamago_skills:
            (source / "skills" / name).mkdir(parents=True)
        return source

    def _make_profile(self, root: Path, profile_skills: list[str]) -> Path:
        profile = root / "profile"
        (profile / "skills").mkdir(parents=True)  # always create skills dir
        for name in profile_skills:
            (profile / "skills" / name).mkdir(parents=True)
        return profile

    def _fake_expanduser(self, root: Path):
        """Return a fake_expanduser that redirects ~ to root/home."""
        orig = Path.expanduser

        def fake(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(root / "home") + s[1:])
            return orig(self)

        return fake

    def test_tamago_builtin_goes_global_when_agent_is_project_scoped(self):
        """Tamago built-in skills always go to ~/.claude/skills/ even with a project-scoped agent."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["tts", "daily-briefing"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    has_global_agent=False,  # all agents are project-scoped
                )

            self.assertTrue((home_claude_skills / "tts").is_symlink())
            self.assertTrue((home_claude_skills / "daily-briefing").is_symlink())
            self.assertFalse((project / ".claude" / "skills" / "tts").exists())
            self.assertFalse((project / ".claude" / "skills" / "daily-briefing").exists())

    def test_tamago_builtin_goes_global_when_agent_is_global_scoped(self):
        """Tamago built-in skills go to ~/.claude/skills/ when agent is global-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["tts"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    has_global_agent=True,
                )

            self.assertTrue((home_claude_skills / "tts").is_symlink())
            self.assertFalse((project / ".claude" / "skills" / "tts").exists())

    def test_profile_skill_goes_global_when_agent_is_global_scoped(self):
        """Profile skills go to ~/.claude/skills/ when the agent is global-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])
            profile = self._make_profile(root, ["agent-skill"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    has_global_agent=True,
                )

            self.assertTrue((home_claude_skills / "agent-skill").is_symlink())
            self.assertFalse((project / ".claude" / "skills" / "agent-skill").exists())

    def test_profile_skill_goes_project_when_agent_is_project_scoped(self):
        """Profile skills go to project/.claude/skills/ when the agent is project-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])
            profile = self._make_profile(root, ["agent-skill"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    has_global_agent=False,
                )

            self.assertTrue((project / ".claude" / "skills" / "agent-skill").is_symlink())
            self.assertFalse((home_claude_skills / "agent-skill").exists())

    def test_both_sources_with_project_scoped_agent(self):
        """Tamago built-ins global, profile skills project when agent is project-scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["builtin-skill"])
            profile = self._make_profile(root, ["agent-skill"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    has_global_agent=False,
                )

            # Tamago built-in → global
            self.assertTrue((home_claude_skills / "builtin-skill").is_symlink())
            self.assertFalse((project / ".claude" / "skills" / "builtin-skill").exists())
            # Profile skill → project
            self.assertTrue((project / ".claude" / "skills" / "agent-skill").is_symlink())
            self.assertFalse((home_claude_skills / "agent-skill").exists())

    def test_explicit_project_scope_overrides_tamago_builtin_default(self):
        """scope="project" in [[skills]] forces a tamago built-in to project level."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["tts"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    project_scoped_skills={"tts"},
                    has_global_agent=False,
                )

            self.assertTrue((project / ".claude" / "skills" / "tts").is_symlink())
            self.assertFalse((home_claude_skills / "tts").exists())

    def test_explicit_project_scope_overrides_profile_skill_on_global_agent(self):
        """scope="project" in [[skills]] forces a profile skill to project even for global agent."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])
            profile = self._make_profile(root, ["agent-skill"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    project_scoped_skills={"agent-skill"},
                    has_global_agent=True,
                )

            self.assertTrue((project / ".claude" / "skills" / "agent-skill").is_symlink())
            self.assertFalse((home_claude_skills / "agent-skill").exists())

    def test_profile_shadows_tamago_builtin_project_agent_goes_project(self):
        """When profile shadows a tamago built-in, profile rules apply: project agent → project."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["tts"])      # tamago built-in
            profile = self._make_profile(root, ["tts"])    # profile shadows it
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    has_global_agent=False,
                )

            # Profile version used, profile rules apply → project scope
            self.assertTrue((project / ".claude" / "skills" / "tts").is_symlink())
            self.assertFalse((home_claude_skills / "tts").exists())

    def test_profile_shadows_tamago_builtin_global_agent_goes_global(self):
        """When profile shadows a tamago built-in, profile rules apply: global agent → global."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["tts"])      # tamago built-in
            profile = self._make_profile(root, ["tts"])    # profile shadows it
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    has_global_agent=True,
                )

            # Profile version used, profile rules apply → global scope
            self.assertTrue((home_claude_skills / "tts").is_symlink())
            self.assertFalse((project / ".claude" / "skills" / "tts").exists())


class DisableSkillTests(unittest.TestCase):
    """Tests for disable=true in [[skills]] entries."""

    def _make_source(self, root: Path, skill_names: list[str]) -> Path:
        """Create a tamago source tree with the given skill dirs."""
        source = root / "tamago"
        for name in skill_names:
            (source / "skills" / name).mkdir(parents=True)
        return source

    def test_load_tamago_conf_parses_disable_true_for_skill(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text('[[skills]]\nname = "foo"\ndisable = true\n')
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertEqual(len(conf.skills), 1)
            self.assertTrue(conf.skills[0].disable)

    def test_load_tamago_conf_disable_defaults_to_false(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text('[[skills]]\nname = "foo"\n')
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertFalse(conf.skills[0].disable)

    def test_disabled_skill_is_not_symlinked(self):
        """A skill in disabled_skills must not be symlinked on install."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["keep-skill", "skip-skill"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            orig_expanduser = Path.expanduser

            def fake_expanduser(self):
                s = str(self)
                if s.startswith("~"):
                    return Path(str(root / "home") + s[1:])
                return orig_expanduser(self)

            with mock.patch.object(Path, "expanduser", fake_expanduser):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    disabled_skills={"skip-skill"},
                )

            # Built-in skills always go to global ~/.claude/skills/
            self.assertTrue((home_claude_skills / "keep-skill").is_symlink())
            self.assertFalse((home_claude_skills / "skip-skill").exists())
            self.assertFalse((root / "home" / ".opencode" / "skills" / "skip-skill").exists())

    def test_existing_symlink_removed_when_skill_disabled(self):
        """If a skill is disabled after having been installed, the old symlink is removed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["my-skill"])
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"

            orig_expanduser = Path.expanduser

            def fake_expanduser(self):
                s = str(self)
                if s.startswith("~"):
                    return Path(str(root / "home") + s[1:])
                return orig_expanduser(self)

            with mock.patch.object(Path, "expanduser", fake_expanduser):
                # First install without disable
                setup_module.setup_skills(setup_module.Operation.INSTALL, source, project)
                self.assertTrue((home_claude_skills / "my-skill").is_symlink())

                # Re-install with disable
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    setup_module.setup_skills(
                        setup_module.Operation.INSTALL,
                        source,
                        project,
                        disabled_skills={"my-skill"},
                    )
            self.assertFalse((home_claude_skills / "my-skill").exists())
            self.assertFalse((root / "home" / ".opencode" / "skills" / "my-skill").exists())
            self.assertIn("disabled", out.getvalue())

    def test_disabled_external_skill_removes_symlink_and_skips_clone(self):
        """Disabled URL skill: remove existing symlink, do not clone."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            skill_dir = root / "cached-skill"
            skill_dir.mkdir()

            # Pre-create symlinks (simulating a previously-installed URL skill)
            for rel in (".claude/skills", ".opencode/skills"):
                sr = project / rel
                sr.mkdir(parents=True)
                (sr / "my-skill").symlink_to(skill_dir)

            skill = setup_module.SkillEntry(
                name="my-skill",
                source="https://example.com/my-skill.git",
                scope="project",
                disable=True,
            )
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(setup_module, "_resolve_external_skill_dir") as mock_resolve,
            ):
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )

            self.assertEqual(rc, 0)
            mock_resolve.assert_not_called()
            self.assertFalse((project / ".claude" / "skills" / "my-skill").exists())
            self.assertFalse((project / ".opencode" / "skills" / "my-skill").exists())
            self.assertIn("disabled", out.getvalue())

    def test_disabled_external_skill_not_cloned_when_no_existing_symlink(self):
        """Disabled URL skill with no pre-existing symlink: skip silently, don't clone."""
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            skill = setup_module.SkillEntry(
                name="my-skill",
                source="https://example.com/my-skill.git",
                scope="project",
                disable=True,
            )
            with mock.patch.object(setup_module, "_resolve_external_skill_dir") as mock_resolve:
                rc = setup_module.setup_external_skills(
                    setup_module.Operation.INSTALL, [skill], project
                )
            self.assertEqual(rc, 0)
            mock_resolve.assert_not_called()


class DisableAgentTests(unittest.TestCase):
    """Tests for disable=true in [[agents]] entries."""

    def _make_source(self, root: Path, agent_names: list[str]) -> Path:
        """Create a tamago source tree with given generic agent .md files."""
        source = root / "tamago"
        agents_dir = source / "agents"
        agents_dir.mkdir(parents=True)
        for name in agent_names:
            (agents_dir / f"{name}.md").write_text(f"# {name}\n")
        # Need skills dir too (setup_skills requires it)
        (source / "skills").mkdir(parents=True)
        # Need opencode plugins dir
        (source / "settings" / "opencode" / "plugins").mkdir(parents=True)
        return source

    def test_load_tamago_conf_parses_disable_true_for_agent(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text('[[agents]]\nname = "hammer.mei"\ndisable = true\n')
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertEqual(len(conf.agents), 1)
            self.assertTrue(conf.agents[0].disable)

    def test_load_tamago_conf_agent_disable_defaults_to_false(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tamago.conf"
            p.write_text('[[agents]]\nname = "hammer.mei"\n')
            conf = setup_module.load_tamago_conf(p)
            self.assertIsNotNone(conf)
            self.assertFalse(conf.agents[0].disable)

    def test_disabled_tamago_agent_not_symlinked(self):
        """A tamago built-in agent in disabled_agents must not be symlinked."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["code-reviewer", "plan"])
            project = root / "project"

            setup_module.setup_agents(
                setup_module.Operation.INSTALL,
                source,
                project,
                disabled_agents={"code-reviewer"},
            )

            self.assertFalse((project / ".claude" / "agents" / "code-reviewer.md").exists())
            self.assertFalse((project / ".opencode" / "agents" / "code-reviewer.md").exists())
            self.assertTrue((project / ".claude" / "agents" / "plan.md").is_symlink())

    def test_existing_symlink_removed_for_disabled_tamago_agent(self):
        """On re-install with disable, the old agent symlink is removed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, ["code-reviewer"])
            project = root / "project"

            # First install without disable
            setup_module.setup_agents(setup_module.Operation.INSTALL, source, project)
            self.assertTrue((project / ".claude" / "agents" / "code-reviewer.md").is_symlink())

            # Re-install with disable
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    disabled_agents={"code-reviewer"},
                )
            self.assertFalse((project / ".claude" / "agents" / "code-reviewer.md").exists())
            self.assertFalse((project / ".opencode" / "agents" / "code-reviewer.md").exists())
            self.assertIn("disabled", out.getvalue())

    def test_disabled_persona_agent_not_generated(self):
        """A persona agent in disabled_agents must not have its .md generated."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])

            # Minimal tamago-agent-base.md required by _merge_agent
            (source / "docs").mkdir(parents=True)
            (source / "docs" / "tamago-agent-base.md").write_text("# Base\n")

            # Profile with persona file
            profile = root / "hammer.mei-profile"
            (profile / "agents").mkdir(parents=True)
            (profile / "agents" / "hammer.mei.persona.md").write_text(
                "---\nname: hammer.mei\n---\n# Persona\n"
            )

            project = root / "project"
            setup_module.setup_agents(
                setup_module.Operation.INSTALL,
                source,
                project,
                profile_root=profile,
                disabled_agents={"hammer.mei"},
            )

            self.assertFalse((project / ".claude" / "agents" / "hammer.mei.md").exists())
            self.assertFalse((project / ".opencode" / "agents" / "hammer.mei.md").exists())

    def test_generated_file_removed_when_persona_agent_disabled(self):
        """On re-install with persona disabled, the previously-generated .md is removed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])
            (source / "docs").mkdir(parents=True)
            (source / "docs" / "tamago-agent-base.md").write_text("# Base\n")

            profile = root / "hammer.mei-profile"
            (profile / "agents").mkdir(parents=True)
            (profile / "agents" / "hammer.mei.persona.md").write_text(
                "---\nname: hammer.mei\n---\n# Persona\n"
            )

            project = root / "project"

            # First install (generates the file)
            setup_module.setup_agents(
                setup_module.Operation.INSTALL, source, project, profile_root=profile
            )
            generated = project / ".claude" / "agents" / "hammer.mei.md"
            self.assertTrue(generated.exists())
            self.assertIn(setup_module.GENERATED_HEADER_MARKER, generated.read_text()[:300])

            # Re-install with disable
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source,
                    project,
                    profile_root=profile,
                    disabled_agents={"hammer.mei"},
                )
            self.assertFalse(generated.exists())
            self.assertFalse((project / ".opencode" / "agents" / "hammer.mei.md").exists())
            self.assertIn("disabled", out.getvalue())

    def test_disabled_agent_memory_not_symlinked(self):
        """Memory dir for a disabled agent must not be symlinked."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root, [])

            # Profile with agent memory dirs
            profile = root / "hammer.mei-profile"
            (profile / "agents" / "memory" / "hammer.mei").mkdir(parents=True)
            (profile / "agents" / "memory" / "other-agent").mkdir(parents=True)

            project = root / "project"
            setup_module.setup_agents(
                setup_module.Operation.INSTALL,
                source,
                project,
                profile_root=profile,
                disabled_agents={"hammer.mei"},
            )

            # hammer.mei memory should NOT be symlinked
            self.assertFalse(
                (project / ".claude" / "agent-memory" / "hammer.mei").exists()
            )
            # other-agent memory SHOULD be symlinked
            self.assertTrue(
                (project / ".claude" / "agent-memory" / "other-agent").is_symlink()
            )

    def test_install_from_conf_builds_disabled_sets(self):
        """install_from_conf extracts disabled names and passes them to setup()."""
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "tamago.conf"
            conf_path.write_text(
                '[[agents]]\nname = "code-reviewer"\ndisable = true\n'
                '[[skills]]\nname = "cmux-markdown"\ndisable = true\n'
            )
            project_root = Path(td) / "project"
            registry = Path(td) / "registry.json"

            captured: dict = {}

            def fake_setup(*args, **kwargs):
                captured["disabled_agents"] = kwargs.get("disabled_agents")
                captured["disabled_skills"] = kwargs.get("disabled_skills")
                return 0

            with (
                mock.patch.object(setup_module, "setup", side_effect=fake_setup),
                mock.patch.object(setup_module, "pull_repo"),
            ):
                setup_module.install_from_conf(
                    conf_path,
                    setup_module.Operation.INSTALL,
                    Path(td) / "source",
                    project_root,
                    registry_path=registry,
                )

            self.assertEqual(captured["disabled_agents"], {"code-reviewer"})
            self.assertEqual(captured["disabled_skills"], {"cmux-markdown"})


# ---------------------------------------------------------------------------
# patch_opencode_global_settings
# ---------------------------------------------------------------------------

class PatchOpencodeGlobalSettingsTests(unittest.TestCase):
    """Tests for patch_opencode_global_settings — INSTALL writes manifest, UNINSTALL removes it."""

    def _fake_expanduser(self, root: Path):
        orig = Path.expanduser
        def fake(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(root / "home") + s[1:])
            return orig(self)
        return fake

    def test_install_writes_opencode_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "tamago"
            (source / "settings" / "opencode").mkdir(parents=True)
            (source / "settings" / "opencode" / "opencode.json").write_text("{}")

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.patch_opencode_global_settings(
                    setup_module.Operation.INSTALL, source
                )

            manifest = root / "home" / ".opencode" / ".tamago-manifest.json"
            self.assertTrue(manifest.exists())

    def test_uninstall_removes_opencode_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "tamago"
            (source / "settings" / "opencode").mkdir(parents=True)
            (source / "settings" / "opencode" / "opencode.json").write_text("{}")

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.patch_opencode_global_settings(
                    setup_module.Operation.INSTALL, source
                )
                manifest = root / "home" / ".opencode" / ".tamago-manifest.json"
                self.assertTrue(manifest.exists())

                setup_module.patch_opencode_global_settings(
                    setup_module.Operation.UNINSTALL, source
                )
            self.assertFalse(manifest.exists())


# ---------------------------------------------------------------------------
# setup_shell_env
# ---------------------------------------------------------------------------

class SetupShellEnvTests(unittest.TestCase):

    def _fake_expanduser(self, root: Path):
        orig = Path.expanduser
        def fake(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(root / "home") + s[1:])
            return orig(self)
        return fake

    def _make_source(self, root: Path) -> Path:
        source = root / "tamago"
        source.mkdir(parents=True)
        return source

    def test_install_adds_env_when_zshrc_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root)
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)  # ensure home dir exists

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_shell_env(setup_module.Operation.INSTALL, source)

            self.assertTrue(zshrc.exists())
            self.assertIn(str(source), zshrc.read_text())

    def test_install_appends_env_when_marker_absent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root)
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)
            zshrc.write_text("export FOO=bar\n")

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_shell_env(setup_module.Operation.INSTALL, source)

            content = zshrc.read_text()
            self.assertIn("ASSISTANT_SETUP_REPO", content)
            self.assertIn("export FOO=bar", content)

    def test_install_updates_existing_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root)
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)
            zshrc.write_text('export ASSISTANT_SETUP_REPO="/old/path"\n')

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_shell_env(setup_module.Operation.INSTALL, source)

            content = zshrc.read_text()
            self.assertIn(str(source), content)
            self.assertNotIn("/old/path", content)

    def test_install_skips_when_marker_matches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root)
            env_line = f'export ASSISTANT_SETUP_REPO="{source}"'
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)
            zshrc.write_text(env_line + "\n")

            out = io.StringIO()
            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                with contextlib.redirect_stdout(out):
                    setup_module.setup_shell_env(setup_module.Operation.INSTALL, source)

            self.assertIn("exists", out.getvalue())
            # Content unchanged
            self.assertEqual(zshrc.read_text(), env_line + "\n")

    def test_install_skips_when_conventional_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = io.StringIO()
            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                with contextlib.redirect_stdout(out):
                    setup_module.setup_shell_env(
                        setup_module.Operation.INSTALL,
                        setup_module.CONVENTIONAL_ROOT,
                    )
            self.assertIn("skip", out.getvalue())

    def test_uninstall_removes_env_line(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root)
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)
            zshrc.write_text(
                "export FOO=bar\n"
                "# Tamago assistant repo\n"
                f'export ASSISTANT_SETUP_REPO="{source}"\n'
            )

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_shell_env(setup_module.Operation.UNINSTALL, source)

            content = zshrc.read_text()
            self.assertNotIn("ASSISTANT_SETUP_REPO", content)
            self.assertNotIn("Tamago assistant repo", content)
            self.assertIn("export FOO=bar", content)

    def test_uninstall_no_op_when_zshrc_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source(root)
            # No zshrc — should not raise
            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_shell_env(setup_module.Operation.UNINSTALL, source)


# ---------------------------------------------------------------------------
# setup_local_bin
# ---------------------------------------------------------------------------

class SetupLocalBinTests(unittest.TestCase):

    def _fake_expanduser(self, root: Path):
        orig = Path.expanduser
        def fake(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(root / "home") + s[1:])
            return orig(self)
        return fake

    def _make_source_with_bin(self, root: Path) -> Path:
        source = root / "tamago"
        tamago_bin = source / "bin" / "tamago"
        tamago_bin.parent.mkdir(parents=True)
        tamago_bin.write_text("#!/bin/bash\n")
        return source

    def test_install_creates_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin(root)
            local_bin = root / "home" / ".local" / "bin"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_local_bin(setup_module.Operation.INSTALL, source)

            self.assertTrue((local_bin / "tamago").is_symlink())

    def test_install_adds_path_to_zshrc(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin(root)
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)
            zshrc.write_text("")

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_local_bin(setup_module.Operation.INSTALL, source)

            self.assertIn(".local/bin", zshrc.read_text())

    def test_install_skips_path_when_already_in_zshrc(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin(root)
            zshrc = root / "home" / ".zshrc"
            zshrc.parent.mkdir(parents=True)
            zshrc.write_text('export PATH="$HOME/.local/bin:$PATH"\n')

            out = io.StringIO()
            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                with contextlib.redirect_stdout(out):
                    setup_module.setup_local_bin(setup_module.Operation.INSTALL, source)

            self.assertIn("exists", out.getvalue())

    def test_install_raises_when_tamago_bin_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "tamago"
            source.mkdir(parents=True)
            # No bin/tamago

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                with self.assertRaises(Exception, msg="tamago bin script not found"):
                    setup_module.setup_local_bin(setup_module.Operation.INSTALL, source)

    def test_install_updates_stale_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin(root)
            local_bin = root / "home" / ".local" / "bin"
            local_bin.mkdir(parents=True)
            old_target = root / "old" / "tamago"
            old_target.parent.mkdir(parents=True)
            old_target.write_text("#!/bin/bash")
            (local_bin / "tamago").symlink_to(old_target)

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_local_bin(setup_module.Operation.INSTALL, source)

            link = local_bin / "tamago"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (source / "bin" / "tamago").resolve())

    def test_uninstall_removes_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin(root)

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_local_bin(setup_module.Operation.INSTALL, source)
                setup_module.setup_local_bin(setup_module.Operation.UNINSTALL, source)

            local_bin = root / "home" / ".local" / "bin"
            self.assertFalse((local_bin / "tamago").exists())


# ---------------------------------------------------------------------------
# setup_git_hooks
# ---------------------------------------------------------------------------

class SetupGitHooksTests(unittest.TestCase):

    def test_install_symlinks_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "tamago"
            hooks_src = source / "git-hooks"
            hooks_src.mkdir(parents=True)
            (hooks_src / "pre-commit").write_text("#!/bin/bash\n")
            git_hooks = source / ".git" / "hooks"

            setup_module.setup_git_hooks(setup_module.Operation.INSTALL, source)

            self.assertTrue((git_hooks / "pre-commit").is_symlink())

    def test_install_no_op_when_no_git_hooks_dir(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "tamago"
            source.mkdir()
            # No git-hooks/ dir — should not raise
            setup_module.setup_git_hooks(setup_module.Operation.INSTALL, source)

    def test_uninstall_removes_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "tamago"
            hooks_src = source / "git-hooks"
            hooks_src.mkdir(parents=True)
            (hooks_src / "pre-commit").write_text("#!/bin/bash\n")

            setup_module.setup_git_hooks(setup_module.Operation.INSTALL, source)
            git_hooks = source / ".git" / "hooks"
            self.assertTrue((git_hooks / "pre-commit").is_symlink())

            setup_module.setup_git_hooks(setup_module.Operation.UNINSTALL, source)
            self.assertFalse((git_hooks / "pre-commit").exists())


# ---------------------------------------------------------------------------
# setup_global (exception isolation)
# ---------------------------------------------------------------------------

class SetupGlobalTests(unittest.TestCase):
    """setup_global runs all steps independently — one failure must not block others."""

    def test_one_failing_step_returns_1_but_others_still_run(self):
        ran = []

        def boom(*a, **k):
            raise Exception("step exploded")

        def ok(*a, **k):
            ran.append("ok")

        # Patch the five steps: first one raises, rest succeed
        with (
            mock.patch.object(setup_module, "patch_global_settings", side_effect=boom),
            mock.patch.object(setup_module, "patch_opencode_global_settings", side_effect=ok),
            mock.patch.object(setup_module, "setup_shell_env", side_effect=ok),
            mock.patch.object(setup_module, "setup_git_hooks", side_effect=ok),
            mock.patch.object(setup_module, "setup_local_bin", side_effect=ok),
        ):
            rc = setup_module.setup_global(
                setup_module.Operation.INSTALL, Path("/fake")
            )

        self.assertEqual(rc, 1)
        self.assertEqual(len(ran), 4)  # all four non-failing steps ran

    def test_all_steps_succeed_returns_0(self):
        noop = mock.Mock()
        with (
            mock.patch.object(setup_module, "patch_global_settings", noop),
            mock.patch.object(setup_module, "patch_opencode_global_settings", noop),
            mock.patch.object(setup_module, "setup_shell_env", noop),
            mock.patch.object(setup_module, "setup_git_hooks", noop),
            mock.patch.object(setup_module, "setup_local_bin", noop),
        ):
            rc = setup_module.setup_global(
                setup_module.Operation.INSTALL, Path("/fake")
            )
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# setup_skills — bin/ entry points and UNINSTALL
# ---------------------------------------------------------------------------

class SetupSkillsBinAndUninstallTests(unittest.TestCase):

    def _fake_expanduser(self, root: Path):
        orig = Path.expanduser
        def fake(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(root / "home") + s[1:])
            return orig(self)
        return fake

    def _make_source_with_bin_skill(self, root: Path, skill_name: str) -> Path:
        source = root / "tamago"
        skill_dir = source / "skills" / skill_name
        bin_dir = skill_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / f"{skill_name}-cli.py").write_text("#!/usr/bin/env python3\n")
        return source

    def test_global_skill_with_bin_creates_local_bin_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin_skill(root, "tts")
            project = root / "project"
            local_bin = root / "home" / ".local" / "bin"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL, source, project,
                    has_global_agent=False,
                )

            self.assertTrue((local_bin / "tts-cli.py").is_symlink())

    def test_uninstall_removes_global_skill_and_bin_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin_skill(root, "tts")
            project = root / "project"
            home_claude_skills = root / "home" / ".claude" / "skills"
            local_bin = root / "home" / ".local" / "bin"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL, source, project,
                    has_global_agent=False,
                )
                self.assertTrue((home_claude_skills / "tts").is_symlink())
                self.assertTrue((local_bin / "tts-cli.py").is_symlink())

                setup_module.setup_skills(
                    setup_module.Operation.UNINSTALL, source, project,
                    has_global_agent=False,
                )

            self.assertFalse((home_claude_skills / "tts").exists())
            self.assertFalse((local_bin / "tts-cli.py").exists())

    def test_project_scoped_skill_bin_removed_from_local_bin_on_scope_change(self):
        """When a global skill is re-scoped to project, its ~/.local/bin entry is removed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_source_with_bin_skill(root, "tts")
            project = root / "project"
            local_bin = root / "home" / ".local" / "bin"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                # First install: global
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL, source, project,
                    has_global_agent=False,
                )
                self.assertTrue((local_bin / "tts-cli.py").is_symlink())

                # Re-install: now project-scoped via override
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL, source, project,
                    project_scoped_skills={"tts"},
                    has_global_agent=False,
                )

            self.assertFalse((local_bin / "tts-cli.py").exists())
            self.assertTrue((project / ".claude" / "skills" / "tts").is_symlink())

    def test_stale_project_symlink_removed_when_skill_moves_to_global(self):
        """When a project-scoped skill moves to global, old project/.claude/skills/ symlink is removed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "tamago"
            (source / "skills" / "tts").mkdir(parents=True)
            project = root / "project"
            project_skills = project / ".claude" / "skills"
            project_skills.mkdir(parents=True)
            (project_skills / "tts").symlink_to(source / "skills" / "tts")

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_skills(
                    setup_module.Operation.INSTALL, source, project,
                    has_global_agent=False,  # tamago built-in → global
                )

            # Old project-level symlink must be gone
            self.assertFalse((project_skills / "tts").exists())
            # Now installed at global
            self.assertTrue((root / "home" / ".claude" / "skills" / "tts").is_symlink())


# ---------------------------------------------------------------------------
# setup_agents — memory dirs and UNINSTALL
# ---------------------------------------------------------------------------

class SetupAgentsMemoryAndUninstallTests(unittest.TestCase):

    def _fake_expanduser(self, root: Path):
        orig = Path.expanduser
        def fake(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(root / "home") + s[1:])
            return orig(self)
        return fake

    def _make_minimal_source(self, root: Path) -> Path:
        source = root / "tamago"
        (source / "agents").mkdir(parents=True)
        (source / "skills").mkdir(parents=True)
        (source / "settings" / "claude").mkdir(parents=True)
        (source / "settings" / "opencode" / "plugins").mkdir(parents=True)
        (source / "settings" / "claude" / "settings.json").write_text("{}")
        (source / "settings" / "opencode" / "opencode.json").write_text("{}")
        (source / "docs").mkdir(parents=True)
        (source / "docs" / "tamago-agent-base.md").write_text("# Base\n")
        return source

    def _make_profile_with_memory(self, root: Path, agent_name: str) -> Path:
        profile = root / "profile"
        (profile / "agents").mkdir(parents=True)
        (profile / "agents" / f"{agent_name}.persona.md").write_text(
            f"---\nname: {agent_name}\n---\n# Persona\n"
        )
        mem_dir = profile / "agents" / "memory" / agent_name
        mem_dir.mkdir(parents=True)
        return profile

    def test_project_agent_memory_goes_to_project_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            profile = self._make_profile_with_memory(root, "hammer.mei")
            project = root / "project"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source, project,
                    profile_root=profile,
                    global_agents=set(),
                )

            mem_link = project / ".claude" / "agent-memory" / "hammer.mei"
            self.assertTrue(mem_link.is_symlink())
            home_mem = root / "home" / ".claude" / "agent-memory" / "hammer.mei"
            self.assertFalse(home_mem.exists())

    def test_global_agent_memory_goes_to_home_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            profile = self._make_profile_with_memory(root, "hammer.mei")
            project = root / "project"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source, project,
                    profile_root=profile,
                    global_agents={"hammer.mei"},
                )

            home_mem = root / "home" / ".claude" / "agent-memory" / "hammer.mei"
            self.assertTrue(home_mem.is_symlink())
            project_mem = project / ".claude" / "agent-memory" / "hammer.mei"
            self.assertFalse(project_mem.exists())

    def test_global_agent_memory_cleans_up_old_project_symlink(self):
        """When agent moves to global, stale project-level memory symlink is removed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            profile = self._make_profile_with_memory(root, "hammer.mei")
            project = root / "project"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                # First install as project-scoped
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source, project,
                    profile_root=profile,
                    global_agents=set(),
                )
                project_mem = project / ".claude" / "agent-memory" / "hammer.mei"
                self.assertTrue(project_mem.is_symlink())

                # Re-install as global-scoped
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL,
                    source, project,
                    profile_root=profile,
                    global_agents={"hammer.mei"},
                )

            # Old project symlink cleaned up
            self.assertFalse(project_mem.exists())
            home_mem = root / "home" / ".claude" / "agent-memory" / "hammer.mei"
            self.assertTrue(home_mem.is_symlink())

    def test_uninstall_removes_project_agent_and_memory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            profile = self._make_profile_with_memory(root, "hammer.mei")
            project = root / "project"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL, source, project,
                    profile_root=profile, global_agents=set(),
                )
                mem_link = project / ".claude" / "agent-memory" / "hammer.mei"
                self.assertTrue(mem_link.is_symlink())

                setup_module.setup_agents(
                    setup_module.Operation.UNINSTALL, source, project,
                    profile_root=profile, global_agents=set(),
                )

            self.assertFalse(mem_link.exists())
            agent_md = project / ".claude" / "agents" / "hammer.mei.md"
            self.assertFalse(agent_md.exists())

    def test_uninstall_removes_global_agent_and_memory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            profile = self._make_profile_with_memory(root, "hammer.mei")
            project = root / "project"

            with mock.patch.object(Path, "expanduser", self._fake_expanduser(root)):
                setup_module.setup_agents(
                    setup_module.Operation.INSTALL, source, project,
                    profile_root=profile, global_agents={"hammer.mei"},
                )
                home_mem = root / "home" / ".claude" / "agent-memory" / "hammer.mei"
                self.assertTrue(home_mem.is_symlink())

                setup_module.setup_agents(
                    setup_module.Operation.UNINSTALL, source, project,
                    profile_root=profile, global_agents={"hammer.mei"},
                )

            self.assertFalse(home_mem.exists())


# ---------------------------------------------------------------------------
# main() / CLI dispatch
# ---------------------------------------------------------------------------

class MainCliDispatchTests(unittest.TestCase):
    """Tests for main() argument parsing and command routing."""

    def _make_minimal_source(self, root: Path) -> Path:
        source = root / "tamago"
        (source / "agents").mkdir(parents=True)
        (source / "skills").mkdir(parents=True)
        (source / "settings" / "claude").mkdir(parents=True)
        (source / "settings" / "opencode" / "plugins").mkdir(parents=True)
        (source / "settings" / "claude" / "settings.json").write_text("{}")
        (source / "settings" / "opencode" / "opencode.json").write_text("{}")
        return source

    def _run(self, argv: list[str], source_root: Path | None = None,
             extra_patches: dict | None = None) -> int:
        patches = {
            "pull_repo": mock.Mock(),
        }
        if source_root:
            patches["resolve_source_root"] = mock.Mock(return_value=source_root)
        if extra_patches:
            patches.update(extra_patches)

        with mock.patch("sys.argv", ["setup.py"] + argv):
            ctx = contextlib.ExitStack()
            for name, val in patches.items():
                if callable(val) and not isinstance(val, mock.Mock):
                    ctx.enter_context(mock.patch.object(setup_module, name, val))
                else:
                    ctx.enter_context(mock.patch.object(setup_module, name, val))
            with ctx:
                return setup_module.main()

    def test_install_global_calls_setup_global(self):
        with tempfile.TemporaryDirectory() as td:
            source = self._make_minimal_source(Path(td))
            sg_mock = mock.Mock(return_value=0)
            rc = self._run(
                ["install-global"],
                source_root=source,
                extra_patches={"setup_global": sg_mock},
            )
            self.assertEqual(rc, 0)
            sg_mock.assert_called_once_with(setup_module.Operation.INSTALL, source)

    def test_uninstall_global_calls_setup_global_uninstall(self):
        with tempfile.TemporaryDirectory() as td:
            source = self._make_minimal_source(Path(td))
            sg_mock = mock.Mock(return_value=0)
            rc = self._run(
                ["uninstall-global"],
                source_root=source,
                extra_patches={"setup_global": sg_mock},
            )
            self.assertEqual(rc, 0)
            sg_mock.assert_called_once_with(setup_module.Operation.UNINSTALL, source)

    def test_update_treated_as_install_from_conf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            conf = root / ".tamago" / "tamago.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text('[agents]\n')

            ifc_mock = mock.Mock(return_value=0)
            with mock.patch("sys.argv", ["setup.py", "update"]):
                with mock.patch("pathlib.Path.cwd", return_value=root):
                    with mock.patch.object(setup_module, "resolve_source_root", return_value=source):
                        with mock.patch.object(setup_module, "pull_repo"):
                            with mock.patch.object(setup_module, "install_from_conf", ifc_mock):
                                setup_module.main()

            ifc_mock.assert_called_once()
            _, kwargs = ifc_mock.call_args
            self.assertTrue(kwargs.get("pull_cached_skills"))

    def test_doctor_calls_run_health_check(self):
        with tempfile.TemporaryDirectory() as td:
            source = self._make_minimal_source(Path(td))
            hc_mock = mock.Mock()
            rc = self._run(
                ["doctor"],
                source_root=source,
                extra_patches={"run_health_check": hc_mock},
            )
            self.assertEqual(rc, 0)
            hc_mock.assert_called_once()

    def test_install_without_conf_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._make_minimal_source(root)
            # No tamago.conf in cwd
            with mock.patch("sys.argv", ["setup.py", "install"]):
                with mock.patch("pathlib.Path.cwd", return_value=root):
                    with mock.patch.object(setup_module, "resolve_source_root", return_value=source):
                        with mock.patch.object(setup_module, "pull_repo"):
                            rc = setup_module.main()
            self.assertEqual(rc, 1)

    def test_resolve_source_root_error_returns_1(self):
        with mock.patch("sys.argv", ["setup.py", "install-global"]):
            with mock.patch.object(
                setup_module, "resolve_source_root",
                side_effect=ValueError("not found"),
            ):
                rc = setup_module.main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
