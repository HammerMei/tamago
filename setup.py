#!/usr/bin/env python3
"""Tamago — AI agent setup tool.

Commands
--------
install-global [--source <tamago>]
    Symlink global Claude/OpenCode settings to ~/.claude and ~/.opencode.
    Also injects ASSISTANT_SETUP_REPO into ~/.zshrc when source != default.

install [--source <tamago>] [--profile <profile_repo>]
    Symlink skills, agents, settings, and memory into the current project
    directory (.claude/, .opencode/).  When --profile is given, agent/persona
    files come from the profile repo and PROFILE_REPO is recorded in
    <tamago>/local.conf so memory-sync.sh knows where to do git operations.

uninstall [--source <tamago>] [--profile <profile_repo>]
    Reverse of install — remove all symlinks created by install.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable
from enum import Enum


class Operation(str, Enum):
    INSTALL = "install"
    UNINSTALL = "uninstall"


# The directory containing this script is always the tamago repo root —
# use it as the default so setup.py works regardless of where tamago is installed.
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parent

# Conventional install location — shell scripts fall back to this path when
# ASSISTANT_SETUP_REPO is not set.  We skip env injection only when tamago
# is actually installed here (the fallback already covers it).
CONVENTIONAL_ROOT = Path("~/.tamago").expanduser()
GITIGNORE_ENTRIES = (".claude", ".opencode", ".tamago")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def sub_paths(root: Path, selector: Callable[[Path], bool]) -> list[Path]:
    if not root.is_dir():
        raise Exception(f"Source directory not found: {root}")
    return sorted(path for path in root.iterdir() if selector(path))


def symlink_paths(source_paths: list[Path], target_root: Path):
    target_root.mkdir(parents=True, exist_ok=True)

    if not source_paths:
        return

    for source in source_paths:
        target = target_root / source.name

        if target.is_symlink():
            if target.resolve() == source.resolve():
                print(f"exists  {target} -> {source}")
                continue
            target.unlink()
        elif target.exists():
            raise Exception(f"skip   {target} (already exists and is not a symlink)")

        target.symlink_to(source)
        print(f"linked  {target} -> {source}")


def unlink_paths(source_paths: list[Path], target_root: Path):
    if not source_paths:
        return

    for source in source_paths:
        target = target_root / source.name

        if target.is_symlink():
            target.unlink()
            print(f"removed {target}")
            continue

        print(f"warning {target} is not a symlink")


# ---------------------------------------------------------------------------
# .gitignore management
# ---------------------------------------------------------------------------

def setup_gitignore(operation: Operation, project_root: Path):
    gitignore_path = project_root / ".gitignore"

    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text().splitlines()
    else:
        existing_lines = []

    normalized_entries = set(GITIGNORE_ENTRIES)

    def normalize_gitignore_line(line: str) -> str:
        return line.strip().rstrip("/")

    if operation == Operation.INSTALL:
        updated_lines = list(existing_lines)
        existing_entries = {normalize_gitignore_line(line) for line in existing_lines}

        for entry in GITIGNORE_ENTRIES:
            if entry not in existing_entries:
                updated_lines.append(entry)

        if updated_lines != existing_lines:
            content = "\n".join(updated_lines)
            if content:
                content = f"{content}\n"
            gitignore_path.write_text(content)
            print(f"updated {gitignore_path}")
        else:
            print(f"exists  {gitignore_path}")
    elif operation == Operation.UNINSTALL:
        updated_lines = [
            line
            for line in existing_lines
            if normalize_gitignore_line(line) not in normalized_entries
        ]

        if updated_lines != existing_lines:
            content = "\n".join(updated_lines)
            if content:
                content = f"{content}\n"
            gitignore_path.write_text(content)
            print(f"updated {gitignore_path}")
        else:
            print(f"exists  {gitignore_path}")


# ---------------------------------------------------------------------------
# Project config — stores PROFILE_REPO per project
# ---------------------------------------------------------------------------
#
# Config is stored in <project_dir>/.tamago/tamago.conf (project-scoped) so
# multiple projects can each use a different profile without overwriting each
# other.  tamago's global local.conf is also written as a convenience cache
# for memory-sync.sh (which may not have easy access to the project dir).

PROJECT_CONF_NAME = "tamago.conf"  # lives inside <project_dir>/.tamago/


def _write_conf_file(path: Path, lines: list[str]) -> None:
    content = "\n".join(lines) + "\n"
    if path.exists() and path.read_text() == content:
        print(f"exists  {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"updated {path}")


def write_local_conf(
    source_root: Path,
    profile_root: Path | None,
    project_root: Path | None = None,
    memory_sync: bool = True,
    tts_enabled: bool = True,
):
    """Write (or remove) project-scoped tamago.conf and tamago's global local.conf."""
    # ── Project-scoped config (.tamago/tamago.conf) ──────────────────────────
    if project_root is not None:
        project_conf = project_root / ".tamago" / PROJECT_CONF_NAME
        if profile_root is None:
            if project_conf.exists():
                project_conf.unlink()
                print(f"removed {project_conf}")
        else:
            lines = [f"PROFILE_REPO={profile_root.resolve()}"]
            if not memory_sync:
                lines.append("MEMORY_SYNC=0")
            if not tts_enabled:
                lines.append("TTS_ENABLED=0")
            _write_conf_file(project_conf, lines)

    # ── Global cache (tamago/local.conf) — used by memory-sync.sh ────────────
    local_conf = source_root / "local.conf"
    if profile_root is None:
        if local_conf.exists():
            local_conf.unlink()
            print(f"removed {local_conf}")
        return

    lines = [f"PROFILE_REPO={profile_root.resolve()}"]
    if project_root is not None:
        lines.append(f"PROJECT_DIR={project_root.resolve()}")
    if not memory_sync:
        lines.append("MEMORY_SYNC=0")
    if not tts_enabled:
        lines.append("TTS_ENABLED=0")
    _write_conf_file(local_conf, lines)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def setup_settings(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
):
    # When a profile is given, the project-level settings come from the profile
    # (which contains the agent name).  Otherwise fall back to tamago's common
    # settings (no agent name — suitable for generic / multi-persona projects).
    settings_source = profile_root if profile_root else source_root
    claude_setting_file = settings_source / "settings" / "claude" / "settings.json"
    opencode_setting_file = settings_source / "settings" / "opencode" / "opencode.json"

    if operation == Operation.INSTALL:
        symlink_paths([claude_setting_file], project_root / ".claude")
        symlink_paths([opencode_setting_file], project_root / ".opencode")
    elif operation == Operation.UNINSTALL:
        unlink_paths([claude_setting_file], project_root / ".claude")
        unlink_paths([opencode_setting_file], project_root / ".opencode")


def setup_global_settings(operation: Operation, source_root: Path):
    """Symlink global Claude / OpenCode settings to ~/.claude and ~/.opencode."""
    home = Path.home()
    claude_setting_file = source_root / "settings" / "claude" / "settings.json"
    opencode_setting_file = source_root / "settings" / "opencode" / "opencode.json"

    if operation == Operation.INSTALL:
        symlink_paths([claude_setting_file], home / ".claude")
        symlink_paths([opencode_setting_file], home / ".opencode")
    elif operation == Operation.UNINSTALL:
        unlink_paths([claude_setting_file], home / ".claude")
        unlink_paths([opencode_setting_file], home / ".opencode")


# ---------------------------------------------------------------------------
# Agents & memory
# ---------------------------------------------------------------------------

GENERATED_HEADER_MARKER = "<!-- TAMAGO GENERATED"


def _merge_agent(
    source_root: Path,
    profile_root: Path,
    persona_file: Path,
    target_dir: Path,
    tts_enabled: bool = True,
) -> None:
    """Merge tamago-agent-base.md + persona file → target_dir/<agent_name>.md."""
    # agent_name: strip the ".persona" suffix  (hammer.mei.persona.md → hammer.mei)
    agent_name = persona_file.stem  # e.g. "hammer.mei.persona"
    if agent_name.endswith(".persona"):
        agent_name = agent_name[: -len(".persona")]

    base_file = source_root / "docs" / "tamago-agent-base.md"
    if not base_file.exists():
        raise Exception(f"tamago-agent-base.md not found: {base_file}")

    profile_path = str(profile_root.resolve())
    memory_path = f"{profile_path}/agents/memory/{agent_name}"

    def _sub(text: str) -> str:
        return (
            text.replace("{{AGENT_NAME}}", agent_name)
                .replace("{{PROFILE_REPO}}", profile_path)
                .replace("{{AGENT_MEMORY_PATH}}", memory_path)
        )

    base_content = _sub(base_file.read_text())
    persona_raw = persona_file.read_text()

    # Split persona file into frontmatter + body
    frontmatter, body = "", persona_raw
    if persona_raw.startswith("---"):
        parts = persona_raw.split("---", 2)
        if len(parts) >= 3:
            frontmatter = "---" + parts[1] + "---\n"
            body = parts[2].lstrip("\n")

    header = (
        f"{GENERATED_HEADER_MARKER} — DO NOT EDIT DIRECTLY\n"
        f"     Sources:\n"
        f"       mechanics : tamago/docs/tamago-agent-base.md\n"
        f"       persona   : profile/agents/{persona_file.name}\n"
        f"     Regenerate  : python3 setup.py install --profile {profile_path}\n"
        f"-->\n\n"
    )

    if not tts_enabled:
        # Strip ## TTS section from persona body (## TTS up to next ## heading or end)
        body = re.sub(r"\n## TTS\n.*?(?=\n## |\Z)", "", body, flags=re.DOTALL)
        # Remove text-to-speech from skills list in frontmatter
        frontmatter = re.sub(r"^(\s*-\s*text-to-speech\s*\n)", "", frontmatter, flags=re.MULTILINE)

    merged = frontmatter + header + base_content + "\n\n---\n\n" + body

    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / f"{agent_name}.md"
    output.write_text(merged)
    print(f"merged  {output}")


def _remove_generated_agents(profile_root: Path, target_dir: Path) -> None:
    """Remove generated agent .md files from target_dir."""
    if not (profile_root / "agents").is_dir():
        return
    for persona_file in (profile_root / "agents").iterdir():
        if not (persona_file.is_file() and persona_file.suffix == ".md"):
            continue
        stem = persona_file.stem
        agent_name = stem[: -len(".persona")] if stem.endswith(".persona") else stem
        target = target_dir / f"{agent_name}.md"
        if target.exists() and GENERATED_HEADER_MARKER in target.read_text()[:300]:
            target.unlink()
            print(f"removed {target}")


def setup_agents(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
    tts_enabled: bool = True,
):
    source_opencode_plugin_root = source_root / "settings" / "opencode" / "plugins"

    target_claude_agent_root = project_root / ".claude" / "agents"
    target_claude_agent_mem_root = project_root / ".claude" / "agent-memory"
    target_opencode_agent_root = project_root / ".opencode" / "agents"
    target_opencode_plugin_root = project_root / ".opencode" / "plugins"

    opencode_plugin_files = sub_paths(
        source_opencode_plugin_root, lambda p: p.is_file() and p.suffix == ".ts"
    )

    # Generic agents from tamago (code-reviewer.md, technical-writer.md, etc.)
    tamago_agent_files = sub_paths(
        source_root / "agents", lambda p: p.is_file() and p.suffix == ".md"
    )

    if operation == Operation.INSTALL:
        # 1. Symlink tamago generic agents
        symlink_paths(tamago_agent_files, target_claude_agent_root)
        symlink_paths(tamago_agent_files, target_opencode_agent_root)
        symlink_paths(opencode_plugin_files, target_opencode_plugin_root)

        # 2. Merge persona agents from profile (*.persona.md → generated *.md)
        #    Plain *.md files in profile/agents/ are symlinked directly.
        if profile_root and (profile_root / "agents").is_dir():
            profile_agent_files = sub_paths(
                profile_root / "agents",
                lambda p: p.is_file() and p.suffix == ".md" and not p.stem.endswith(".persona"),
            )
            symlink_paths(profile_agent_files, target_claude_agent_root)
            symlink_paths(profile_agent_files, target_opencode_agent_root)

            for persona_file in sorted((profile_root / "agents").iterdir()):
                if persona_file.is_file() and persona_file.name.endswith(".persona.md"):
                    _merge_agent(source_root, profile_root, persona_file, target_claude_agent_root, tts_enabled)
                    _merge_agent(source_root, profile_root, persona_file, target_opencode_agent_root, tts_enabled)

        # 3. Memory dirs from profile (or tamago fallback)
        if profile_root and (profile_root / "agents" / "memory").is_dir():
            mem_source = profile_root / "agents" / "memory"
        elif (source_root / "agents" / "memory").is_dir():
            mem_source = source_root / "agents" / "memory"
        else:
            mem_source = None

        if mem_source:
            agent_mem_dirs = sub_paths(mem_source, lambda p: p.is_dir() and not p.name.startswith("."))
            symlink_paths(agent_mem_dirs, target_claude_agent_mem_root)

    elif operation == Operation.UNINSTALL:
        unlink_paths(tamago_agent_files, target_claude_agent_root)
        unlink_paths(tamago_agent_files, target_opencode_agent_root)
        unlink_paths(opencode_plugin_files, target_opencode_plugin_root)

        # Remove symlinked plain profile agents and generated persona agents
        if profile_root and (profile_root / "agents").is_dir():
            profile_agent_files = sub_paths(
                profile_root / "agents",
                lambda p: p.is_file() and p.suffix == ".md" and not p.stem.endswith(".persona"),
            )
            unlink_paths(profile_agent_files, target_claude_agent_root)
            unlink_paths(profile_agent_files, target_opencode_agent_root)
            _remove_generated_agents(profile_root, target_claude_agent_root)
            _remove_generated_agents(profile_root, target_opencode_agent_root)

        # Remove memory symlinks
        mem_source = (
            (profile_root / "agents" / "memory") if profile_root
            else (source_root / "agents" / "memory")
        )
        if mem_source.is_dir():
            agent_mem_dirs = sub_paths(mem_source, lambda p: p.is_dir() and not p.name.startswith("."))
            unlink_paths(agent_mem_dirs, target_claude_agent_mem_root)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def setup_skills(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
):
    """Symlink skills into project_root.

    Skills come from two sources (profile skills take precedence over tamago skills):
      1. tamago/skills/  — built-in skills bundled with tamago
      2. profile/skills/ — custom skills defined in the profile repo (optional)

    When both sources contain a skill with the same name, the profile version wins.
    """
    target_claude_skills_root = project_root / ".claude" / "skills"
    target_opencode_skills_root = project_root / ".opencode" / "skills"

    # Collect tamago built-in skills
    tamago_skills_root = source_root / "skills"
    tamago_skill_dirs = sub_paths(
        tamago_skills_root, lambda p: p.is_dir() and not p.name.startswith(".")
    )

    # Collect profile-specific skills (may override tamago skills of the same name)
    profile_skill_dirs: list[Path] = []
    if profile_root:
        profile_skills_root = profile_root / "skills"
        if profile_skills_root.is_dir():
            profile_skill_dirs = sub_paths(
                profile_skills_root, lambda p: p.is_dir() and not p.name.startswith(".")
            )

    # Build merged list: profile skills shadow tamago skills of the same name
    profile_skill_names = {p.name for p in profile_skill_dirs}
    merged_skill_dirs = [
        d for d in tamago_skill_dirs if d.name not in profile_skill_names
    ] + profile_skill_dirs

    if operation == Operation.INSTALL:
        symlink_paths(merged_skill_dirs, target_claude_skills_root)
        symlink_paths(merged_skill_dirs, target_opencode_skills_root)
    elif operation == Operation.UNINSTALL:
        unlink_paths(merged_skill_dirs, target_claude_skills_root)
        unlink_paths(merged_skill_dirs, target_opencode_skills_root)


# ---------------------------------------------------------------------------
# ~/.zshrc env injection
# ---------------------------------------------------------------------------

def setup_shell_env(operation: Operation, source_root: Path):
    """Inject/remove ASSISTANT_SETUP_REPO in ~/.zshrc"""
    zshrc = Path("~/.zshrc").expanduser()
    env_key = "ASSISTANT_SETUP_REPO"
    env_line = f'export {env_key}="{source_root}"'
    marker = f"{env_key}="
    comment = "# Tamago assistant repo"

    if operation == Operation.INSTALL:
        if source_root == CONVENTIONAL_ROOT:
            print(f"skip    {env_key} injection (using default path, fallback covers it)")
            return

        lines = zshrc.read_text().splitlines() if zshrc.exists() else []
        matching = [i for i, l in enumerate(lines) if marker in l]

        if matching:
            if env_line in lines[matching[0]]:
                print(f"exists  {env_key} in ~/.zshrc")
                return
            lines[matching[0]] = env_line
            zshrc.write_text("\n".join(lines) + "\n")
            print(f"updated {env_key} in ~/.zshrc  (run: source ~/.zshrc)")
        else:
            with open(zshrc, "a") as f:
                f.write(f"\n{comment}\n{env_line}\n")
            print(f"added   {env_key} to ~/.zshrc  (run: source ~/.zshrc)")

    elif operation == Operation.UNINSTALL:
        if not zshrc.exists():
            return
        lines = zshrc.read_text().splitlines()
        new_lines = [
            l for i, l in enumerate(lines)
            if marker not in l and not (
                l.strip() == comment and i + 1 < len(lines) and marker in lines[i + 1]
            )
        ]
        if new_lines != lines:
            zshrc.write_text("\n".join(new_lines) + "\n")
            print(f"removed {env_key} from ~/.zshrc  (run: source ~/.zshrc)")
        else:
            print(f"skip    {env_key} not found in ~/.zshrc")


# ---------------------------------------------------------------------------
# Top-level orchestrators
# ---------------------------------------------------------------------------

def run_health_check(source_root: Path, project_root: Path) -> None:
    """Run health-check.sh after install to surface any environment issues."""
    health_check = source_root / "scripts" / "health-check.sh"
    if not health_check.exists():
        print("skip    health check (scripts/health-check.sh not found)")
        return

    print("\n" + "─" * 60)
    subprocess.run(
        ["bash", str(health_check), "--project", str(project_root)],
        check=False,
    )


def setup_git_hooks(operation: Operation, source_root: Path) -> None:
    """Install/remove tamago's git hooks into source_root/.git/hooks/."""
    git_hooks_dir = source_root / ".git" / "hooks"
    source_hooks_dir = source_root / "git-hooks"

    if not source_hooks_dir.is_dir():
        return

    hook_files = sub_paths(source_hooks_dir, lambda p: p.is_file())

    if operation == Operation.INSTALL:
        git_hooks_dir.mkdir(parents=True, exist_ok=True)
        symlink_paths(hook_files, git_hooks_dir)
    elif operation == Operation.UNINSTALL:
        unlink_paths(hook_files, git_hooks_dir)


def setup_global(operation: Operation, source_root: Path) -> int:
    """Home-level install: symlink to ~/.claude and ~/.opencode, update ~/.zshrc,
    and install tamago's own git hooks.
    Each step runs independently — one failure does not block the others."""
    errors: list[str] = []

    for step in (
        lambda: setup_global_settings(operation, source_root),
        lambda: setup_shell_env(operation, source_root),
        lambda: setup_git_hooks(operation, source_root),
    ):
        try:
            step()
        except Exception as e:
            print(e, file=sys.stderr)
            errors.append(str(e))

    return 1 if errors else 0


def setup(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
    memory_sync: bool = True,
    tts_enabled: bool = True,
) -> int:
    """Project-level install: symlink skills, agents, settings, memory into project_root."""
    try:
        # For INSTALL: if a different profile was previously installed, clean it up first
        # so stale agent .md files and memory symlinks from the old profile are removed.
        if operation == Operation.INSTALL and profile_root is not None:
            project_conf = project_root / ".tamago" / PROJECT_CONF_NAME
            if project_conf.exists():
                for line in project_conf.read_text().splitlines():
                    if line.startswith("PROFILE_REPO="):
                        old_profile = Path(line.split("=", 1)[1].strip())
                        if old_profile.is_dir() and old_profile.resolve() != profile_root.resolve():
                            print(
                                f"info    replacing profile: {old_profile.name} → {profile_root.name}"
                            )
                            setup_agents(Operation.UNINSTALL, source_root, project_root, old_profile)
                            setup_skills(Operation.UNINSTALL, source_root, project_root, old_profile)
                        break

        setup_gitignore(operation, project_root)
        setup_skills(operation, source_root, project_root, profile_root)
        setup_agents(operation, source_root, project_root, profile_root, tts_enabled=tts_enabled)
        setup_settings(operation, source_root, project_root, profile_root)

        # Record (or remove) PROFILE_REPO + PROJECT_DIR + MEMORY_SYNC in local.conf
        if operation == Operation.INSTALL:
            write_local_conf(source_root, profile_root, project_root, memory_sync, tts_enabled)
            run_health_check(source_root, project_root)
        elif operation == Operation.UNINSTALL:
            write_local_conf(source_root, None)

        return 0
    except Exception as e:
        print(e, file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Argument parsing & main
# ---------------------------------------------------------------------------

def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a git URL, stripping any trailing .git suffix."""
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def resolve_profile_root(
    source_root: Path,
    profile_dir: str | None,
    profile_repo: str | None,
    profile_name: str | None,
) -> Path | None:
    """Resolve profile_root from one of the three profile specifier options.

    --profile-dir <path>      — use path directly (must already exist)
    --profile-repo <url>      — clone (or pull) the repo; enforce *-profile naming
    --profile-name <name>     — shorthand: <source_root>/<name>-profile
    """
    if profile_dir:
        p = Path(profile_dir).expanduser().resolve()
        if not p.is_dir():
            raise ValueError(f"Profile directory not found: {p}")
        return p

    if profile_name:
        p = source_root / f"{profile_name}-profile"
        if not p.is_dir():
            raise ValueError(
                f"Profile directory not found: {p}\n"
                f"  Hint: clone your profile repo there first, or use --profile-repo to clone automatically."
            )
        return p

    if profile_repo:
        repo_name = _repo_name_from_url(profile_repo)
        if not repo_name.endswith("-profile"):
            raise ValueError(
                f"Profile repo name must end with '-profile', got: '{repo_name}'\n"
                f"  Rename your repo to follow the convention (e.g. '{repo_name}-profile'),\n"
                f"  or use --profile-dir to skip the naming check."
            )
        clone_dir = source_root / repo_name
        if clone_dir.is_dir() and (clone_dir / ".git").exists():
            print(f"exists  {clone_dir}  (pulling latest)")
            result = subprocess.run(
                ["git", "-C", str(clone_dir), "pull", "--rebase"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise ValueError(
                    f"git pull failed in {clone_dir}:\n{result.stderr.strip()}"
                )
        elif clone_dir.exists():
            raise ValueError(
                f"Directory exists but is not a git repo: {clone_dir}\n"
                f"  Remove it first or use --profile-dir to point elsewhere."
            )
        else:
            print(f"cloning {profile_repo}")
            print(f"     → {clone_dir}")
            result = subprocess.run(
                ["git", "clone", profile_repo, str(clone_dir)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise ValueError(
                    f"git clone failed:\n{result.stderr.strip()}"
                )
        return clone_dir

    return None


def resolve_source_root(source_override: str | None) -> Path:
    source_root = source_override or os.environ.get("ASSISTANT_SETUP_REPO")

    if source_root is None:
        resolved_root = DEFAULT_SOURCE_ROOT
    else:
        resolved_root = Path(source_root).expanduser()

    if not resolved_root.is_dir():
        raise ValueError(f"Source directory not found: {resolved_root}")

    return resolved_root


def build_parser() -> argparse.ArgumentParser:
    source_parser = argparse.ArgumentParser(add_help=False)
    source_parser.add_argument(
        "-s", "--source",
        dest="source",
        help="path to tamago repo (overrides ASSISTANT_SETUP_REPO and default)",
    )

    profile_parser = argparse.ArgumentParser(add_help=False)
    profile_group = profile_parser.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--profile-dir",
        dest="profile_dir",
        default=None,
        metavar="PATH",
        help="path to an already-cloned profile repo",
    )
    profile_group.add_argument(
        "--profile-repo",
        dest="profile_repo",
        default=None,
        metavar="URL",
        help="git URL of profile repo — clones to <tamago>/<repo-name>/ (name must end with -profile)",
    )
    profile_group.add_argument(
        "--profile-name",
        dest="profile_name",
        default=None,
        metavar="NAME",
        help="short name, e.g. 'hammer.mei' — resolves to <tamago>/hammer.mei-profile/",
    )

    profile_parser.add_argument(
        "--no-memory-sync",
        dest="no_memory_sync",
        action="store_true",
        default=False,
        help="write MEMORY_SYNC=0 to local.conf — disables git-based memory sync",
    )

    profile_parser.add_argument(
        "--no-tts",
        dest="no_tts",
        action="store_true",
        default=False,
        help="disable TTS in generated agent files — useful for RC/headless deployments",
    )

    parser = argparse.ArgumentParser(
        description="Tamago — AI agent setup tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--source",
        dest="global_source",
        help="path to tamago repo (overrides ASSISTANT_SETUP_REPO and default)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "install-global",
        help="symlink global settings to ~/.claude and ~/.opencode, update ~/.zshrc",
        parents=[source_parser],
    )
    subparsers.add_parser(
        "uninstall-global",
        help="remove global symlinks from ~/.claude and ~/.opencode",
        parents=[source_parser],
    )
    subparsers.add_parser(
        Operation.INSTALL.value,
        help="symlink skills, agents, settings, and memory into the current project",
        parents=[source_parser, profile_parser],
    )
    subparsers.add_parser(
        Operation.UNINSTALL.value,
        help="remove symlinks created by install",
        parents=[source_parser, profile_parser],
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    project_root = Path.cwd()

    try:
        source_override = (
            args.source if getattr(args, "source", None) else args.global_source
        )
        source_root = resolve_source_root(source_override)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    if args.command == "install-global":
        return setup_global(Operation.INSTALL, source_root)

    if args.command == "uninstall-global":
        return setup_global(Operation.UNINSTALL, source_root)

    # Resolve profile root from whichever flag was given (or None)
    try:
        profile_root = resolve_profile_root(
            source_root,
            profile_dir=getattr(args, "profile_dir", None),
            profile_repo=getattr(args, "profile_repo", None),
            profile_name=getattr(args, "profile_name", None),
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    # Uninstall fallback: if no profile flag given, read PROFILE_REPO from the
    # project-scoped .tamago/tamago.conf — correct even with multiple projects.
    if profile_root is None and args.command == Operation.UNINSTALL.value:
        project_conf = project_root / ".tamago" / PROJECT_CONF_NAME
        fallback_conf = project_conf if project_conf.exists() else source_root / "local.conf"
        if fallback_conf.exists():
            for line in fallback_conf.read_text().splitlines():
                if line.startswith("PROFILE_REPO="):
                    candidate = Path(line.split("=", 1)[1].strip())
                    if candidate.is_dir():
                        profile_root = candidate
                        print(f"info    using PROFILE_REPO from {fallback_conf.name}: {profile_root}")
                    break

    for op in Operation:
        if args.command == op.value:
            memory_sync = not getattr(args, "no_memory_sync", False)
            tts_enabled = not getattr(args, "no_tts", False)
            # Read TTS_ENABLED from local.conf if --no-tts not explicitly passed
            if tts_enabled:
                for conf_path in [
                    project_root / ".tamago" / PROJECT_CONF_NAME if project_root else None,
                    source_root / "local.conf",
                ]:
                    if conf_path and conf_path.exists():
                        for line in conf_path.read_text().splitlines():
                            if line.strip() == "TTS_ENABLED=0":
                                tts_enabled = False
                                break
                    if not tts_enabled:
                        break
            return setup(op, source_root, project_root, profile_root, memory_sync, tts_enabled)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
