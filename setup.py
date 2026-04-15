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
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable
from enum import Enum


class Operation(str, Enum):
    INSTALL = "install"
    UNINSTALL = "uninstall"


DEFAULT_SOURCE_ROOT = Path("~/workspace/tamago").expanduser()
GITIGNORE_ENTRIES = (".claude", ".opencode")


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
# local.conf — stores PROFILE_REPO for memory-sync.sh
# ---------------------------------------------------------------------------

def write_local_conf(source_root: Path, profile_root: Path | None):
    """Write (or remove) PROFILE_REPO= in <source_root>/local.conf."""
    local_conf = source_root / "local.conf"

    if profile_root is None:
        if local_conf.exists():
            local_conf.unlink()
            print(f"removed {local_conf}")
        return

    content = f"PROFILE_REPO={profile_root.resolve()}\n"
    if local_conf.exists() and local_conf.read_text() == content:
        print(f"exists  {local_conf}")
        return

    local_conf.write_text(content)
    print(f"updated {local_conf}  (PROFILE_REPO={profile_root.resolve()})")


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

def setup_agents(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
):
    # Agent persona .md files: prefer profile, fall back to tamago's generic agents
    agent_source = profile_root if profile_root else source_root
    source_agent_root = agent_source / "agents"
    source_opencode_plugin_root = source_root / "settings" / "opencode" / "plugins"

    target_claude_agent_root = project_root / ".claude" / "agents"
    target_claude_agent_mem_root = project_root / ".claude" / "agent-memory"
    target_opencode_agent_root = project_root / ".opencode" / "agents"
    target_opencode_plugin_root = project_root / ".opencode" / "plugins"

    agent_md_files = sub_paths(
        source_agent_root, lambda p: p.is_file() and p.suffix == ".md"
    )
    opencode_plugin_files = sub_paths(
        source_opencode_plugin_root, lambda p: p.is_file() and p.suffix == ".ts"
    )

    if operation == Operation.INSTALL:
        symlink_paths(agent_md_files, target_claude_agent_root)
        symlink_paths(agent_md_files, target_opencode_agent_root)
        symlink_paths(opencode_plugin_files, target_opencode_plugin_root)

        # Memory dirs live in the profile repo (if given), otherwise tamago
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
        unlink_paths(agent_md_files, target_claude_agent_root)
        unlink_paths(agent_md_files, target_opencode_agent_root)
        unlink_paths(opencode_plugin_files, target_opencode_plugin_root)

        # Remove memory symlinks (best effort — identify by what exists)
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

def setup_skills(operation: Operation, source_root: Path, project_root: Path):
    source_skills_root = source_root / "skills"
    target_claude_skills_root = project_root / ".claude" / "skills"
    target_opencode_skills_root = project_root / ".opencode" / "skills"
    skill_dirs = sub_paths(
        source_skills_root, lambda p: p.is_dir() and not p.name.startswith(".")
    )

    if operation == Operation.INSTALL:
        symlink_paths(skill_dirs, target_claude_skills_root)
        symlink_paths(skill_dirs, target_opencode_skills_root)
    elif operation == Operation.UNINSTALL:
        unlink_paths(skill_dirs, target_claude_skills_root)
        unlink_paths(skill_dirs, target_opencode_skills_root)


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
        if source_root == DEFAULT_SOURCE_ROOT:
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


def setup_global(operation: Operation, source_root: Path) -> int:
    """Home-level install: symlink to ~/.claude and ~/.opencode, update ~/.zshrc."""
    try:
        setup_global_settings(operation, source_root)
        setup_shell_env(operation, source_root)
        return 0
    except Exception as e:
        print(e, file=sys.stderr)
        return 1


def setup(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
) -> int:
    """Project-level install: symlink skills, agents, settings, memory into project_root."""
    try:
        setup_gitignore(operation, project_root)
        setup_skills(operation, source_root, project_root)
        setup_agents(operation, source_root, project_root, profile_root)
        setup_settings(operation, source_root, project_root, profile_root)

        # Record (or remove) PROFILE_REPO in local.conf so memory-sync.sh can find it
        if operation == Operation.INSTALL:
            write_local_conf(source_root, profile_root)
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
    profile_parser.add_argument(
        "--profile",
        dest="profile",
        default=None,
        help="path to profile repo (persona, memory, agent-specific settings)",
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

    # Resolve optional --profile path
    profile_root: Path | None = None
    if getattr(args, "profile", None):
        profile_path = Path(args.profile).expanduser()
        if not profile_path.is_dir():
            print(f"Profile directory not found: {profile_path}", file=sys.stderr)
            return 1
        profile_root = profile_path

    for op in Operation:
        if args.command == op.value:
            return setup(op, source_root, project_root, profile_root)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
