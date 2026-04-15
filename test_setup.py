import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("setup.py")
SPEC = importlib.util.spec_from_file_location("assistant_setup", MODULE_PATH)
setup_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(setup_module)


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


class SetupGitignoreTests(unittest.TestCase):
    def test_install_adds_missing_entries_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text("node_modules\n.claude\n")

            setup_module.setup_gitignore(setup_module.Operation.INSTALL, project_root)

            self.assertEqual(
                gitignore.read_text(),
                "node_modules\n.claude\n.opencode\n",
            )

    def test_install_does_not_duplicate_entries_with_trailing_slash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text(".claude/\n.opencode/\n")

            setup_module.setup_gitignore(setup_module.Operation.INSTALL, project_root)

            self.assertEqual(gitignore.read_text(), ".claude/\n.opencode/\n")

    def test_install_creates_gitignore_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            setup_module.setup_gitignore(setup_module.Operation.INSTALL, project_root)

            self.assertEqual(
                (project_root / ".gitignore").read_text(),
                ".claude\n.opencode\n",
            )

    def test_uninstall_removes_managed_entries_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text("node_modules\n.claude\n.opencode\ndist\n")

            setup_module.setup_gitignore(setup_module.Operation.UNINSTALL, project_root)

            self.assertEqual(gitignore.read_text(), "node_modules\ndist\n")

    def test_uninstall_removes_entries_with_trailing_slash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            gitignore = project_root / ".gitignore"
            gitignore.write_text(".claude/\n.opencode/\n")

            setup_module.setup_gitignore(setup_module.Operation.UNINSTALL, project_root)

            self.assertEqual(gitignore.read_text(), "")


class MainTests(unittest.TestCase):
    def test_main_passes_cli_source_to_setup(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(setup_module, "setup", return_value=0) as setup_mock,
            mock.patch("sys.argv", ["setup.py", "install", "--source", temp_dir]),
        ):
            result = setup_module.main()

        self.assertEqual(result, 0)
        setup_mock.assert_called_once_with(
            setup_module.Operation.INSTALL,
            Path(temp_dir),
            setup_module.Path(setup_module.__file__).resolve().parent,
            None,  # profile_root
        )

    def test_main_accepts_source_before_subcommand(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(setup_module, "setup", return_value=0) as setup_mock,
            mock.patch("sys.argv", ["setup.py", "--source", temp_dir, "install"]),
        ):
            result = setup_module.main()

        self.assertEqual(result, 0)
        setup_mock.assert_called_once_with(
            setup_module.Operation.INSTALL,
            Path(temp_dir),
            setup_module.Path(setup_module.__file__).resolve().parent,
            None,  # profile_root
        )

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


if __name__ == "__main__":
    unittest.main()
