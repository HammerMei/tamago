#!/usr/bin/env python3
"""hatch.py — Scaffold a new tamago agent profile repo.

Creates a profile directory with:
  - agents/<name>.persona.md
  - agents/memory/<name>/MEMORY.md
  - settings/claude/settings.json
  - settings/opencode/opencode.json
  - .gitignore

Then optionally runs `setup.py install --profile <profile_dir>`.

Usage:
    python3 .claude/skills/hatch/hatch.py \\
        --name xiao.mei \\
        --display-name 小小妹 \\
        --description "Junior helper agent for research and task delegation" \\
        --profile-dir ~/workspace/xiao.mei-profile \\
        [--language "Traditional Chinese"] \\
        [--tone "Friendly, energetic, helpful"] \\
        [--user-address "老哥"] \\
        [--remote ssh://user@host/path.git] \\
        [--tts --tts-voice Meijia] \\
        [--skills text-to-speech] \\
        [--source ~/.tamago] \\
        [--install] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TAMAGO_ROOT = Path("~/.tamago").expanduser()
TEMPLATES_DIR = "templates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_tamago_root(source_override: str | None) -> Path:
    """Resolve tamago root: explicit arg > ASSISTANT_SETUP_REPO env var > default."""
    if source_override:
        return Path(source_override).expanduser().resolve()
    env = os.environ.get("ASSISTANT_SETUP_REPO")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_TAMAGO_ROOT


def render(template: str, variables: dict[str, str]) -> str:
    """Replace {{key}} placeholders in template text."""
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def build_skills_yaml(skills: list[str]) -> str:
    """Format a skills list as indented YAML list items."""
    if not skills:
        return "  []"
    return "\n".join(f"  - {s}" for s in skills)


def build_tts_section(tts_enabled: bool, voice: str) -> str:
    """Return the TTS special-skill-instructions block, or empty string."""
    if not tts_enabled:
        return ""
    return f"""

# Special Skill Instructions

## TTS
- **Voice**: "{voice}"
- **Rate**: 1.0 (default)
- Read every reply aloud using the `text-to-speech` skill with the above settings
- For long text, pipe via stdin to avoid shell limits:
  `python3 .claude/skills/text-to-speech/tts-cli.py --voice {voice} --file - <<'EOF' ... EOF`
- If TTS fails, notify the user explicitly — never silently skip"""


# ---------------------------------------------------------------------------
# Profile creation
# ---------------------------------------------------------------------------

def create_profile(
    *,
    name: str,
    display_name: str,
    description: str,
    language: str,
    tone: str,
    user_address: str,
    profile_dir: Path,
    remote: str | None,
    tts: bool,
    tts_voice: str,
    skills: list[str],
    tamago_root: Path,
    dry_run: bool,
) -> None:
    """Scaffold the full profile directory structure."""
    templates_dir = tamago_root / TEMPLATES_DIR

    # Ensure text-to-speech skill is included when TTS is enabled
    effective_skills = list(skills)
    if tts and "text-to-speech" not in effective_skills:
        effective_skills.insert(0, "text-to-speech")

    # Template variable substitution map
    tts_section = build_tts_section(tts, tts_voice)
    variables: dict[str, str] = {
        "agent_name": name,
        "display_name": display_name,
        "description": description,
        "language": language,
        "tone": tone,
        "user_address": user_address,
        "skills_yaml": build_skills_yaml(effective_skills),
        "tts_section": tts_section,
    }

    # --- Load templates ---
    persona_tmpl = templates_dir / "agent.persona.md.tmpl"
    memory_tmpl = templates_dir / "MEMORY.md.tmpl"
    gitignore_tmpl = templates_dir / "profile.gitignore"

    if not persona_tmpl.exists():
        raise FileNotFoundError(f"Persona template not found: {persona_tmpl}")

    persona_content = render(persona_tmpl.read_text(), variables)
    memory_content = (
        render(memory_tmpl.read_text(), variables)
        if memory_tmpl.exists()
        else f"# {display_name} Memory\n\n_No memories yet._\n"
    )
    gitignore_content = (
        gitignore_tmpl.read_text()
        if gitignore_tmpl.exists()
        else "secrets/\n*.kdbx\n*.key\n.DS_Store\n"
    )

    claude_settings_content = json.dumps(
        {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "agent": name,
        },
        indent=2,
    ) + "\n"

    opencode_settings_content = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "default_agent": name,
        },
        indent=2,
    ) + "\n"

    # --- Files to create ---
    files: list[tuple[Path, str]] = [
        (profile_dir / "agents" / f"{name}.persona.md", persona_content),
        (profile_dir / "agents" / "memory" / name / "MEMORY.md", memory_content),
        (profile_dir / "settings" / "claude" / "settings.json", claude_settings_content),
        (profile_dir / "settings" / "opencode" / "opencode.json", opencode_settings_content),
        (profile_dir / ".gitignore", gitignore_content),
    ]

    if dry_run:
        print("🥚 DRY RUN — no files will be written\n")
        for path, _ in files:
            print(f"  create  {path}")
        print(f"\n  git init  {profile_dir}")
        if remote:
            print(f"  remote    origin → {remote}")
        return

    # --- Create dirs and write files ---
    for path, content in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"created  {path}")

    # --- Git init + initial commit ---
    if not (profile_dir / ".git").exists():
        subprocess.run(
            ["git", "init", str(profile_dir)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(profile_dir), "add", "-A"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(profile_dir), "commit",
             "-m", f"init: hatch {name} profile\n\nAuthored-By: Hammer Mei (铁锤老妹🔨)"],
            check=True, capture_output=True,
        )
        print(f"git init {profile_dir}  (initial commit created)")
    else:
        print(f"exists   {profile_dir}/.git  (skipping git init)")

    # --- Git remote ---
    if remote:
        result = subprocess.run(
            ["git", "-C", str(profile_dir), "remote", "add", "origin", remote],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"remote   origin → {remote}")
        elif "already exists" in result.stderr:
            print(f"exists   remote origin (unchanged)")
        else:
            print(
                f"warning  could not set remote: {result.stderr.strip()}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="hatch — scaffold a new tamago agent profile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--name", required=True,
        help="Agent identifier slug, e.g. xiao.mei",
    )
    p.add_argument(
        "--display-name", required=True,
        help="Human-readable display name, e.g. 小小妹",
    )
    p.add_argument(
        "--description", required=True,
        help="One-line description of what the agent does",
    )
    p.add_argument(
        "--language", default="Traditional Chinese",
        help="Response language (default: Traditional Chinese)",
    )
    p.add_argument(
        "--tone", default="Friendly and helpful",
        help="Personality/tone description (default: Friendly and helpful)",
    )
    p.add_argument(
        "--user-address", default="老哥",
        help="How the agent addresses the user (default: 老哥)",
    )
    p.add_argument(
        "--profile-dir", required=True,
        help="Directory where the profile repo will be created",
    )
    p.add_argument(
        "--remote", default=None,
        help="Git remote URL to set as origin (optional)",
    )
    p.add_argument(
        "--tts", action="store_true",
        help="Enable TTS skill instructions in the persona",
    )
    p.add_argument(
        "--tts-voice", default="Meijia",
        help="TTS voice name (default: Meijia; used only when --tts is set)",
    )
    p.add_argument(
        "--skills", default="",
        help="Comma-separated tamago skill names to include, e.g. text-to-speech",
    )
    p.add_argument(
        "--source", default=None,
        help="Path to tamago repo (overrides ASSISTANT_SETUP_REPO env var)",
    )
    p.add_argument(
        "--install", action="store_true",
        help="Run `setup.py install --profile` in cwd after hatching",
    )
    p.add_argument(
        "--no-memory-sync", action="store_true",
        help="Disable git memory sync (writes MEMORY_SYNC=0 to local.conf); "
             "useful when no remote is configured yet",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be created without writing any files",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    tamago_root = resolve_tamago_root(args.source)
    if not tamago_root.is_dir():
        print(f"error: tamago root not found: {tamago_root}", file=sys.stderr)
        return 1

    profile_dir = Path(args.profile_dir).expanduser().resolve()

    # Guard: refuse to overwrite a non-empty existing directory
    if not args.dry_run and profile_dir.exists() and any(profile_dir.iterdir()):
        print(
            f"error: profile directory already exists and is not empty:\n  {profile_dir}",
            file=sys.stderr,
        )
        print("  Choose a different --profile-dir or remove the existing directory first.",
              file=sys.stderr)
        return 1

    skills = [s.strip() for s in args.skills.split(",") if s.strip()]

    try:
        create_profile(
            name=args.name,
            display_name=args.display_name,
            description=args.description,
            language=args.language,
            tone=args.tone,
            user_address=args.user_address,
            profile_dir=profile_dir,
            remote=args.remote,
            tts=args.tts,
            tts_voice=args.tts_voice,
            skills=skills,
            tamago_root=tamago_root,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        return 0

    print(f"\n🥚 Hatched! Profile created at: {profile_dir}")

    if args.install:
        setup_py = tamago_root / "setup.py"
        install_cmd = [sys.executable, str(setup_py), "install", "--profile", str(profile_dir)]
        if getattr(args, "no_memory_sync", False):
            install_cmd.append("--no-memory-sync")
        print(f"\n▶ Running: python3 setup.py install --profile {profile_dir}"
              + (" --no-memory-sync" if getattr(args, "no_memory_sync", False) else ""))
        result = subprocess.run(install_cmd, check=False)
        if result.returncode != 0:
            print(
                "warning: setup.py install returned a non-zero exit code.",
                file=sys.stderr,
            )
            return result.returncode
        print(f"\n✅ Agent '{args.name}' installed. Restart Claude to activate.")
    else:
        print("\nNext steps:")
        print(f"  1. cd <your-project-dir>")
        print(f"  2. python3 {tamago_root}/setup.py install --profile {profile_dir}")
        print(f"  3. Restart Claude to activate the '{args.name}' agent")

    if args.remote:
        print(f"\n📡 Don't forget to push the profile repo:")
        print(f"     cd {profile_dir} && git push -u origin main")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
