#!/usr/bin/env python3
"""Tamago — AI agent setup tool.

Commands
--------
install-global [--source <tamago>]
    Patch global Claude/OpenCode settings in ~/.claude and ~/.opencode.
install [--source <tamago>] [--config <path>]
    Install agents/skills into the current project from .tamago/tamago.conf.
    Auto-detects .tamago/tamago.conf in the current directory — create one
    from templates/tamago.conf.example before running.

uninstall [--source <tamago>] [--config <path>]
    Reverse of install — remove all tamago-managed files from the project.

update
    Alias for install — also pulls cached skill repos from their remotes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]  # pip install tomli
    except ModuleNotFoundError:
        print(
            "error: tomllib not found.\n"
            "  Python 3.11+ includes it built-in.\n"
            "  On Python 3.10 or earlier: pip install tomli\n"
            "  Or upgrade Python: brew install python@3.11",
            file=sys.stderr,
        )
        sys.exit(1)
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from collections.abc import Callable
from enum import Enum


class Operation(str, Enum):
    INSTALL = "install"
    UNINSTALL = "uninstall"


# Conventional install location: tamago lives at ~/.tamago by default.
# Shell scripts and hooks always reference this path.
# To point tamago at a different directory (e.g. a development checkout),
# set ASSISTANT_SETUP_REPO=/path/to/tamago in your shell profile — tamago
# will never auto-inject this variable.
CONVENTIONAL_ROOT = Path("~/.tamago").expanduser()
GITIGNORE_ENTRIES = (".claude", ".opencode", ".tamago/machine.env", ".tamago/machine.toml")


# Cache root for skill repos cloned from git URLs.
# Uses SHA-256[:16] of the raw URL as the dir name — no URL normalization:
# https://x.git and https://x hash to different directories.
DEFAULT_CACHE_ROOT = Path("~/.tamago/repo-cache").expanduser()

# Global project registry — JSON file tracking all tamago-installed projects.
# Format: {"projects": [{"path": "/abs/path"}, ...]}
# Extensible: future entries can add "last_install", "tamago_version", etc.
KNOWN_PROJECTS_FILE = Path("~/.tamago/known-projects.json").expanduser()


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
# Agent memory directory naming
# ---------------------------------------------------------------------------

def _agent_memory_link_names(agent_name: str) -> list[str]:
    """Return all symlink names to create for an agent's memory directory.

    Claude Code 2.1.121+ changed the agent-memory directory naming convention:
    dots in agent names are replaced with hyphens when constructing the path
    (e.g. ``hammer.mei`` → ``hammer-mei``).  We create both forms pointing at
    the same source so installations work across Claude Code versions until
    Anthropic resolves this.

    Tracking issue: https://github.com/anthropics/claude-code/issues/54208
    """
    normalized = agent_name.replace(".", "-")
    names: list[str] = [agent_name]
    if normalized != agent_name:
        names.append(normalized)
    return names


def _symlink_mem_dir(source: Path, target_root: Path) -> None:
    """Create both the canonical and normalized memory-dir symlinks for *source*.

    If Claude Code has already auto-created an *empty* real directory at the
    normalized path (e.g. ``hammer-mei/``), it is removed and replaced with a
    symlink.  A non-empty directory is left untouched with a warning.
    """
    target_root.mkdir(parents=True, exist_ok=True)
    for link_name in _agent_memory_link_names(source.name):
        target = target_root / link_name
        if target.is_symlink():
            if target.resolve() == source.resolve():
                print(f"exists  {target} -> {source}")
                continue
            target.unlink()
        elif target.is_dir():
            if any(target.iterdir()):
                print(f"warning {target} is a non-empty directory — skipping symlink")
                continue
            # Empty dir auto-created by Claude Code 2.1.121+ (see issue #54208); replace.
            target.rmdir()
        elif target.exists():
            print(f"warning {target} exists and is not a symlink — skipping")
            continue
        target.symlink_to(source)
        print(f"linked  {target} -> {source}")


def _unlink_mem_dir(source: Path, target_root: Path) -> None:
    """Remove all memory-dir symlinks (both canonical and normalized forms)."""
    for link_name in _agent_memory_link_names(source.name):
        target = target_root / link_name
        if target.is_symlink():
            target.unlink()
            print(f"removed {target}")


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
# Project config — user-created TOML tamago.conf + machine-generated machine.env
# ---------------------------------------------------------------------------
#
# tamago.conf is a user-authored TOML file at <project_dir>/.tamago/tamago.conf.
# It is the canonical signal that a project has tamago set up — memory-sync.sh
# exits 0 silently when machine.env is absent (not a tamago project).
#
# machine.env is a shell-sourceable KEY=VALUE file generated by `tamago install`.
# It is gitignored (machine-local) and regenerated on every install.

PROJECT_CONF_NAME = "tamago.conf"  # lives inside <project_dir>/.tamago/
MACHINE_TOML_NAME = "machine.toml"  # machine-local install state (Slice E)

# Global conf and machine.toml paths (Step 2: two-tier refactor).
GLOBAL_CONF_PATH = CONVENTIONAL_ROOT / PROJECT_CONF_NAME   # ~/.tamago/tamago.conf
GLOBAL_MACHINE_TOML_PATH = CONVENTIONAL_ROOT / MACHINE_TOML_NAME  # ~/.tamago/machine.toml


def _write_conf_file(path: Path, lines: list[str]) -> None:
    content = "\n".join(lines) + "\n"
    if path.exists() and path.read_text() == content:
        print(f"exists  {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"updated {path}")


# ---------------------------------------------------------------------------
# TOML config model (Phase 1 / Slice A)
# ---------------------------------------------------------------------------

@dataclass
class ProfileEntry:
    name: str | None = None
    repo: str | None = None


@dataclass
class AgentEntry:
    name: str
    source: str = "tamago"
    scope: str = "project"
    tts: bool = True
    memory: bool = True
    disable: bool = False


@dataclass
class SkillEntry:
    name: str
    source: str = "tamago"
    scope: str = "global"
    path: str | None = None
    disable: bool = False


@dataclass
class PluginEntry:
    name: str
    repo: str                 # required: path to plugin repo (expanduser applied at parse time)
    scope: str = "global"     # "global" | "project"
    disable: bool = False


@dataclass
class TamagoConf:
    profiles: list[ProfileEntry] = dataclass_field(default_factory=list)
    agents: list[AgentEntry] = dataclass_field(default_factory=list)
    skills: list[SkillEntry] = dataclass_field(default_factory=list)
    plugins: list[PluginEntry] = dataclass_field(default_factory=list)
    memory_sync: bool = True


@dataclass
class MachineToml:
    """Machine-local install-time state.  Written by install_from_conf, read by uninstall.

    profiles:    agent-name → absolute path of resolved profile root
    skill_cache: skill-name → absolute path of its skill repo cache dir
    """
    profiles: dict[str, str] = dataclass_field(default_factory=dict)
    skill_cache: dict[str, str] = dataclass_field(default_factory=dict)


def load_tamago_conf(path: Path) -> "TamagoConf | None":
    """Parse a TOML tamago.conf and return a TamagoConf, or None on error.

    Returns None if the file does not exist, is not valid TOML, or cannot be parsed.
    """
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        profiles = [
            ProfileEntry(name=p.get("name"), repo=p.get("repo"))
            for p in raw.get("profiles", [])
        ]
        agents = [
            AgentEntry(
                name=a["name"],
                source=a.get("source", "tamago"),
                scope=a.get("scope", "project"),
                tts=a.get("tts", True),
                memory=a.get("memory", True),
                disable=a.get("disable", False),
            )
            for a in raw.get("agents", [])
            if "name" in a
        ]
        skills = [
            SkillEntry(
                name=s["name"],
                source=os.path.expanduser(s.get("source", "tamago")),
                scope=s.get("scope", "global"),
                path=s.get("path"),
                disable=s.get("disable", False),
            )
            for s in raw.get("skills", [])
            if "name" in s
        ]
        plugins = [
            PluginEntry(
                name=pl["name"],
                repo=os.path.expanduser(pl["repo"]),
                scope=pl.get("scope", "global"),
                disable=pl.get("disable", False),
            )
            for pl in raw.get("plugins", [])
            if "name" in pl and "repo" in pl
        ]
        settings_block = raw.get("settings", {})
        if not isinstance(settings_block, dict):
            return None
        raw_sync = settings_block.get("memory_sync", True)
        if not isinstance(raw_sync, bool):
            return None
        return TamagoConf(
            profiles=profiles,
            agents=agents,
            skills=skills,
            plugins=plugins,
            memory_sync=raw_sync,
        )
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError, KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# machine.toml helpers (Slice E)
# ---------------------------------------------------------------------------

def _toml_string(s: str) -> str:
    """Encode s as a TOML basic string (double-quoted, spec-compliant escaping).

    Escapes backslash, double-quote, and the C0 control characters that TOML
    requires to be escaped in basic strings (U+0000–U+001F).
    """
    # Backslash must be escaped first to avoid double-escaping subsequent replacements.
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    s = s.replace("\t", "\\t")
    # Remaining C0 controls (U+0000–U+001F, excluding the three above)
    s = "".join(
        f"\\u{ord(c):04X}" if (ord(c) < 0x20 and c not in "\n\r\t") else c
        for c in s
    )
    return '"' + s + '"'


def write_machine_toml(path: Path, data: MachineToml) -> None:
    """Write .tamago/machine.toml — machine-local install-time state.

    Creates parent dirs if needed.  Skips the write when content is unchanged
    (idempotent).  The file is gitignored; read by load_machine_toml on uninstall.
    """
    lines = ["# .tamago/machine.toml — auto-generated by tamago install, do not edit\n"]
    if data.profiles:
        lines.append("\n[profiles]\n")
        for name, resolved in data.profiles.items():
            lines.append(f"{_toml_string(name)} = {_toml_string(resolved)}\n")
    if data.skill_cache:
        lines.append("\n[skill_cache]\n")
        for name, cache_path in data.skill_cache.items():
            lines.append(f"{_toml_string(name)} = {_toml_string(cache_path)}\n")
    content = "".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        print(f"exists  {path}")
        return
    path.write_text(content)
    print(f"updated {path}")


def load_machine_toml(path: Path) -> "MachineToml | None":
    """Read .tamago/machine.toml; return None if missing or unparseable."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        profiles = {str(k): str(v) for k, v in raw.get("profiles", {}).items()}
        skill_cache = {str(k): str(v) for k, v in raw.get("skill_cache", {}).items()}
        return MachineToml(profiles=profiles, skill_cache=skill_cache)
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError, ValueError):
        return None



MACHINE_ENV_NAME = "machine.env"  # lives inside <project_dir>/.tamago/


def _shell_quote_value(v: str) -> str:
    """Wrap a string value in single quotes for safe shell sourcing.

    Escapes any embedded single quotes using the 'x'"'"'y' idiom so the result
    is always valid shell regardless of spaces, $, backticks, or other metacharacters.
    """
    return "'" + v.replace("'", "'\\''") + "'"


def write_machine_env(
    path: Path,
    profile_repo: Path | None,
    agent_name: str | None,
    memory_sync: bool = True,
    tts_enabled: bool = True,
) -> None:
    """Write (or remove) .tamago/machine.env — a shell-sourceable KEY=VALUE bridge.

    This file lets bash scripts (memory-sync.sh) read tamago config without a TOML
    parser.  Values containing paths or user-supplied strings are single-quoted so
    sourcing the file is safe even when they contain spaces or shell metacharacters.
    The file is gitignored (machine-local) and regenerated on every install.
    """
    if profile_repo is None:
        if path.exists():
            path.unlink()
            print(f"removed {path}")
        return
    lines = [
        f"PROFILE_REPO={_shell_quote_value(str(profile_repo.resolve()))}",
        f"AGENT_NAME={_shell_quote_value(agent_name or '')}",
        f"MEMORY_SYNC={1 if memory_sync else 0}",
        f"TTS_ENABLED={1 if tts_enabled else 0}",
    ]
    content = "\n".join(lines) + "\n"
    if path.exists() and path.read_text() == content:
        print(f"exists  {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"updated {path}")




# ---------------------------------------------------------------------------
# Settings — agent scope merge/patch
# ---------------------------------------------------------------------------
# Agent-scoped settings ({"agent": "<name>"} + profile overrides) are merged
# into the target settings file using a sidecar-manifest approach so user edits
# survive re-installs cleanly.
#
# Scope routing (mirrors setup_skills):
#   global agent  → patch ~/.claude/settings.json  + ~/.opencode/opencode.json
#   project agent → patch <project>/.claude/settings.json + <project>/.opencode/opencode.json
#
# Sidecar manifest (contribution blob format):
#   Global agent  → ~/.claude/.tamago-agent-manifest.json
#   Project agent → <project>/.claude/.tamago-agent-manifest.json
#
# Tamago global settings (hooks/perms/statusLine) are handled separately by
# patch_global_settings() and are unaffected by the functions below.
#
# Merge layer order (later wins):
#   original user settings
#   + default-agent settings  ({"agent": "<name>"} auto-generated by tamago)
#   + profile override         (<profile>/settings/claude/settings.json)


def _deep_merge_json(base: dict, override: dict) -> dict:
    """Deep-merge *override* into *base*, returning a new dict.

    Merge rules (applied recursively):
      - both values are dicts  → recurse
      - both values are lists  → set union (override items appended after base items,
                                  deduped by JSON serialization)
      - scalar or mixed types  → override wins (last-writer wins)
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge_json(result[key], val)
        elif key in result and isinstance(result[key], list) and isinstance(val, list):
            existing = result[key]
            seen = {json.dumps(item, sort_keys=True) for item in existing}
            additions = [item for item in val if json.dumps(item, sort_keys=True) not in seen]
            result[key] = existing + additions
        else:
            result[key] = val
    return result


def _deep_subtract_json(current: dict, contributed: dict) -> dict:
    """Remove *contributed* entries from *current*, returning a new dict.

    Removal rules (applied recursively):
      - both values are dicts  → recurse; key removed when result is empty
      - both values are lists  → contributed items filtered out (exact JSON equality)
      - scalar                 → key removed only when current value == contributed value
                                  (user-modified values are preserved)

    Known limitation: list deduplication is purely value-based (JSON serialization).
    If the user independently added an item that tamago also contributed, uninstall will
    remove it because the entries are indistinguishable. This is an inherent trade-off of
    the set-union merge design — richer per-item provenance tracking would be needed to
    distinguish tamago-added vs. user-added copies of identical values.
    """
    result = dict(current)
    for key, contributed_val in contributed.items():
        if key not in result:
            continue
        current_val = result[key]
        if isinstance(contributed_val, dict) and isinstance(current_val, dict):
            subtracted = _deep_subtract_json(current_val, contributed_val)
            if subtracted:
                result[key] = subtracted
            else:
                del result[key]
        elif isinstance(contributed_val, list) and isinstance(current_val, list):
            contributed_set = {json.dumps(item, sort_keys=True) for item in contributed_val}
            remaining = [item for item in current_val
                         if json.dumps(item, sort_keys=True) not in contributed_set]
            if remaining:
                result[key] = remaining
            else:
                del result[key]
        elif current_val == contributed_val:
            del result[key]
        # else: user modified the value — leave it untouched
    return result


def _make_default_agent_settings_claude(agent_name: str) -> dict:
    """Return the default Claude Code settings tamago injects for an agent.

    Sets the active agent name so Claude Code loads the correct persona.
    """
    return {"agent": agent_name}


def _make_default_agent_settings_opencode(agent_name: str) -> dict:
    """Return the default OpenCode settings tamago injects for an agent."""
    return {"default_agent": agent_name}


def patch_agent_settings(
    operation: Operation,
    settings_path: Path,
    manifest_path: Path,
    contribution: dict,
) -> None:
    """Merge/unmerge agent-scoped settings into a Claude Code or OpenCode settings file.

    On INSTALL:
      1. Migrates a legacy symlink to a real file when needed.
      2. Subtracts the prior contribution (from sidecar) then deep-merges the new one,
         so re-installs are idempotent even when the contribution changes.
      3. Writes a sidecar manifest recording exactly what was contributed.

    On UNINSTALL:
      - If the file is a legacy symlink, unlinks it directly.
      - Otherwise reads the sidecar and deep-subtracts tamago's contribution.
      - Deletes the sidecar manifest.

    User-modified values (current value ≠ contributed value) are always preserved.
    """
    if operation == Operation.INSTALL:
        # Symlink migration: convert old symlink-based installs to real files.
        # Validate JSON *before* unlinking so we never destroy a symlink and leave
        # a corrupt settings.json behind.
        if settings_path.is_symlink():
            try:
                content = settings_path.read_text()
                json.loads(content)  # validate before destroying symlink
            except (OSError, ValueError):
                content = "{}"
            settings_path.unlink()
            settings_path.write_text(content)
            print(f"migrated {settings_path} (symlink → real file)")

        # Load existing settings; treat corrupt/missing as empty and warn.
        current: dict = {}
        if settings_path.exists():
            try:
                current = json.loads(settings_path.read_text())
            except (OSError, ValueError):
                print(f"warn    {settings_path} is corrupt — treating as empty")

        # Load prior contribution for idempotent re-install (subtract old, merge new).
        old_contribution: dict = {}
        if manifest_path.exists():
            try:
                old_contribution = json.loads(manifest_path.read_text()).get("contribution", {})
            except (OSError, ValueError):
                print(f"warn    {manifest_path} is corrupt — prior contribution not subtracted")

        base = _deep_subtract_json(current, old_contribution)
        new_settings = _deep_merge_json(base, contribution)

        if new_settings != current:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(new_settings, indent=2) + "\n")
            print(f"patched {settings_path}")
        else:
            print(f"exists  {settings_path}")

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"contribution": contribution}, indent=2) + "\n")
        print(f"updated {manifest_path}")

    elif operation == Operation.UNINSTALL:
        # Legacy symlink: just remove it (no manifest to read).
        if settings_path.is_symlink():
            settings_path.unlink()
            print(f"removed {settings_path} (legacy symlink)")
            manifest_path.unlink(missing_ok=True)
            return

        if not manifest_path.exists():
            print(f"skip    {manifest_path} not found — nothing to uninstall")
            return

        try:
            old_contribution = json.loads(manifest_path.read_text()).get("contribution", {})
        except (OSError, ValueError):
            print(f"warn    {manifest_path} is corrupt — skipping to avoid data loss")
            return

        if not settings_path.exists():
            manifest_path.unlink(missing_ok=True)
            print(f"removed {manifest_path}")
            return

        try:
            current = json.loads(settings_path.read_text())
        except (OSError, ValueError):
            print(f"warn    {settings_path} is corrupt — cannot uninstall")
            return

        result = _deep_subtract_json(current, old_contribution)
        if result != current:
            settings_path.write_text(json.dumps(result, indent=2) + "\n")
            print(f"unpatched {settings_path}")
        else:
            print(f"exists  {settings_path} (no agent entries to remove)")

        manifest_path.unlink(missing_ok=True)
        print(f"removed {manifest_path}")


def setup_settings(
    operation: Operation,
    source_root: Path,  # retained for API compatibility; unused internally
    project_root: Path,
    profile_root: Path | None = None,
    install_globally: bool = False,
    agent_name: str | None = None,
):
    """Merge agent settings into .claude/settings.json and .opencode/opencode.json.

    Scope routing (mirrors setup_skills):
      install_globally=True  (global agent):
        - agent settings merged into ~/.claude/settings.json and ~/.opencode/opencode.json
        - <project>/.claude/settings.json is NOT touched
      install_globally=False (project agent):
        - agent settings merged into <project>/.claude/settings.json and
          <project>/.opencode/opencode.json
        - ~/.claude/settings.json is NOT touched (tamago layer handled by patch_global_settings)

    Non-settings JSON files from the profile (e.g. agent-emojis.json) continue to be
    symlinked at the appropriate scope.
    """
    # --- Build Claude contribution: default-agent pointer (if named) + profile override ---
    if agent_name:
        claude_contribution = _make_default_agent_settings_claude(agent_name)
    else:
        print("warn    no agent name in tamago.conf — skipping default-agent pointer in settings")
        claude_contribution = {}
    if profile_root:
        profile_claude_path = profile_root / "settings" / "claude" / "settings.json"
        if profile_claude_path.exists():
            try:
                profile_claude = json.loads(profile_claude_path.read_text())
                claude_contribution = _deep_merge_json(claude_contribution, profile_claude)
            except (OSError, ValueError) as e:
                print(f"warn    could not load profile claude settings: {e}")

    # --- Build OpenCode contribution: default-agent pointer (if named) + profile override ---
    if agent_name:
        opencode_contribution = _make_default_agent_settings_opencode(agent_name)
    else:
        opencode_contribution = {}
    if profile_root:
        profile_opencode_path = profile_root / "settings" / "opencode" / "opencode.json"
        if profile_opencode_path.exists():
            try:
                profile_opencode = json.loads(profile_opencode_path.read_text())
                opencode_contribution = _deep_merge_json(opencode_contribution, profile_opencode)
            except (OSError, ValueError) as e:
                print(f"warn    could not load profile opencode settings: {e}")

    # --- Route patch to the correct scope ---
    if install_globally:
        claude_settings_path = Path("~/.claude/settings.json").expanduser()
        claude_manifest_path = Path("~/.claude/.tamago-agent-manifest.json").expanduser()
        opencode_settings_path = Path("~/.opencode/opencode.json").expanduser()
        opencode_manifest_path = Path("~/.opencode/.tamago-agent-manifest.json").expanduser()
        # Stale manifests at project scope (scope flip: project → global): clean up.
        _stale_claude_m  = project_root / ".claude"   / ".tamago-agent-manifest.json"
        _stale_opencode_m = project_root / ".opencode" / ".tamago-agent-manifest.json"
    else:
        claude_settings_path = project_root / ".claude" / "settings.json"
        claude_manifest_path = project_root / ".claude" / ".tamago-agent-manifest.json"
        opencode_settings_path = project_root / ".opencode" / "opencode.json"
        opencode_manifest_path = project_root / ".opencode" / ".tamago-agent-manifest.json"
        # Stale manifests at global scope (scope flip: global → project): clean up.
        _stale_claude_m  = Path("~/.claude/.tamago-agent-manifest.json").expanduser()
        _stale_opencode_m = Path("~/.opencode/.tamago-agent-manifest.json").expanduser()

    # On install, clean up any stale contribution from the opposite scope first so a
    # scope flip (global ↔ project) doesn't leave the agent registered in two places.
    if operation == Operation.INSTALL:
        if _stale_claude_m.exists():
            patch_agent_settings(Operation.UNINSTALL,
                                  _stale_claude_m.parent / "settings.json",
                                  _stale_claude_m, {})
        if _stale_opencode_m.exists():
            patch_agent_settings(Operation.UNINSTALL,
                                  _stale_opencode_m.parent / "opencode.json",
                                  _stale_opencode_m, {})

    patch_agent_settings(operation, claude_settings_path, claude_manifest_path, claude_contribution)
    patch_agent_settings(operation, opencode_settings_path, opencode_manifest_path, opencode_contribution)

    # --- Symlink non-settings JSON files (e.g. agent-emojis.json) at correct scope ---
    if profile_root:
        claude_dir = profile_root / "settings" / "claude"
        opencode_dir = profile_root / "settings" / "opencode"
        claude_extras = (
            [f for f in sorted(claude_dir.glob("*.json")) if f.name != "settings.json"]
            if claude_dir.is_dir() else []
        )
        opencode_extras = (
            [f for f in sorted(opencode_dir.glob("*.json")) if f.name != "opencode.json"]
            if opencode_dir.is_dir() else []
        )

        if install_globally:
            target_claude = Path("~/.claude").expanduser()
            target_opencode = Path("~/.opencode").expanduser()
        else:
            target_claude = project_root / ".claude"
            target_opencode = project_root / ".opencode"

        if operation == Operation.INSTALL:
            symlink_paths(claude_extras, target_claude)
            symlink_paths(opencode_extras, target_opencode)
        elif operation == Operation.UNINSTALL:
            unlink_paths(claude_extras, target_claude)
            unlink_paths(opencode_extras, target_opencode)




# ---------------------------------------------------------------------------
# Global settings patch/merge (Slice B)
# ---------------------------------------------------------------------------
# Rather than symlinking ~/.claude/settings.json to tamago's source file, we
# *patch* the real ~/.claude/settings.json so user-owned keys (advisorModel,
# nagori hooks, etc.) survive install and uninstall unchanged.
#
# Lifecycle:
#   install-global  → patch_global_settings (INSTALL)  → writes ~/.claude/.tamago-manifest.json
#   uninstall-global → patch_global_settings (UNINSTALL) → reads manifest, removes entries, deletes manifest
#
# Manifest path: ~/.claude/.tamago-manifest.json  (sidecar to the file being patched)
# OpenCode:      still symlinked (no equivalent patching needed — file is ours end-to-end)


def _read_tamago_source_hooks(source_settings: dict) -> dict[str, list[str]]:
    """Extract hook commands from tamago source settings, keyed by event name.

    Only reads from default matchers (those with no 'matcher' key or matcher == '').
    Returns a dict like {"SessionStart": ["cmd1"], "Stop": ["cmd2"]}.
    """
    result: dict[str, list[str]] = {}
    for event, matchers in source_settings.get("hooks", {}).items():
        cmds: list[str] = []
        for matcher in matchers:
            if matcher.get("matcher", "") == "":
                for h in matcher.get("hooks", []):
                    if h.get("type") == "command" and "command" in h:
                        cmds.append(h["command"])
        if cmds:
            result[event] = cmds
    return result


def _expand_home_in_str(s: str) -> str:
    """Expand ~ to the absolute home directory in a string.

    Claude Code expands ~ in commands before matching against permission patterns,
    but does NOT expand ~ in the patterns themselves.  To ensure patterns match,
    we expand ~ at inject-time so the stored pattern uses the absolute path.
    """
    import os
    home = os.path.expanduser("~")
    return s.replace("~", home)


def _read_tamago_source_perms(source_settings: dict) -> list[str]:
    """Extract permissions.allow list from tamago source settings.

    Expands ~ to the absolute home path so injected patterns match commands
    where Claude Code has already expanded ~.
    """
    perms = source_settings.get("permissions", {}).get("allow", [])
    return [_expand_home_in_str(p) for p in perms]


def _read_tamago_source_additional_dirs(source_settings: dict) -> list[str]:
    """Extract permissions.additionalDirectories list from tamago source settings.

    Expands ~ to the absolute home path for consistent path comparison.
    """
    dirs = source_settings.get("permissions", {}).get("additionalDirectories", [])
    return [_expand_home_in_str(d) for d in dirs]


def patch_settings(
    path: Path,
    hook_commands: dict[str, list[str]],
    perms: list[str],
    status_line: dict | None,
    manifest_path: Path,
    additional_dirs: list[str] | None = None,
) -> None:
    """Patch a Claude Code settings.json with tamago entries (hooks, perms, statusLine, additionalDirectories).

    Idempotent: entries already present are skipped (dedup by exact command string
    for hooks, set membership for perms/additionalDirectories).

    Migration: if path is a symlink, it is converted to a regular file first so
    subsequent user edits (or tamago source changes) are independent.

    Sidecar: a JSON manifest at manifest_path records exactly what was injected;
    unpatch_settings uses it to remove only tamago's entries, leaving user additions
    untouched.
    """
    import json

    # Handle symlink migration: read content, unlink, write as real file.
    # Track whether we migrated so we can pre-populate the manifest below.
    migrated_from_symlink = False
    if path.is_symlink():
        try:
            content = path.read_text()
        except OSError:
            # Dangling symlink — target is gone.  Unlink and start fresh.
            path.unlink()
            content = "{}"
        else:
            path.unlink()
            path.write_text(content)
        migrated_from_symlink = True
        print(f"migrated {path} (symlink → real file)")

    # Load existing settings or start fresh
    current: dict = json.loads(path.read_text()) if path.exists() else {}

    changed = False

    additional_dirs = additional_dirs or []

    # Load existing manifest (cumulative — re-installs must not wipe prior injection record)
    _empty_manifest: dict = {
        "injected_perms": [],
        "injected_hooks": {},
        "injected_status_line": False,
        "injected_additional_dirs": [],
    }
    if manifest_path.exists():
        try:
            manifest: dict = json.loads(manifest_path.read_text())
            manifest.setdefault("injected_perms", [])
            manifest.setdefault("injected_hooks", {})
            manifest.setdefault("injected_status_line", False)
            manifest.setdefault("injected_additional_dirs", [])
        except (OSError, ValueError):
            manifest = _empty_manifest
    else:
        manifest = _empty_manifest

    # Symlink migration: claim ownership of tamago entries that were already in the
    # file (because the file WAS tamago's own settings.json via symlink).  The normal
    # dedup loop will skip them (already present), so we record them in the manifest
    # here to ensure uninstall can remove them later.
    if migrated_from_symlink:
        already_perms = set(current.get("permissions", {}).get("allow", []))
        for p in perms:
            if p in already_perms and p not in manifest["injected_perms"]:
                manifest["injected_perms"].append(p)
        for event, commands in hook_commands.items():
            already_cmds = {
                h["command"]
                for m in current.get("hooks", {}).get(event, [])
                if m.get("matcher", "") == ""
                for h in m.get("hooks", [])
                if h.get("type") == "command" and "command" in h
            }
            for cmd in commands:
                injected = manifest["injected_hooks"].setdefault(event, [])
                if cmd in already_cmds and cmd not in injected:
                    injected.append(cmd)
        if status_line is not None and current.get("statusLine") == status_line:
            manifest["injected_status_line"] = True
        already_add_dirs = set(current.get("permissions", {}).get("additionalDirectories", []))
        for d in additional_dirs:
            if d in already_add_dirs and d not in manifest["injected_additional_dirs"]:
                manifest["injected_additional_dirs"].append(d)

    # 1. Permissions — set union (append new ones after existing, preserve order)
    existing_perms: list = current.get("permissions", {}).get("allow", [])
    existing_perm_set = set(existing_perms)
    to_add = [p for p in perms if p not in existing_perm_set]
    if to_add:
        current.setdefault("permissions", {}).setdefault("allow", []).extend(to_add)
        manifest["injected_perms"].extend(to_add)  # extend (not assign) — cumulative
        changed = True

    # 2. Hooks — add missing commands into the default (no-matcher) block
    for event, commands in hook_commands.items():
        event_matchers: list = current.get("hooks", {}).get(event, [])
        default_block: dict | None = next(
            (m for m in event_matchers if m.get("matcher", "") == ""),
            None,
        )
        # Only count type=command entries for dedup (mirrors _read_tamago_source_hooks)
        existing_cmds = {
            h["command"]
            for h in (default_block.get("hooks", []) if default_block else [])
            if h.get("type") == "command" and "command" in h
        }

        to_inject = [cmd for cmd in commands if cmd not in existing_cmds]
        if to_inject:
            if default_block is None:
                default_block = {"hooks": []}
                current.setdefault("hooks", {}).setdefault(event, []).insert(0, default_block)
            for cmd in to_inject:
                default_block.setdefault("hooks", []).append({"type": "command", "command": cmd})
            manifest["injected_hooks"].setdefault(event, []).extend(to_inject)
            changed = True

    # 3. statusLine — inject only if the key is absent
    if status_line is not None and "statusLine" not in current:
        current["statusLine"] = status_line
        manifest["injected_status_line"] = True
        changed = True

    # 4. additionalDirectories — set union (same dedup policy as perms)
    existing_add_dirs: list = current.get("permissions", {}).get("additionalDirectories", [])
    existing_add_dirs_set = set(existing_add_dirs)
    dirs_to_add = [d for d in additional_dirs if d not in existing_add_dirs_set]
    if dirs_to_add:
        current.setdefault("permissions", {}).setdefault("additionalDirectories", []).extend(dirs_to_add)
        manifest["injected_additional_dirs"].extend(dirs_to_add)
        changed = True

    # Write settings back only if something changed (avoid spurious reformatting)
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2) + "\n")
        print(f"patched {path}")
    else:
        print(f"exists  {path}")

    # Write sidecar manifest (always, to keep it current even on no-op runs)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"updated {manifest_path}")


def unpatch_settings(path: Path, manifest_path: Path) -> None:
    """Remove tamago-injected entries from a Claude Code settings.json.

    Reads the sidecar manifest to know exactly what tamago added. Entries that
    the user modified after injection are left in place (command string must match
    exactly for removal to trigger — a modified command is treated as user-owned).
    """
    import json

    if not manifest_path.exists():
        print(f"skip    {manifest_path} not found — nothing to uninstall")
        return

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        print(f"warn    {manifest_path} is corrupt — skipping uninstall to avoid data loss")
        return

    if not path.exists():
        manifest_path.unlink(missing_ok=True)
        print(f"removed {manifest_path}")
        return

    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        print(f"warn    {path} is corrupt — cannot uninstall; remove {manifest_path} manually")
        return

    changed = False

    # 1. Remove injected permissions (exact string match only)
    injected_perms = set(manifest.get("injected_perms", []))
    if injected_perms and "permissions" in current and "allow" in current["permissions"]:
        new_perms = [p for p in current["permissions"]["allow"] if p not in injected_perms]
        if new_perms != current["permissions"]["allow"]:
            current["permissions"]["allow"] = new_perms
            changed = True

    # 2. Remove injected hook commands (exact command string match).
    #    Drop empty default blocks and empty event keys so no residue is left behind.
    for event, commands in manifest.get("injected_hooks", {}).items():
        cmd_set = set(commands)
        event_list = current.get("hooks", {}).get(event, [])
        new_event_list = []
        for matcher in event_list:
            if matcher.get("matcher", "") != "":
                new_event_list.append(matcher)
                continue  # skip non-default matchers — tamago never touches them
            hooks = matcher.get("hooks", [])
            new_hooks = [h for h in hooks if h.get("command") not in cmd_set]
            if new_hooks != hooks:
                changed = True
            if new_hooks:
                matcher["hooks"] = new_hooks
                new_event_list.append(matcher)
            # else: block now empty — drop it entirely
        if len(new_event_list) != len(event_list):
            if new_event_list:
                current["hooks"][event] = new_event_list
            elif event in current.get("hooks", {}):
                del current["hooks"][event]

    # 3. Remove injected statusLine (only if we added it)
    if manifest.get("injected_status_line") and "statusLine" in current:
        del current["statusLine"]
        changed = True

    # 4. Remove injected additionalDirectories (exact string match only)
    injected_add_dirs = set(manifest.get("injected_additional_dirs", []))
    if injected_add_dirs and "permissions" in current and "additionalDirectories" in current["permissions"]:
        new_add_dirs = [d for d in current["permissions"]["additionalDirectories"] if d not in injected_add_dirs]
        if new_add_dirs != current["permissions"]["additionalDirectories"]:
            if new_add_dirs:
                current["permissions"]["additionalDirectories"] = new_add_dirs
            else:
                del current["permissions"]["additionalDirectories"]
            changed = True

    if changed:
        path.write_text(json.dumps(current, indent=2) + "\n")
        print(f"unpatched {path}")
    else:
        print(f"exists  {path} (no tamago entries to remove)")

    manifest_path.unlink()
    print(f"removed {manifest_path}")


def patch_global_settings(operation: Operation, source_root: Path) -> None:
    """Patch (or unpatch) ~/.claude/settings.json with tamago entries.

    Reads tamago's source settings/claude/settings.json to determine what to inject.
    Writes a sidecar manifest at ~/.claude/.tamago-manifest.json for clean uninstall.
    """
    import json

    settings_path = Path("~/.claude/settings.json").expanduser()
    manifest_path = Path("~/.claude/.tamago-manifest.json").expanduser()

    if operation == Operation.INSTALL:
        source_path = source_root / "settings" / "claude" / "settings.json"
        if not source_path.exists():
            print(f"warn    {source_path} not found — patching with empty config")
        source_settings: dict = (
            json.loads(source_path.read_text()) if source_path.exists() else {}
        )
        hook_commands = _read_tamago_source_hooks(source_settings)
        perms = _read_tamago_source_perms(source_settings)
        additional_dirs = _read_tamago_source_additional_dirs(source_settings)
        status_line = source_settings.get("statusLine")
        patch_settings(settings_path, hook_commands, perms, status_line, manifest_path, additional_dirs)
    elif operation == Operation.UNINSTALL:
        unpatch_settings(settings_path, manifest_path)


def patch_opencode_global_settings(operation: Operation, source_root: Path) -> None:
    """Patch (or unpatch) ~/.opencode/opencode.json with tamago entries.

    Mirrors patch_global_settings() for OpenCode.  The key immediate benefit is
    symlink migration: if ~/.opencode/opencode.json is currently a tamago symlink,
    install converts it to a real file so user-added OpenCode config (model prefs,
    provider settings, etc.) survives future 'tamago update' runs.

    OpenCode hooks live in the TypeScript plugin (memory-bootstrap.ts) rather than
    the JSON config, so no hooks or permissions are injected for now.  The sidecar
    manifest at ~/.opencode/.tamago-manifest.json is still written so that uninstall
    can cleanly remove tamago's footprint even when future slices start injecting
    OpenCode-specific keys.
    """
    settings_path = Path("~/.opencode/opencode.json").expanduser()
    manifest_path = Path("~/.opencode/.tamago-manifest.json").expanduser()

    if operation == Operation.INSTALL:
        # Nothing to inject into opencode.json yet — but patch_settings() handles
        # symlink migration and manifest writing for us.
        patch_settings(settings_path, {}, [], None, manifest_path)
    elif operation == Operation.UNINSTALL:
        unpatch_settings(settings_path, manifest_path)


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
    scope: str = "project",
) -> None:
    """Merge tamago-agent-base.md + persona file → target_dir/<agent_name>.md.

    scope controls the Claude Code ``memory:`` frontmatter field and the memory
    path references emitted in the agent body:
      "project"  → memory: project, .claude/agent-memory/ (project-relative)
      "user"     → memory: user,    ~/.claude/agent-memory/ (home-relative)
    """
    # agent_name: strip the ".persona" suffix  (hammer.mei.persona.md → hammer.mei)
    agent_name = persona_file.stem  # e.g. "hammer.mei.persona"
    if agent_name.endswith(".persona"):
        agent_name = agent_name[: -len(".persona")]

    base_file = source_root / "docs" / "tamago-agent-base.md"
    if not base_file.exists():
        raise Exception(f"tamago-agent-base.md not found: {base_file}")

    profile_path = str(profile_root.resolve())
    memory_path = f"{profile_path}/agents/memory/{agent_name}"

    agent_memory_dir = agent_name.replace(".", "-")  # normalized per CC convention

    # Scope-dependent memory values used to expand template variables.
    if scope == "user":
        _mem_root = "~/.claude/agent-memory/"
        _mem_location_desc = "a user-scope directory at `~/.claude/agent-memory/`"
        _mem_path_warning = (
            f"> ⚠️ **Always use the path** `~/.claude/agent-memory/{agent_memory_dir}/`"
            " for all Read/Write\n"
            "> tool calls. Claude Code does not require permission approval to access"
            " this directory.\n"
            "> Never write directly to the absolute profile source path."
        )
        _mem_frontmatter_value = "user"
    else:  # "project"
        _mem_root = ".claude/agent-memory/"
        _mem_location_desc = "project-scope symlinks under `.claude/agent-memory/`"
        _mem_path_warning = (
            f"> ⚠️ **Always use the symlink path** `.claude/agent-memory/{agent_memory_dir}/`"
            " for all Read/Write\n"
            "> tool calls — it lives inside the project directory and never requires"
            " permission approval.\n"
            "> Never write to `~/.claude/` or any absolute profile path — those are"
            " outside the project\n"
            "> scope and will trigger approval prompts."
        )
        _mem_frontmatter_value = "project"

    def _sub(text: str) -> str:
        return (
            text.replace("{{AGENT_NAME}}", agent_name)
                .replace("{{AGENT_MEMORY_DIR}}", agent_memory_dir)
                .replace("{{PROFILE_REPO}}", profile_path)
                .replace("{{AGENT_MEMORY_PATH}}", memory_path)
                .replace("{{AGENT_MEMORY_ROOT}}", _mem_root)
                .replace("{{AGENT_MEMORY_LOCATION_DESC}}", _mem_location_desc)
                .replace("{{AGENT_MEMORY_PATH_WARNING}}", _mem_path_warning)
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

    # Override memory scope in frontmatter to match install scope so Claude Code
    # injects the correct memory path for this deployment.
    if frontmatter:
        frontmatter = re.sub(
            r"^(memory:\s*)\S+",
            rf"\g<1>{_mem_frontmatter_value}",
            frontmatter,
            flags=re.MULTILINE,
        )

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
        if target.exists() and GENERATED_HEADER_MARKER in target.read_text()[:1024]:
            target.unlink()
            print(f"removed {target}")


def _remove_agent_files_if_managed(agent_name: str, target_dirs: list[Path], label: str = "") -> None:
    """Remove a tamago-managed agent file (symlink or generated) from each target dir.

    Removes the file only if it is a symlink OR contains the GENERATED_HEADER_MARKER.
    Silently skips if the file is absent.  Warns if the file exists but is user-owned.
    """
    suffix = f" ({label})" if label else ""
    for target_dir in target_dirs:
        target = target_dir / f"{agent_name}.md"
        if target.is_symlink():
            target.unlink()
            print(f"removed {target}{suffix}")
        elif target.exists():
            if GENERATED_HEADER_MARKER in target.read_text()[:1024]:
                target.unlink()
                print(f"removed {target}{suffix}")
            else:
                print(f"warning {target} is not tamago-managed — leaving it in place")


def setup_agents(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
    tts_enabled: bool = True,
    disabled_agents: "set[str] | None" = None,
    install_globally: bool = False,
):
    """Install/uninstall agents into project_root (or ~/.claude/agents/ when install_globally=True).

    disabled_agents:   skip entirely — no agent file, no memory dir.
    install_globally:  when True, install to ~/.claude/agents/ (and ~/.opencode/agents/)
                       instead of the project-level agents dir.  Memory dirs follow the
                       same routing.  Use for the global-conf path (tamago install-global).
    On UNINSTALL: the scope matches the current install_globally flag.  If scope changed
    between installs without re-running install, orphaned files may remain — acceptable;
    the user can clean them up manually or re-install first.
    """
    if disabled_agents is None:
        disabled_agents = set()

    source_opencode_plugin_root = source_root / "settings" / "opencode" / "plugins"

    target_claude_agent_root = project_root / ".claude" / "agents"
    target_claude_agent_mem_root = project_root / ".claude" / "agent-memory"
    target_opencode_agent_root = project_root / ".opencode" / "agents"
    target_opencode_plugin_root = project_root / ".opencode" / "plugins"

    home_claude_agents = Path("~/.claude/agents").expanduser()
    home_opencode_agents = Path("~/.opencode/agents").expanduser()

    # All dirs an agent might have been installed to (for disabled cleanup / uninstall)
    all_claude_dirs = [target_claude_agent_root, home_claude_agents]
    all_opencode_dirs = [target_opencode_agent_root, home_opencode_agents]

    # Route all agents to the same tier (global or project) — no per-agent routing.
    claude_agents_dir  = home_claude_agents  if install_globally else target_claude_agent_root
    opencode_agents_dir = home_opencode_agents if install_globally else target_opencode_agent_root

    opencode_plugin_files = sub_paths(
        source_opencode_plugin_root, lambda p: p.is_file() and p.suffix == ".ts"
    )

    # Generic agents from tamago (code-reviewer.md, technical-writer.md, etc.)
    tamago_agent_files = sub_paths(
        source_root / "agents", lambda p: p.is_file() and p.suffix == ".md"
    )
    enabled_tamago_agents = [f for f in tamago_agent_files if f.stem not in disabled_agents]

    if operation == Operation.INSTALL:
        # Remove files for disabled agents from all possible locations
        for agent_name in disabled_agents:
            _remove_agent_files_if_managed(agent_name, all_claude_dirs + all_opencode_dirs, "disabled")

        if install_globally:
            # Remove project-level files for all agents (scope flip: project → global)
            for f in enabled_tamago_agents:
                _remove_agent_files_if_managed(
                    f.stem,
                    [target_claude_agent_root, target_opencode_agent_root],
                    "moved to global scope",
                )

        # 1. Symlink tamago generic agents — routed to project or global tier
        for f in enabled_tamago_agents:
            symlink_paths([f], claude_agents_dir)
            symlink_paths([f], opencode_agents_dir)
        symlink_paths(opencode_plugin_files, target_opencode_plugin_root)

        # 2. Merge persona agents from profile (*.persona.md → generated *.md)
        #    Plain *.md files in profile/agents/ are symlinked directly.
        if profile_root and (profile_root / "agents").is_dir():
            profile_agent_files = sub_paths(
                profile_root / "agents",
                lambda p: p.is_file() and p.suffix == ".md" and not p.stem.endswith(".persona"),
            )
            enabled_profile_agents = [f for f in profile_agent_files if f.stem not in disabled_agents]

            if install_globally:
                # Remove project-level files for all profile agents (scope flip: project → global)
                for f in enabled_profile_agents:
                    _remove_agent_files_if_managed(
                        f.stem,
                        [target_claude_agent_root, target_opencode_agent_root],
                        "moved to global scope",
                    )

            for f in enabled_profile_agents:
                symlink_paths([f], claude_agents_dir)
                symlink_paths([f], opencode_agents_dir)

            agent_scope = "user" if install_globally else "project"
            for persona_file in sorted((profile_root / "agents").iterdir()):
                if persona_file.is_file() and persona_file.name.endswith(".persona.md"):
                    agent_name = persona_file.stem
                    if agent_name.endswith(".persona"):
                        agent_name = agent_name[: -len(".persona")]
                    if agent_name in disabled_agents:
                        continue
                    _merge_agent(source_root, profile_root, persona_file, claude_agents_dir, tts_enabled, scope=agent_scope)
                    _merge_agent(source_root, profile_root, persona_file, opencode_agents_dir, tts_enabled, scope=agent_scope)

        # 3. Memory dirs — follow agent scope (same as agent file)
        #    Global agents: ~/.claude/agent-memory/  (accessible from any project)
        #    Project agents: {project}/.claude/agent-memory/
        home_agent_mem_root = Path("~/.claude/agent-memory").expanduser()

        if profile_root and (profile_root / "agents" / "memory").is_dir():
            mem_source = profile_root / "agents" / "memory"
        elif (source_root / "agents" / "memory").is_dir():
            mem_source = source_root / "agents" / "memory"
        else:
            mem_source = None

        if mem_source:
            agent_mem_dirs = sub_paths(mem_source, lambda p: p.is_dir() and not p.name.startswith("."))
            enabled_mem_dirs = [d for d in agent_mem_dirs if d.name not in disabled_agents]

            if install_globally:
                # Remove project-level memory symlinks (scope flip: project → global)
                for d in enabled_mem_dirs:
                    _unlink_mem_dir(d, target_claude_agent_mem_root)
                for d in enabled_mem_dirs:
                    _symlink_mem_dir(d, home_agent_mem_root)
            else:
                for d in enabled_mem_dirs:
                    _symlink_mem_dir(d, target_claude_agent_mem_root)

    elif operation == Operation.UNINSTALL:
        # Determine which dirs to clean up based on current install_globally flag.
        # If scope changed between installs without re-running install, an orphan may remain.
        uninstall_dirs = (
            [home_claude_agents, home_opencode_agents]
            if install_globally
            else [target_claude_agent_root, target_opencode_agent_root]
        )

        for f in enabled_tamago_agents:
            for d in uninstall_dirs:
                t = d / f.name
                if t.is_symlink():
                    t.unlink()
                    print(f"removed {t}")
        unlink_paths(opencode_plugin_files, target_opencode_plugin_root)

        # Remove symlinked plain profile agents and generated persona agents
        if profile_root and (profile_root / "agents").is_dir():
            profile_agent_files = sub_paths(
                profile_root / "agents",
                lambda p: p.is_file() and p.suffix == ".md" and not p.stem.endswith(".persona"),
            )
            enabled_profile_agents = [f for f in profile_agent_files if f.stem not in disabled_agents]
            for f in enabled_profile_agents:
                for d in uninstall_dirs:
                    t = d / f.name
                    if t.is_symlink():
                        t.unlink()
                        print(f"removed {t}")

            # Generated persona agents: remove from their scoped dirs
            for persona_file in (profile_root / "agents").iterdir():
                if not (persona_file.is_file() and persona_file.name.endswith(".persona.md")):
                    continue
                name = persona_file.stem
                if name.endswith(".persona"):
                    name = name[: -len(".persona")]
                if name in disabled_agents:
                    continue
                for d in uninstall_dirs:
                    _remove_agent_files_if_managed(name, [d])

        # Remove memory symlinks — scope-aware (mirrors INSTALL routing).
        # Both canonical (hammer.mei) and normalized (hammer-mei) forms are removed.
        mem_source = (
            (profile_root / "agents" / "memory") if profile_root
            else (source_root / "agents" / "memory")
        )
        home_agent_mem_root = Path("~/.claude/agent-memory").expanduser()
        if mem_source.is_dir():
            agent_mem_dirs = sub_paths(mem_source, lambda p: p.is_dir() and not p.name.startswith("."))
            enabled_mem_dirs = [d for d in agent_mem_dirs if d.name not in disabled_agents]
            mem_root = home_agent_mem_root if install_globally else target_claude_agent_mem_root
            for d in enabled_mem_dirs:
                _unlink_mem_dir(d, mem_root)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def setup_skills(
    operation: Operation,
    source_root: Path,
    project_root: Path,
    profile_root: Path | None = None,
    disabled_skills: "set[str] | None" = None,
    install_globally: bool = False,
):
    """Symlink skills into the appropriate location.

    Routing rules:
      - Tamago built-in skills (tamago/skills/) → always ~/.claude/skills/ (global),
        regardless of install_globally.  They are shared across all projects.
      - Profile skills (profile/skills/) → follow install_globally:
          install_globally=True  → ~/.claude/skills/  (global)
          install_globally=False → <project>/.claude/skills/

    When a profile skill shadows a tamago built-in (same name), the profile version is
    used and profile routing rules apply.

    Skills come from two sources (profile skills take precedence over tamago skills):
      1. tamago/skills/  — built-in skills bundled with tamago
      2. profile/skills/ — custom skills defined in the profile repo (optional)

    disabled_skills:  names to skip entirely (remove existing symlinks on INSTALL).
    install_globally: when True, install profile skills globally (used for the global-conf
                      path: tamago install-global).
    """
    if disabled_skills is None:
        disabled_skills = set()

    home_claude_skills = Path("~/.claude/skills").expanduser()
    home_opencode_skills = Path("~/.opencode/skills").expanduser()
    project_claude_skills = project_root / ".claude" / "skills"
    project_opencode_skills = project_root / ".opencode" / "skills"

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

    # Track which names are provided by the profile (profile shadows tamago of same name)
    profile_skill_names = {p.name for p in profile_skill_dirs}

    # Tamago built-ins NOT shadowed by a profile skill
    tamago_only_dirs = [d for d in tamago_skill_dirs if d.name not in profile_skill_names]

    # Apply disabled filter per source
    tamago_enabled = [d for d in tamago_only_dirs if d.name not in disabled_skills]
    profile_enabled = [d for d in profile_skill_dirs if d.name not in disabled_skills]

    # Route tamago built-ins: always global (shared across all projects)
    tamago_global: list[Path] = tamago_enabled
    tamago_local:  list[Path] = []

    # Route profile skills: follow install_globally
    if install_globally:
        profile_global: list[Path] = profile_enabled
        profile_local:  list[Path] = []
    else:
        profile_global = []
        profile_local  = profile_enabled  # project-local when not installing globally

    global_skill_dirs = tamago_global + profile_global
    local_skill_dirs  = tamago_local  + profile_local
    all_enabled_dirs  = tamago_enabled + profile_enabled  # used for UNINSTALL

    local_bin = Path("~/.local/bin").expanduser()

    if operation == Operation.INSTALL:
        # Remove disabled skills from all possible locations
        for skill_name in disabled_skills:
            for skills_root in (home_claude_skills, home_opencode_skills,
                                project_claude_skills, project_opencode_skills):
                target = skills_root / skill_name
                if target.is_symlink():
                    target.unlink()
                    print(f"removed {target} (disabled)")

        # Remove project-level symlinks for skills moving to global scope
        for d in global_skill_dirs:
            for skills_root in (project_claude_skills, project_opencode_skills):
                target = skills_root / d.name
                if target.is_symlink():
                    target.unlink()
                    print(f"removed {target} (moved to global scope)")

        # Install globally
        symlink_paths(global_skill_dirs, home_claude_skills)
        symlink_paths(global_skill_dirs, home_opencode_skills)

        # Remove global symlinks for skills moving to project scope
        for d in local_skill_dirs:
            for skills_root in (home_claude_skills, home_opencode_skills):
                target = skills_root / d.name
                if target.is_symlink():
                    target.unlink()
                    print(f"removed {target} (moved to project scope)")

        # Install project-scoped skills
        symlink_paths(local_skill_dirs, project_claude_skills)
        symlink_paths(local_skill_dirs, project_opencode_skills)

        # Install CLI entry points to ~/.local/bin for ALL enabled skills that have a bin/ dir.
        # bin/ contains system-level CLI tools (e.g. tts-cli.py) that must be on PATH
        # regardless of whether the skill itself is installed globally or project-scoped.
        for d in all_enabled_dirs:
            skill_bin = d / "bin"
            if skill_bin.is_dir():
                bin_files = [
                    f for f in sorted(skill_bin.iterdir())
                    if not f.name.startswith(".") and not f.is_dir()
                ]
                if bin_files:
                    symlink_paths(bin_files, local_bin)

    elif operation == Operation.UNINSTALL:
        # Remove from all possible locations (handles scope changes between installs)
        unlink_paths(all_enabled_dirs, home_claude_skills)
        unlink_paths(all_enabled_dirs, home_opencode_skills)
        unlink_paths(all_enabled_dirs, project_claude_skills)
        unlink_paths(all_enabled_dirs, project_opencode_skills)
        # Remove CLI entry points from ~/.local/bin
        for d in all_enabled_dirs:
            skill_bin = d / "bin"
            if skill_bin.is_dir():
                bin_files = [
                    f for f in sorted(skill_bin.iterdir())
                    if not f.name.startswith(".") and not f.is_dir()
                ]
                if bin_files:
                    unlink_paths(bin_files, local_bin)


# ---------------------------------------------------------------------------
# ~/.zshrc env injection
# ---------------------------------------------------------------------------

def setup_shell_env(operation: Operation, source_root: Path):
    """Migration cleanup: remove any ASSISTANT_SETUP_REPO that older tamago versions
    auto-injected into ~/.zshrc.

    Install is intentionally a no-op.  Tamago no longer auto-injects this variable —
    if you need to point tamago at a non-conventional source directory, set
    ASSISTANT_SETUP_REPO manually in your shell profile.

    Uninstall removes stale entries written by older tamago versions.
    """
    if operation == Operation.INSTALL:
        return

    # UNINSTALL: clean up any entry written by an older tamago.
    zshrc = Path("~/.zshrc").expanduser()
    if not zshrc.exists():
        return
    env_key = "ASSISTANT_SETUP_REPO"
    marker = f"{env_key}="
    comment = "# Tamago assistant repo"
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

def pull_repo(path: Path, label: str) -> None:
    """git pull --rebase on a repo; warn on failure, never block the install.

    Skips silently when:
    - path is not a git repo
    - no remote is configured (e.g. freshly-hatched local-only profile)
    - current branch has no upstream tracking branch set
    """
    if not (path / ".git").exists():
        return
    # Check for any configured remote — no remote means nothing to pull from.
    remote_check = subprocess.run(
        ["git", "-C", str(path), "remote"],
        capture_output=True, text=True,
    )
    if not remote_check.stdout.strip():
        print(f"info    {label}: no remote configured — skipping pull")
        return
    # Check for an upstream tracking branch on the current branch.
    upstream_check = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if upstream_check.returncode != 0:
        print(f"info    {label}: no upstream branch — skipping pull")
        return
    result = subprocess.run(
        ["git", "-C", str(path), "pull", "--rebase", "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"warning git pull failed in {label}: {result.stderr.strip()}", file=sys.stderr)
    else:
        print(f"pulled  {label}")


# ---------------------------------------------------------------------------
# External skill repo helpers (Slice D)
# ---------------------------------------------------------------------------

def _skill_repo_cache_dir(url: str, cache_root: Path) -> Path:
    """Compute a stable per-URL cache directory path.

    Uses the first 16 hex chars of SHA-256(url) as the dir name.  The hash
    is of the literal URL string — no normalization — so callers must use
    the exact same URL string in every reference to the same repo.
    """
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    return cache_root / key


def _clone_or_reuse_skill_repo(url: str, cache_dir: Path) -> Path:
    """Return cache_dir pointing to a valid git clone of url.

    On cache miss: clones url into cache_dir and returns it.
    On cache hit: prints 'cached' and returns immediately without pulling
    (callers that want updates should call _pull_skill_repos separately).
    Raises Exception if cache_dir exists but is not a git repo.
    """
    if cache_dir.is_dir() and (cache_dir / ".git").exists():
        print(f"cached  {url}")
        return cache_dir
    if cache_dir.exists():
        raise Exception(f"Cache path exists but is not a git repo: {cache_dir}")
    print(f"cloning {url}")
    print(f"     → {cache_dir}")
    result = subprocess.run(
        ["git", "clone", url, str(cache_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise Exception(f"git clone failed for {url}:\n{result.stderr.strip()}")
    return cache_dir


def _resolve_external_skill_dir(skill: "SkillEntry", cache_root: Path) -> Path:
    """Return the directory to symlink for an external (URL-sourced) skill.

    Single-skill repos (no path): the cache dir itself.
    Monorepos   (path set):      cache_dir / skill.path  (must exist).
    """
    cache_dir = _skill_repo_cache_dir(skill.source, cache_root)
    repo_dir = _clone_or_reuse_skill_repo(skill.source, cache_dir)
    if skill.path:
        skill_dir = repo_dir / skill.path
        if not skill_dir.is_dir():
            raise Exception(f"Skill path not found in repo: {skill_dir}")
        return skill_dir
    return repo_dir


def _pull_skill_repos(conf_skills: list["SkillEntry"], cache_root: Path) -> None:
    """Pull (update) each unique URL-sourced skill repo once.

    Deduplicates by URL so monorepos referenced by multiple skill entries
    are only pulled once.  Skips repos not yet cloned — they'll be cloned
    fresh when setup_external_skills runs.
    """
    seen: set[str] = set()
    for skill in conf_skills:
        url = skill.source
        if url in ("tamago", "profile") or url in seen:
            continue
        seen.add(url)
        cache_dir = _skill_repo_cache_dir(url, cache_root)
        if cache_dir.is_dir() and (cache_dir / ".git").exists():
            pull_repo(cache_dir, url)


def setup_external_skills(
    operation: Operation,
    conf_skills: list["SkillEntry"],
    project_root: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> int:
    """Symlink (or remove) external (URL-sourced) skills.

    Processes only SkillEntry objects where source is a git URL (not
    "tamago" or "profile").  Supports both project and global scope.
    Returns 0 on success, 1 if any skill fails to clone, resolve, or link.
    """
    errors = 0

    home_claude_skills   = Path("~/.claude/skills").expanduser()
    home_opencode_skills = Path("~/.opencode/skills").expanduser()
    project_claude_skills   = project_root / ".claude"   / "skills"
    project_opencode_skills = project_root / ".opencode" / "skills"
    local_bin = Path("~/.local/bin").expanduser()

    for skill in conf_skills:
        url = skill.source
        if url in ("tamago", "profile"):
            continue

        # Route by scope — mirrors the logic in setup_skills
        if skill.scope == "global":
            skills_roots = (home_claude_skills, home_opencode_skills)
            stale_roots  = (project_claude_skills, project_opencode_skills)
        else:  # project scope (default)
            skills_roots = (project_claude_skills, project_opencode_skills)
            stale_roots  = (home_claude_skills, home_opencode_skills)

        if operation == Operation.INSTALL:
            if skill.disable:
                # Remove from all possible locations; leave cache dir intact
                for skills_root in (*skills_roots, *stale_roots):
                    target = skills_root / skill.name
                    if target.is_symlink():
                        target.unlink()
                        print(f"removed {target} (disabled)")
                continue
            try:
                skill_dir = _resolve_external_skill_dir(skill, cache_root)

                # Clean up stale symlinks left from a previous scope (handles scope changes)
                for stale_root in stale_roots:
                    stale = stale_root / skill.name
                    if stale.is_symlink():
                        stale.unlink()
                        print(f"removed {stale} (scope changed)")

                # Install into target scope locations
                for skills_root in skills_roots:
                    target = skills_root / skill.name
                    skills_root.mkdir(parents=True, exist_ok=True)
                    if target.is_symlink():
                        if target.resolve() == skill_dir.resolve():
                            print(f"exists  {target} -> {skill_dir}")
                            continue
                        target.unlink()
                    elif target.exists():
                        raise Exception(
                            f"{target} exists and is not a symlink — remove it manually"
                        )
                    target.symlink_to(skill_dir)
                    print(f"linked  {target} -> {skill_dir}")

                # Install CLI entry points from skill's bin/ dir to ~/.local/bin
                skill_bin = skill_dir / "bin"
                if skill_bin.is_dir():
                    bin_files = [
                        f for f in sorted(skill_bin.iterdir())
                        if not f.name.startswith(".") and not f.is_dir()
                    ]
                    if bin_files:
                        symlink_paths(bin_files, local_bin)

            except Exception as e:
                print(f"error   {e}", file=sys.stderr)
                errors += 1

        elif operation == Operation.UNINSTALL:
            # Remove from all possible locations (handles scope changes between installs)
            for skills_root in (home_claude_skills, home_opencode_skills,
                                project_claude_skills, project_opencode_skills):
                target = skills_root / skill.name
                if target.is_symlink():
                    target.unlink()
                    print(f"removed {target}")

            # Remove bin/ entry points from ~/.local/bin (best-effort: cache may be gone)
            try:
                skill_dir = _resolve_external_skill_dir(skill, cache_root)
                skill_bin = skill_dir / "bin"
                if skill_bin.is_dir():
                    for f in sorted(skill_bin.iterdir()):
                        if not f.name.startswith(".") and not f.is_dir():
                            local_target = local_bin / f.name
                            if local_target.is_symlink() and local_target.resolve() == f.resolve():
                                local_target.unlink()
                                print(f"removed {local_target}")
            except Exception:
                pass  # Cache may be absent; skip bin cleanup

    return 1 if errors else 0


def _is_git_url(s: str) -> bool:
    """Return True if s looks like a git remote URL rather than a local path."""
    return s.startswith(("https://", "http://", "git@", "ssh://"))


def _pull_plugin_repos(plugins: list["PluginEntry"], cache_root: Path) -> None:
    """Pull (update) each unique URL-sourced plugin repo once.

    Skips repos not yet cloned — they'll be cloned fresh when setup_plugins runs.
    Local-path plugins are not touched (caller owns those).
    """
    seen: set[str] = set()
    for plugin in plugins:
        url = plugin.repo
        if not _is_git_url(url) or url in seen:
            continue
        seen.add(url)
        cache_dir = _skill_repo_cache_dir(url, cache_root)
        if cache_dir.is_dir() and (cache_dir / ".git").exists():
            pull_repo(cache_dir, url)


def setup_plugins(
    operation: Operation,
    plugins: list["PluginEntry"],
    project_root: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> int:
    """Run each plugin's install.py with the requested operation and scope.

    Plugin contract:
      <plugin.repo>/install.py {install,uninstall} --scope {global|project}
                               [--project <project_root>]   # only when scope=project

    plugin.repo may be either:
      - A local path (absolute or starting with ~, already expanduser'd at parse time)
      - A git remote URL (https://, http://, git@, ssh://) — cloned/cached under
        cache_root using the same mechanism as external skills.

    Plugins whose `disable` flag is True are skipped on INSTALL but will
    still be uninstalled when `disable` is newly set (tamago calls UNINSTALL
    before re-running INSTALL, so the plugin gets cleaned up first).

    Missing install.py is a warning, not a hard failure — the rest of the
    plugins still run.  Returns 0 on success, 1 if any plugin call fails.
    """
    errors = 0
    for plugin in plugins:
        if plugin.disable and operation == Operation.INSTALL:
            print(f"skip    plugin {plugin.name} (disabled)")
            continue

        # Resolve repo dir — clone from remote or use local path directly.
        try:
            if _is_git_url(plugin.repo):
                cache_dir = _skill_repo_cache_dir(plugin.repo, cache_root)
                repo = _clone_or_reuse_skill_repo(plugin.repo, cache_dir)
            else:
                repo = Path(plugin.repo)
        except Exception as e:
            print(f"error   plugin {plugin.name}: {e}", file=sys.stderr)
            errors += 1
            continue

        install_script = repo / "install.py"
        if not install_script.exists():
            print(
                f"warn    plugin {plugin.name}: install.py not found at {install_script}",
                file=sys.stderr,
            )
            continue

        cmd = [sys.executable, str(install_script), operation.value,
               "--scope", plugin.scope]
        if plugin.scope == "project":
            cmd += ["--project", str(project_root)]

        print(f"plugin  {plugin.name}: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(
                f"error   plugin {plugin.name} exited with code {result.returncode}",
                file=sys.stderr,
            )
            errors += 1

    return 1 if errors else 0


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


def setup_local_bin(operation: Operation, source_root: Path) -> None:
    """Symlink the tamago CLI wrapper into ~/.local/bin and ensure it is on PATH."""
    local_bin = Path("~/.local/bin").expanduser()
    tamago_bin = source_root / "bin" / "tamago"
    target = local_bin / "tamago"

    if operation == Operation.INSTALL:
        if not tamago_bin.exists():
            raise Exception(f"tamago bin script not found: {tamago_bin}")

        local_bin.mkdir(parents=True, exist_ok=True)

        if target.is_symlink():
            if target.resolve() == tamago_bin.resolve():
                print(f"exists  {target} -> {tamago_bin}")
            else:
                target.unlink()
                target.symlink_to(tamago_bin)
                print(f"linked  {target} -> {tamago_bin}")
        elif target.exists():
            raise Exception(f"skip    {target} (already exists and is not a symlink — remove it manually)")
        else:
            target.symlink_to(tamago_bin)
            print(f"linked  {target} -> {tamago_bin}")

        # Add ~/.local/bin to PATH in ~/.zshrc if not already present
        zshrc = Path("~/.zshrc").expanduser()
        path_line = 'export PATH="$HOME/.local/bin:$PATH"'
        path_marker = ".local/bin"
        lines = zshrc.read_text().splitlines() if zshrc.exists() else []
        if any(path_marker in l for l in lines):
            print(f"exists  {path_marker} in ~/.zshrc PATH")
        else:
            with open(zshrc, "a") as f:
                f.write(f"\n# tamago CLI\n{path_line}\n")
            print(f"added   {path_marker} to ~/.zshrc PATH  (run: source ~/.zshrc)")

    elif operation == Operation.UNINSTALL:
        if target.is_symlink():
            target.unlink()
            print(f"removed {target}")
        else:
            print(f"skip    {target} not found")


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


def _setup_bootstrap_skill(operation: Operation, source_root: Path) -> None:
    """Symlink the hatch skill globally so it is available before any project install.

    Only 'hatch' is installed here — it is the only built-in skill needed during
    the bootstrap phase (creating a first agent profile).  All other built-in
    skills are installed on the first `tamago install` run via tamago.conf.
    """
    hatch_dir = source_root / "skills" / "hatch"
    if not hatch_dir.is_dir():
        return
    home_claude_skills  = Path("~/.claude/skills").expanduser()
    home_opencode_skills = Path("~/.opencode/skills").expanduser()
    if operation == Operation.INSTALL:
        symlink_paths([hatch_dir], home_claude_skills)
        symlink_paths([hatch_dir], home_opencode_skills)
    elif operation == Operation.UNINSTALL:
        unlink_paths([hatch_dir], home_claude_skills)
        unlink_paths([hatch_dir], home_opencode_skills)


def setup_global(operation: Operation, source_root: Path) -> int:
    """Home-level install: patch ~/.claude/settings.json, patch ~/.opencode/opencode.json,
    install tamago's own git hooks, bootstrap the hatch skill, and clean up any stale
    shell env entries from older tamago versions.
    Each step runs independently — one failure does not block the others."""
    errors: list[str] = []

    for step in (
        lambda: patch_global_settings(operation, source_root),         # Claude: patch/merge
        lambda: patch_opencode_global_settings(operation, source_root), # OpenCode: patch/merge
        lambda: setup_shell_env(operation, source_root),
        lambda: setup_git_hooks(operation, source_root),
        lambda: setup_local_bin(operation, source_root),
        lambda: _setup_bootstrap_skill(operation, source_root),        # hatch skill globally
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
    disabled_skills: "set[str] | None" = None,
    disabled_agents: "set[str] | None" = None,
    install_globally: bool = False,
    agent_name: str | None = None,
) -> int:
    """Project-level install: symlink skills, agents, settings, memory into project_root."""
    try:
        setup_gitignore(operation, project_root)
        setup_skills(operation, source_root, project_root, profile_root, disabled_skills, install_globally=install_globally)
        setup_agents(operation, source_root, project_root, profile_root, tts_enabled=tts_enabled, disabled_agents=disabled_agents, install_globally=install_globally)
        setup_settings(operation, source_root, project_root, profile_root, install_globally=install_globally, agent_name=agent_name)

        machine_env = project_root / ".tamago" / MACHINE_ENV_NAME
        global_machine_env = Path.home() / ".tamago" / MACHINE_ENV_NAME
        if operation == Operation.INSTALL:
            write_machine_env(machine_env, profile_root, agent_name, memory_sync, tts_enabled)
            if install_globally:
                # Global install: also write ~/.tamago/machine.env so memory-sync.sh
                # finds the profile when Claude runs outside this project directory.
                write_machine_env(global_machine_env, profile_root, agent_name, memory_sync, tts_enabled)
        elif operation == Operation.UNINSTALL:
            write_machine_env(machine_env, None, "")
            if install_globally:
                write_machine_env(global_machine_env, None, "")

        return 0
    except Exception as e:
        print(e, file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Project registry helpers (Slice F)
# ---------------------------------------------------------------------------
# Registry lives at ~/.tamago/known-projects.json
# Format: {"projects": [{"path": "/abs/path"}, ...]}
# The "projects" list is the stable key; each entry is an object so future
# slices can add metadata (last_install, tamago_version, etc.) without a
# format bump.

def read_known_projects(registry_path: Path = KNOWN_PROJECTS_FILE) -> list[dict]:
    """Return the raw project entries from the registry.

    Each entry is a dict with at least a "path" key.  Returns [] if the file
    is missing, empty, or unparseable — never raises.
    """
    if not registry_path.exists():
        return []
    try:
        raw = json.loads(registry_path.read_text())
        entries = raw.get("projects", [])
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and "path" in e]
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def _write_registry(registry_path: Path, entries: list[dict]) -> None:
    """Overwrite the registry with a new entries list."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({"projects": entries}, indent=2) + "\n")


def add_project_to_registry(
    project_root: Path,
    registry_path: Path = KNOWN_PROJECTS_FILE,
) -> None:
    """Add project_root to the registry (idempotent — dedup by resolved path)."""
    resolved = str(project_root.resolve())
    entries = read_known_projects(registry_path)
    if any(e.get("path") == resolved for e in entries):
        return
    entries.append({"path": resolved})
    _write_registry(registry_path, entries)
    print(f"registered {resolved}")


def remove_project_from_registry(
    project_root: Path,
    registry_path: Path = KNOWN_PROJECTS_FILE,
) -> None:
    """Remove project_root from the registry (no-op if not present)."""
    if not registry_path.exists():
        return
    resolved = str(project_root.resolve())
    entries = read_known_projects(registry_path)
    new_entries = [e for e in entries if e.get("path") != resolved]
    if len(new_entries) == len(entries):
        return  # wasn't registered
    _write_registry(registry_path, new_entries)
    print(f"unregistered {resolved}")


# ---------------------------------------------------------------------------
# Prune (Slice F)
# ---------------------------------------------------------------------------

def prune(
    cache_root: Path = DEFAULT_CACHE_ROOT,
    registry_path: Path = KNOWN_PROJECTS_FILE,
    dry_run: bool = False,
) -> int:
    """Remove skill cache dirs not referenced by any known tamago project.

    Reads ~/.tamago/known-projects.json, loads each project's machine.toml,
    collects all referenced cache dirs, then deletes any entries in cache_root
    not in that set.  Stale registry entries (project dir gone) are cleaned up
    automatically unless dry_run is True.

    Returns 0 on success, 1 if any removal fails.
    """
    entries = read_known_projects(registry_path)

    referenced: set[str] = set()
    stale: list[dict] = []
    # Pre-Slice-E projects: registered dir exists but has no machine.toml.
    # We cannot know which cache dirs they reference, so we must not delete anything.
    ambiguous: list[Path] = []

    for entry in entries:
        project_path = Path(entry["path"])
        machine_toml = project_path / ".tamago" / MACHINE_TOML_NAME
        data = load_machine_toml(machine_toml)
        if data is None:
            if not project_path.is_dir():
                print(f"stale   {project_path} (dir gone — will clean registry)")
                stale.append(entry)
            else:
                # Project exists but no machine.toml — pre-Slice-E install.
                # Its cache usage is unknown; treat conservatively.
                print(
                    f"warn    {project_path} has no machine.toml "
                    f"(run 'tamago install --config tamago.conf' to upgrade) — "
                    f"skipping prune to avoid deleting caches it may still use"
                )
                ambiguous.append(project_path)
            continue
        for cache_path_str in data.skill_cache.values():
            referenced.add(str(Path(cache_path_str).resolve()))

    if ambiguous:
        print(
            f"info    prune aborted — {len(ambiguous)} project(s) with unknown cache usage. "
            f"Re-install them with 'tamago install --config tamago.conf' then retry."
        )
        return 1

    if not cache_root.is_dir():
        print(f"info    cache root absent — nothing to prune: {cache_root}")
    else:
        errors = 0
        pruned = 0
        for entry_path in sorted(cache_root.iterdir()):
            if not entry_path.is_dir():
                continue
            if str(entry_path.resolve()) not in referenced:
                if dry_run:
                    print(f"would   remove {entry_path}")
                else:
                    try:
                        shutil.rmtree(entry_path)
                        print(f"pruned  {entry_path}")
                        pruned += 1
                    except OSError as exc:
                        print(f"error   could not remove {entry_path}: {exc}", file=sys.stderr)
                        errors += 1
        if not dry_run and pruned == 0 and errors == 0:
            print("info    nothing to prune")
        if errors:
            return 1

    # Clean stale entries from registry (skip in dry-run)
    if stale and not dry_run:
        active = [e for e in entries if e not in stale]
        _write_registry(registry_path, active)
        print(f"info    removed {len(stale)} stale registry entr{'y' if len(stale) == 1 else 'ies'}")

    return 0


# ---------------------------------------------------------------------------
# TOML-conf driven install (Slice C / D / E)
# ---------------------------------------------------------------------------

def _write_install_machine_toml(
    path: Path,
    conf: "TamagoConf",
    profile_root: "Path | None",
    cache_root: Path,
) -> None:
    """Build and write machine.toml from the resolved install state.

    Called by install_from_conf AFTER setup() succeeds but BEFORE
    setup_external_skills() so the profile path is always recorded
    even if a URL-skill clone fails.
    """
    profiles: dict[str, str] = {}
    if profile_root is not None and conf.profiles:
        p = conf.profiles[0]
        key = p.name or (_repo_name_from_url(p.repo) if p.repo else "default")
        profiles[key] = str(profile_root.resolve())

    skill_cache: dict[str, str] = {}
    for skill in conf.skills:
        if skill.source not in ("tamago", "profile"):
            cache_dir = _skill_repo_cache_dir(skill.source, cache_root)
            # Use resolve() so prune()'s set membership check (which also resolves)
            # matches even when DEFAULT_CACHE_ROOT contains symlink components.
            skill_cache[skill.name] = str(cache_dir.resolve())

    write_machine_toml(path, MachineToml(profiles=profiles, skill_cache=skill_cache))


# ---------------------------------------------------------------------------
# Two-tier conflict detection  (Step 3)
# ---------------------------------------------------------------------------

def _check_conf_conflicts(
    global_conf: "TamagoConf | None",
    project_conf: "TamagoConf",
) -> list[str]:
    """Return human-readable conflict messages for items declared in both tiers.

    The two tiers are additive: the same agent/skill/plugin name must not appear
    in both ~/.tamago/tamago.conf and <project>/.tamago/tamago.conf at the same
    time.  Disabled items (disable=True) are excluded — they are being removed,
    not installed, so overlap is harmless.

    Returns an empty list when there are no conflicts (including when global_conf
    is None, i.e. no global conf exists).
    """
    if global_conf is None:
        return []

    conflicts: list[str] = []

    global_agents  = {a.name for a in global_conf.agents  if not a.disable}
    global_skills  = {s.name for s in global_conf.skills  if not s.disable}
    global_plugins = {p.name for p in global_conf.plugins if not p.disable}

    for a in project_conf.agents:
        if not a.disable and a.name in global_agents:
            conflicts.append(
                f"agent '{a.name}' is declared in both global and project tamago.conf"
            )
    for s in project_conf.skills:
        if not s.disable and s.name in global_skills:
            conflicts.append(
                f"skill '{s.name}' is declared in both global and project tamago.conf"
            )
    for p in project_conf.plugins:
        if not p.disable and p.name in global_plugins:
            conflicts.append(
                f"plugin '{p.name}' is declared in both global and project tamago.conf"
            )

    return conflicts


def install_from_conf(
    conf_path: Path,
    operation: Operation,
    source_root: Path,
    project_root: Path,
    pull_cached_skills: bool = False,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    registry_path: Path = KNOWN_PROJECTS_FILE,
    global_conf_path: Path = GLOBAL_CONF_PATH,
) -> int:
    """Install/uninstall all agents and skills declared in a tamago.conf TOML file.

    This is the v2 entry point — all deployment decisions (profile, tts, memory_sync,
    scope) live in tamago.conf rather than CLI flags.

    MVP limitations (Slice C/D/E):
    - Only the first [[profiles]] entry is used; a second one raises an error.
    - tts is derived from the first profile-sourced agent entry; if multiple profile
      agents have conflicting tts values, the first one wins for all of them.
    - [[skills]] with scope=global is the default for built-in/profile skills.
      URL-sourced skills support both scope=global and scope=project.

    pull_cached_skills: when True, pull already-cached skill repos before installing
      (used by 'tamago update'; False for plain 'tamago install').

    machine.toml (Slice E):
    - On INSTALL: written after setup() succeeds, recording resolved profile path and
      skill cache dirs.  Written BEFORE setup_external_skills so the profile is always
      recorded even if a URL-skill clone fails.
    - On UNINSTALL: read first to recover the reliably-resolved profile path from the
      previous install; falls back to conf.profiles re-resolution for pre-Slice-E
      installs.  Deleted after uninstall completes.
    """
    conf = load_tamago_conf(conf_path)
    if conf is None:
        print(f"error   could not parse tamago.conf: {conf_path}", file=sys.stderr)
        return 1

    # Guard: spec says "only one [[profiles]] entry for now".
    if len(conf.profiles) > 1:
        print(
            f"error   tamago.conf has {len(conf.profiles)} [[profiles]] entries — "
            f"only one is supported in this version",
            file=sys.stderr,
        )
        return 1

    # Two-tier conflict check: same name in both global and project conf is an error.
    global_conf = load_tamago_conf(global_conf_path)
    conflicts = _check_conf_conflicts(global_conf, conf)
    if conflicts:
        for msg in conflicts:
            print(f"error   {msg}", file=sys.stderr)
        print(
            "error   resolve conflicts before installing: remove duplicate entries from "
            "either the global (~/.tamago/tamago.conf) or project (.tamago/tamago.conf) conf",
            file=sys.stderr,
        )
        return 1

    machine_toml_path = project_root / ".tamago" / MACHINE_TOML_NAME

    # For UNINSTALL: try machine.toml first — it has the path we resolved at install
    # time, which is stable even if the user later edits tamago.conf.
    profile_root: Path | None = None
    if operation == Operation.UNINSTALL:
        machine_data = load_machine_toml(machine_toml_path)
        if machine_data is not None:
            for path_str in machine_data.profiles.values():
                candidate = Path(path_str)
                if candidate.is_dir():
                    profile_root = candidate
                    print(f"info    using profile from machine.toml: {profile_root}")
                    break

    # Resolve profile: project conf takes precedence; global conf is the fallback.
    # This lets a single [[profiles]] entry in ~/.tamago/tamago.conf serve all projects
    # without repeating it in every project conf.
    if profile_root is None:
        if conf.profiles:
            profile_entry = conf.profiles[0]
        elif global_conf is not None and global_conf.profiles:
            profile_entry = global_conf.profiles[0]
            label = profile_entry.name or profile_entry.repo or "default"
            print(f"info    using profile from global tamago.conf: {label}")
        else:
            profile_entry = None

        if profile_entry is not None:
            try:
                profile_root = resolve_profile_root(
                    source_root,
                    profile_dir=None,
                    profile_repo=profile_entry.repo,
                    profile_name=profile_entry.name,
                )
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1

    # Derive tts_enabled: first [[agents]] entry with source="profile" wins.
    # tamago-built-in agents don't generate TTS sections regardless.
    tts_enabled = True
    for a in conf.agents:
        if a.source == "profile":
            tts_enabled = a.tts
            break

    # Pull latest repos on install (uninstall works from whatever is on disk).
    if operation == Operation.INSTALL:
        pull_repo(source_root, "tamago")
        if profile_root is not None:
            pull_repo(profile_root, "profile")
        if pull_cached_skills:
            _pull_skill_repos(conf.skills, cache_root)
            _pull_plugin_repos(conf.plugins, cache_root)

    disabled_skills: set[str] = {s.name for s in conf.skills if s.disable}
    disabled_agents: set[str] = {a.name for a in conf.agents if a.disable}

    # Two-tier model: everything in the project conf is always project-scoped.
    # scope="global" no longer has any effect in the project conf — warn and ignore.
    for _a in conf.agents:
        if not _a.disable and _a.scope == "global":
            print(
                f"warning scope=\"global\" on agent '{_a.name}' in project tamago.conf is "
                f"ignored — declare it in ~/.tamago/tamago.conf instead",
                file=sys.stderr,
            )
    for _s in conf.skills:
        if not _s.disable and _s.source in ("tamago", "profile") and _s.scope == "global":
            print(
                f"warning scope=\"global\" on skill '{_s.name}' in project tamago.conf is "
                f"ignored — declare it in ~/.tamago/tamago.conf instead",
                file=sys.stderr,
            )

    # Agent name for default-agent pointer: first non-disabled agent in conf.
    agent_name: str | None = next(
        (a.name for a in conf.agents if not a.disable),
        None,
    )

    rc = setup(
        operation,
        source_root,
        project_root,
        profile_root=profile_root,
        memory_sync=conf.memory_sync,
        tts_enabled=tts_enabled,
        disabled_skills=disabled_skills,
        disabled_agents=disabled_agents,
        install_globally=False,
        agent_name=agent_name,
    )
    if rc != 0:
        return rc

    # Write machine.toml AFTER setup() succeeds so we only record a working install.
    # Do it BEFORE setup_external_skills so the profile path is captured even if a
    # URL-skill clone fails.
    if operation == Operation.INSTALL:
        _write_install_machine_toml(machine_toml_path, conf, profile_root, cache_root)
        # Register in the global project registry so 'tamago prune' and future
        # 'tamago update --all' can find this project.
        add_project_to_registry(project_root, registry_path)

    rc = setup_external_skills(operation, conf.skills, project_root, cache_root)

    plugin_rc = setup_plugins(operation, conf.plugins, project_root, cache_root)
    if plugin_rc != 0:
        rc = plugin_rc

    # Clean up machine.toml and registry entry after uninstall.
    # setup(UNINSTALL) already removes tamago.conf and machine.env;
    # machine.toml and the registry entry are our responsibility here.
    if operation == Operation.UNINSTALL:
        if machine_toml_path.exists():
            machine_toml_path.unlink()
            print(f"removed {machine_toml_path}")
        remove_project_from_registry(project_root, registry_path)

    # Run health check after everything is written — machine.toml must exist before
    # the check runs, otherwise the first install always warns about it being missing.
    if operation == Operation.INSTALL:
        run_health_check(source_root, project_root)

    return rc


# ---------------------------------------------------------------------------
# Global conf install  (two-tier refactor Step 1)
# ---------------------------------------------------------------------------

def _write_global_conf_template(path: Path) -> None:
    """Write a commented-out starter template to ~/.tamago/tamago.conf if it does not exist.

    The template is a no-op until the user uncomments entries — existing installs are
    therefore unaffected on upgrade.
    """
    template = (
        "# ~/.tamago/tamago.conf — global tamago configuration\n"
        "# Items declared here are installed globally (→ ~/.claude/ and ~/.opencode/).\n"
        "# Run 'tamago install-global' after editing this file.\n"
        "#\n"
        "# ── Skills ──────────────────────────────────────────────────────────────────\n"
        "# Uncomment to install built-in tamago skills globally.\n"
        "# [[skills]]\n"
        "# name   = \"hatch\"\n"
        "# source = \"tamago\"\n"
        "#\n"
        "# ── Plugins ─────────────────────────────────────────────────────────────────\n"
        "# [[plugins]]\n"
        "# name  = \"nagori\"\n"
        "# repo  = \"https://github.com/HammerMei/nagori\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template)
    print(f"created {path}  (starter template — edit and re-run 'tamago install-global')")


def install_global_from_conf(
    conf_path: Path,
    operation: Operation,
    source_root: Path,
    pull_cached_skills: bool = False,
    cache_root: Path = DEFAULT_CACHE_ROOT,
) -> int:
    """Install/uninstall agents, skills, and plugins declared in ~/.tamago/tamago.conf.

    Tamago infra (hooks, git hooks, local bin, global settings patch) runs
    unconditionally on every call regardless of conf contents.

    If conf_path does not exist on INSTALL, a commented-out starter template is
    written there so the user has a ready-made starting point — but no conf-driven
    items are installed (the template is entirely commented out).

    All agents/skills/plugins declared in the global conf are installed globally
    (→ ~/.claude/ and ~/.opencode/); the ``scope`` field is ignored here because
    tier determines scope in the two-tier architecture.
    """
    errors: list[str] = []

    # ── Tamago infra (always unconditional) ───────────────────────────────────
    for step in (
        lambda: patch_global_settings(operation, source_root),
        lambda: patch_opencode_global_settings(operation, source_root),
        lambda: setup_shell_env(operation, source_root),
        lambda: setup_git_hooks(operation, source_root),
        lambda: setup_local_bin(operation, source_root),
        lambda: _setup_bootstrap_skill(operation, source_root),
    ):
        try:
            step()
        except Exception as e:
            print(e, file=sys.stderr)
            errors.append(str(e))

    # ── Generate starter template if no global conf exists ────────────────────
    if operation == Operation.INSTALL and not conf_path.exists():
        try:
            _write_global_conf_template(conf_path)
        except Exception as e:
            print(e, file=sys.stderr)
            errors.append(str(e))
        # Template is all comments — nothing to install from it yet.
        return 1 if errors else 0

    # ── Load global conf ──────────────────────────────────────────────────────
    conf = load_tamago_conf(conf_path)
    if conf is None:
        if conf_path.exists():
            # File exists but is unparseable
            msg = f"error   could not parse global tamago.conf: {conf_path}"
            print(msg, file=sys.stderr)
            errors.append(msg)
        # Missing file: infra already ran, nothing more to do.
        return 1 if errors else 0

    # Guard: only one [[profiles]] entry supported
    if len(conf.profiles) > 1:
        print(
            f"error   global tamago.conf has {len(conf.profiles)} [[profiles]] entries — "
            f"only one is supported in this version",
            file=sys.stderr,
        )
        return 1

    # ── Resolve profile ───────────────────────────────────────────────────────
    profile_root: Path | None = None

    if operation == Operation.UNINSTALL:
        machine_data = load_machine_toml(GLOBAL_MACHINE_TOML_PATH)
        if machine_data is not None:
            for path_str in machine_data.profiles.values():
                candidate = Path(path_str)
                if candidate.is_dir():
                    profile_root = candidate
                    print(f"info    using profile from global machine.toml: {profile_root}")
                    break

    if profile_root is None and conf.profiles:
        p = conf.profiles[0]
        try:
            profile_root = resolve_profile_root(
                source_root,
                profile_dir=None,
                profile_repo=p.repo,
                profile_name=p.name,
            )
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1

    # ── Pull repos (on 'tamago update' only) ──────────────────────────────────
    if operation == Operation.INSTALL and pull_cached_skills:
        _pull_skill_repos(conf.skills, cache_root)
        _pull_plugin_repos(conf.plugins, cache_root)

    # ── Derive install params ─────────────────────────────────────────────────
    # In the global conf, ALL items are global-scoped (tier determines scope).
    disabled_agents: set[str] = {a.name for a in conf.agents if a.disable}
    disabled_skills: set[str] = {s.name for s in conf.skills if s.disable}
    tts_enabled = True
    for a in conf.agents:
        if a.source == "profile":
            tts_enabled = a.tts
            break
    agent_name: str | None = next(
        (a.name for a in conf.agents if not a.disable), None
    )

    # CONVENTIONAL_ROOT (~/.tamago) is used as stand-in project_root.
    # With install_globally=True, setup_agents/setup_skills route to ~/.claude/ so
    # they never write under ~/.tamago/.claude/ in practice.
    global_root = CONVENTIONAL_ROOT

    # ── Agents ────────────────────────────────────────────────────────────────
    try:
        setup_agents(
            operation, source_root, global_root, profile_root,
            tts_enabled=tts_enabled,
            disabled_agents=disabled_agents,
            install_globally=True,
        )
    except Exception as e:
        print(e, file=sys.stderr)
        errors.append(str(e))

    # ── Skills (built-in + profile) ───────────────────────────────────────────
    try:
        setup_skills(
            operation, source_root, global_root, profile_root,
            disabled_skills=disabled_skills,
            install_globally=True,
        )
    except Exception as e:
        print(e, file=sys.stderr)
        errors.append(str(e))

    # ── Agent settings (default-agent pointer + profile overrides) ────────────
    try:
        setup_settings(
            operation, source_root, global_root, profile_root,
            install_globally=True,
            agent_name=agent_name,
        )
    except Exception as e:
        print(e, file=sys.stderr)
        errors.append(str(e))

    # ── Machine.env ───────────────────────────────────────────────────────────
    global_machine_env = CONVENTIONAL_ROOT / MACHINE_ENV_NAME
    if operation == Operation.INSTALL:
        try:
            write_machine_env(
                global_machine_env, profile_root, agent_name,
                conf.memory_sync, tts_enabled,
            )
        except Exception as e:
            print(e, file=sys.stderr)
            errors.append(str(e))
    elif operation == Operation.UNINSTALL:
        try:
            write_machine_env(global_machine_env, None, "")
        except Exception as e:
            print(e, file=sys.stderr)
            errors.append(str(e))

    # ── External skills (URL-sourced) ─────────────────────────────────────────
    ext_rc = setup_external_skills(operation, conf.skills, global_root, cache_root)
    if ext_rc != 0:
        errors.append(f"external skills failed (rc={ext_rc})")

    # ── Plugins ───────────────────────────────────────────────────────────────
    plugin_rc = setup_plugins(operation, conf.plugins, global_root, cache_root)
    if plugin_rc != 0:
        errors.append(f"plugins failed (rc={plugin_rc})")

    # ── Write/remove global machine.toml ──────────────────────────────────────
    if operation == Operation.INSTALL:
        _write_install_machine_toml(GLOBAL_MACHINE_TOML_PATH, conf, profile_root, cache_root)
    elif operation == Operation.UNINSTALL:
        if GLOBAL_MACHINE_TOML_PATH.exists():
            GLOBAL_MACHINE_TOML_PATH.unlink()
            print(f"removed {GLOBAL_MACHINE_TOML_PATH}")

    # ── Health check ──────────────────────────────────────────────────────────
    if operation == Operation.INSTALL:
        run_health_check(source_root, global_root)

    return 1 if errors else 0


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

    Priority order:
      1. profile_dir  — use path directly (must already exist)
      2. profile_repo — clone (or pull) the repo; profile_name provides the local
                        dir name when both are set (e.g. name="hammer.mei" →
                        clone into <source_root>/hammer.mei-profile/)
      3. profile_name — shorthand: <source_root>/<name>-profile (must already exist)

    When both name and repo appear in tamago.conf, repo wins so that a fresh
    machine can auto-clone without the directory having to exist first.
    """
    if profile_dir:
        p = Path(profile_dir).expanduser().resolve()
        if not p.is_dir():
            raise ValueError(f"Profile directory not found: {p}")
        return p

    if profile_repo:
        # Expand ~ for local paths (git clone via subprocess does not use a shell,
        # so "~/foo" would be passed literally and fail).
        profile_repo_expanded = os.path.expanduser(profile_repo)
        # Use explicit profile_name for the local dir when provided so the user
        # can control the directory name independently of the URL.
        if profile_name:
            name_slug = profile_name if profile_name.endswith("-profile") else f"{profile_name}-profile"
            repo_name = name_slug
        else:
            repo_name = _repo_name_from_url(profile_repo_expanded)
        if not repo_name.endswith("-profile"):
            raise ValueError(
                f"Profile repo name must end with '-profile', got: '{repo_name}'\n"
                f"  Rename your repo to follow the convention (e.g. '{repo_name}-profile'),\n"
                f"  or use --profile-dir to skip the naming check."
            )
        clone_dir = source_root / repo_name
        if clone_dir.is_dir() and (clone_dir / ".git").exists():
            print(f"exists  {clone_dir}  (pulling latest)")
            pull_repo(clone_dir, "profile")
        elif clone_dir.exists():
            raise ValueError(
                f"Directory exists but is not a git repo: {clone_dir}\n"
                f"  Remove it first or use --profile-dir to point elsewhere."
            )
        else:
            print(f"cloning {profile_repo_expanded}")
            print(f"     → {clone_dir}")
            result = subprocess.run(
                ["git", "clone", profile_repo_expanded, str(clone_dir)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise ValueError(
                    f"git clone failed:\n{result.stderr.strip()}"
                )
        return clone_dir

    if profile_name:
        # Check CONVENTIONAL_ROOT (~/.tamago/) first so that profile repos cloned
        # there are found regardless of where the tamago source lives.
        conventional = CONVENTIONAL_ROOT / f"{profile_name}-profile"
        if conventional.is_dir():
            return conventional
        # Fall back to a profile living inside the tamago source tree itself.
        p = source_root / f"{profile_name}-profile"
        if not p.is_dir():
            raise ValueError(
                f"Profile directory not found: {p}\n"
                f"  Also checked: {conventional}\n"
                f"  Hint: clone your profile repo to ~/.tamago/{profile_name}-profile,\n"
                f"  or use --profile-repo to clone automatically."
            )
        return p

    return None


def resolve_source_root(source_override: str | None) -> Path:
    """Resolve the tamago source directory.

    Priority (highest → lowest):
      1. --source CLI argument (source_override)
      2. ASSISTANT_SETUP_REPO environment variable (set manually by developer)
      3. ~/.tamago  (CONVENTIONAL_ROOT — the standard install location)

    Tamago never auto-injects ASSISTANT_SETUP_REPO.  If you need to point tamago
    at a development checkout, set it explicitly in your shell profile.
    """
    source = source_override or os.environ.get("ASSISTANT_SETUP_REPO")
    resolved_root = Path(source).expanduser() if source else CONVENTIONAL_ROOT

    if not resolved_root.is_dir():
        raise ValueError(
            f"Source directory not found: {resolved_root}\n"
            f"  Hint: clone tamago to ~/.tamago, or set ASSISTANT_SETUP_REPO=/path/to/tamago"
        )
    return resolved_root


def build_parser() -> argparse.ArgumentParser:
    source_parser = argparse.ArgumentParser(add_help=False)
    source_parser.add_argument(
        "-s", "--source",
        dest="source",
        help="path to tamago repo (overrides ASSISTANT_SETUP_REPO and default)",
    )

    conf_parser = argparse.ArgumentParser(add_help=False)
    conf_parser.add_argument(
        "--config",
        dest="config",
        default=None,
        metavar="PATH",
        help=(
            "path to a TOML tamago.conf; when given, overrides auto-detection. "
            "Profile, tts, and memory_sync all come from the config file. "
            "Defaults to auto-detecting .tamago/tamago.conf in the current directory."
        ),
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
        help="install agents/skills from .tamago/tamago.conf into the current project",
        parents=[source_parser, conf_parser],
    )
    subparsers.add_parser(
        Operation.UNINSTALL.value,
        help="remove symlinks and files created by install",
        parents=[source_parser, conf_parser],
    )
    subparsers.add_parser(
        "update",
        help="update installed agents and skills — alias for install",
        parents=[source_parser, conf_parser],
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="run health check for the current (or specified) project",
        parents=[source_parser],
    )
    doctor_parser.add_argument(
        "--project",
        dest="project",
        default=None,
        metavar="PATH",
        help="project directory to check (default: current working directory)",
    )

    prune_parser = subparsers.add_parser(
        "prune",
        help="remove skill cache dirs no longer used by any tamago-installed project",
    )
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="show what would be removed without actually deleting anything",
    )
    prune_parser.add_argument(
        "--cache-root",
        dest="cache_root",
        default=None,
        metavar="PATH",
        help=f"skill repo cache directory (default: {DEFAULT_CACHE_ROOT})",
    )
    prune_parser.add_argument(
        "--registry",
        dest="registry",
        default=None,
        metavar="PATH",
        help=f"project registry file (default: {KNOWN_PROJECTS_FILE})",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Track whether this was 'tamago update' before aliasing — determines whether
    # cached skill repos should be pulled (update pulls; install does not).
    was_update = args.command == "update"

    # 'update' is an alias for 'install' — same behaviour, future-proof name.
    if args.command == "update":
        args.command = "install"

    # prune doesn't need source_root — handle it before resolution.
    if args.command == "prune":
        cache_root = Path(args.cache_root).expanduser() if args.cache_root else DEFAULT_CACHE_ROOT
        registry_path = Path(args.registry).expanduser() if args.registry else KNOWN_PROJECTS_FILE
        return prune(cache_root=cache_root, registry_path=registry_path, dry_run=args.dry_run)

    project_root = Path.cwd()

    try:
        source_override = (
            args.source if getattr(args, "source", None) else args.global_source
        )
        source_root = resolve_source_root(source_override)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    if args.command in ("install", "install-global"):
        pull_repo(source_root, "tamago")

    if args.command == "doctor":
        project = Path(getattr(args, "project", None) or Path.cwd()).expanduser().resolve()
        run_health_check(source_root, project)
        return 0

    if args.command == "install-global":
        return install_global_from_conf(
            GLOBAL_CONF_PATH, Operation.INSTALL, source_root,
            pull_cached_skills=was_update,
        )

    if args.command == "uninstall-global":
        return install_global_from_conf(
            GLOBAL_CONF_PATH, Operation.UNINSTALL, source_root,
        )

    # install/uninstall route through install_from_conf via the TOML tamago.conf.
    # Auto-detect .tamago/tamago.conf in CWD; --config overrides the detected path.
    config_path = getattr(args, "config", None)
    if config_path is None and args.command in (
        Operation.INSTALL.value,
        Operation.UNINSTALL.value,
    ):
        auto = project_root / ".tamago" / PROJECT_CONF_NAME
        if auto.exists():
            config_path = str(auto)
            print(f"info    auto-detected config: {auto}")
    if config_path is not None and args.command in (
        Operation.INSTALL.value,
        Operation.UNINSTALL.value,
    ):
        return install_from_conf(
            Path(config_path).expanduser(),
            Operation.INSTALL if args.command == Operation.INSTALL.value else Operation.UNINSTALL,
            source_root,
            project_root,
            pull_cached_skills=was_update,
        )

    # No tamago.conf found — tell the user how to set one up.
    if args.command in (Operation.INSTALL.value, Operation.UNINSTALL.value):
        conf_path = project_root / ".tamago" / PROJECT_CONF_NAME
        print(
            f"error   no tamago.conf found at {conf_path}\n"
            f"        Create one from the template: cp $(tamago --source)/templates/tamago.conf.example .tamago/tamago.conf\n"
            f"        Then edit it and run: tamago install",
            file=sys.stderr,
        )
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
