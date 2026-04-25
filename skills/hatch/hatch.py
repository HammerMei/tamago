#!/usr/bin/env python3
"""hatch.py — Scaffold a new tamago agent profile repo.

Creates a profile directory with:
  - agents/<name>.persona.md
  - agents/memory/<name>/MEMORY.md
  - settings/claude/agent-emojis.json
  - .gitignore

Then writes .tamago/tamago.conf into the project directory and runs
`tamago install` to activate the agent immediately.

Usage:
    python3 .claude/skills/hatch/hatch.py \\
        --name xiao.mei \\
        --display-name 小小妹 \\
        --description "Junior helper agent for research and task delegation" \\
        --profile-dir ~/.tamago/xiao.mei-profile \\
        [--project-dir ~/workspace/my-project]   (default: cwd) \\
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
import datetime
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TAMAGO_ROOT = Path("~/.tamago").expanduser()
TEMPLATES_DIR = "templates"

# Chinese zodiac animals in cycle order (index 0 = Rat, starting from year 4 CE)
_CHINESE_ZODIAC = [
    "鼠 🐭", "牛 🐮", "虎 🐯", "兔 🐰", "龍 🐲", "蛇 🐍",
    "馬 🐴", "羊 🐑", "猴 🐵", "雞 🐔", "狗 🐶", "豬 🐷",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def chinese_zodiac(year: int) -> str:
    return _CHINESE_ZODIAC[(year - 4) % 12]


def western_zodiac(month: int, day: int) -> str:
    md = (month, day)
    if md >= (12, 22) or md <= (1, 19): return "魔羯座 ♑"
    if md <= (2, 18):  return "水瓶座 ♒"
    if md <= (3, 20):  return "雙魚座 ♓"
    if md <= (4, 19):  return "牡羊座 ♈"
    if md <= (5, 20):  return "金牛座 ♉"
    if md <= (6, 20):  return "雙子座 ♊"
    if md <= (7, 22):  return "巨蟹座 ♋"
    if md <= (8, 22):  return "獅子座 ♌"
    if md <= (9, 22):  return "處女座 ♍"
    if md <= (10, 22): return "天秤座 ♎"
    if md <= (11, 21): return "天蠍座 ♏"
    return "射手座 ♐"


def get_tamago_dna(tamago_root: Path) -> str:
    """Return the short git commit hash of tamago at hatch time (8 chars)."""
    result = subprocess.run(
        ["git", "-C", str(tamago_root), "rev-parse", "--short=8", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def build_birth_certificate(
    *,
    agent_name: str,
    display_name: str,
    gender: str,
    birth_dt: datetime.datetime,
    hatcher: str,
    lineage: str,
    tamago_root: Path,
    templates_dir: Path,
) -> str:
    """Render birth_certificate.md content from template."""
    tmpl_path = templates_dir / "birth_certificate.md.tmpl"
    if tmpl_path.exists():
        tmpl = tmpl_path.read_text()
    else:
        # Inline fallback if template is missing
        tmpl = (
            "---\nname: Birth Certificate\n"
            "description: Immutable origin record\ntype: reference\n---\n\n"
            "# 🥚 出生證明 — {{display_name}}\n\n"
            "| 欄位 | 內容 |\n|------|------|\n"
            "| **姓名** | `{{agent_name}}` |\n"
            "| **生日** | {{birth_datetime}} |\n"
            "| **生肖** | {{chinese_zodiac}} |\n"
            "| **星座** | {{western_zodiac}} |\n"
            "| **出生地** | `{{hostname}}` |\n"
            "| **孵化者** | {{hatcher}} |\n"
            "| **家族族譜** | {{lineage}} |\n"
            "| **tamago DNA** | `{{tamago_dna}}` |\n"
        )

    _gender_display = {"女": "女 ♀", "男": "男 ♂", "不詳": "不詳 ⚧"}.get(gender, gender or "不詳 ⚧")
    variables = {
        "agent_name": agent_name,
        "display_name": display_name,
        "gender": _gender_display,
        "birth_datetime": birth_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "chinese_zodiac": chinese_zodiac(birth_dt.year),
        "western_zodiac": western_zodiac(birth_dt.month, birth_dt.day),
        "hostname": socket.gethostname(),
        "hatcher": hatcher or "石頭蹦出來的 🪨",
        "lineage": lineage or hatcher or "石頭蹦出來的 🪨",
        "tamago_dna": get_tamago_dna(tamago_root),
    }
    return render(tmpl, variables)


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
    gender: str,
    emoji: str,
    profile_dir: Path,
    remote: str | None,
    tts: bool,
    tts_voice: str,
    skills: list[str],
    tamago_root: Path,
    hatcher: str,
    lineage: str,
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

    birth_dt = datetime.datetime.now()

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

    agent_emojis_content = json.dumps({name: emoji}, indent=2) + "\n"

    birth_cert_content = build_birth_certificate(
        agent_name=name,
        display_name=display_name,
        gender=gender,
        birth_dt=birth_dt,
        hatcher=hatcher,
        lineage=lineage,
        tamago_root=tamago_root,
        templates_dir=templates_dir,
    )

    # --- Files to create ---
    files: list[tuple[Path, str]] = [
        (profile_dir / "agents" / f"{name}.persona.md", persona_content),
        (profile_dir / "agents" / "memory" / name / "MEMORY.md", memory_content),
        (profile_dir / "agents" / "memory" / name / "birth_certificate.md", birth_cert_content),
        (profile_dir / "settings" / "claude" / "agent-emojis.json", agent_emojis_content),
        (profile_dir / ".gitignore", gitignore_content),
    ]

    if dry_run:
        print("🥚 DRY RUN — no files will be written\n")
        for path, _ in files:
            print(f"  create  {path}")
        print(f"\n  gender         : {gender or '不詳'}")
        print(f"  birth_datetime : {birth_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  chinese_zodiac : {chinese_zodiac(birth_dt.year)}")
        print(f"  western_zodiac : {western_zodiac(birth_dt.month, birth_dt.day)}")
        print(f"  hostname       : {socket.gethostname()}")
        print(f"  hatcher        : {hatcher or '石頭蹦出來的 🪨'}")
        print(f"  lineage        : {lineage or hatcher or '石頭蹦出來的 🪨'}")
        print(f"  tamago_dna     : {get_tamago_dna(tamago_root)}")
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
# tamago.conf generation
# ---------------------------------------------------------------------------

def write_tamago_conf(
    *,
    project_dir: Path,
    name: str,
    profile_dir: Path,
    remote: str | None,
    tts: bool,
    memory_sync: bool,
    dry_run: bool,
) -> Path:
    """Write (or preview) .tamago/tamago.conf in the project directory.

    The generated conf is minimal:
      - [[profiles]] pointing at the new profile dir (or remote if provided)
      - [[agents]] with source="profile", scope="project"
      - [settings] with memory_sync
    No [[skills]] section is needed — tamago built-in skills install globally by default.
    """
    import datetime as _dt
    conf_path = project_dir / ".tamago" / "tamago.conf"
    repo_line = f'repo = "{remote}"' if remote else f'repo = "{profile_dir}"'

    content = f"""\
# tamago.conf — generated by hatch on {_dt.date.today()}
# Edit as needed, then run `tamago install` to (re-)activate.

[[profiles]]
name = "{name}"
{repo_line}

[[agents]]
name   = "{name}"
source = "profile"
scope  = "project"
tts    = {"true" if tts else "false"}
memory = true

[settings]
memory_sync = {"true" if memory_sync else "false"}
"""

    if dry_run:
        print(f"\n  would write  {conf_path}")
        print("  --- tamago.conf ---")
        for line in content.splitlines():
            print(f"  {line}")
        print("  ---")
        return conf_path

    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(content)
    print(f"created  {conf_path}")
    return conf_path


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
        "--project-dir", default=None,
        help="Project directory where .tamago/tamago.conf will be written (default: cwd)",
    )
    p.add_argument(
        "--gender", default="不詳",
        choices=["女", "男", "不詳"],
        help="Agent gender for birth certificate (default: 不詳)",
    )
    p.add_argument(
        "--remote", default=None,
        help="Git remote URL to set as origin (optional)",
    )
    p.add_argument(
        "--emoji", default="🤖",
        help="Agent emoji for status line display (default: 🤖)",
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
        help="Write tamago.conf and run `tamago install` in the project dir after hatching",
    )
    p.add_argument(
        "--no-memory-sync", action="store_true",
        help="Set memory_sync=false in the generated tamago.conf (useful when no git remote yet)",
    )
    p.add_argument(
        "--hatcher", default="",
        help=(
            "Agent name of whoever is running hatch (e.g. 'hammer.mei'). "
            "Leave empty for the bootstrap case — defaults to '石頭蹦出來的 🪨'."
        ),
    )
    p.add_argument(
        "--lineage", default="",
        help=(
            "Full ancestor chain to record in birth_certificate.md "
            "(e.g. '石頭蹦出來的 🪨 → hammer.mei'). "
            "Defaults to just the --hatcher name when omitted."
        ),
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
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else Path.cwd()
    memory_sync = not args.no_memory_sync

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
            gender=args.gender,
            emoji=args.emoji,
            profile_dir=profile_dir,
            remote=args.remote,
            tts=args.tts,
            tts_voice=args.tts_voice,
            skills=skills,
            tamago_root=tamago_root,
            hatcher=args.hatcher,
            lineage=args.lineage,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    conf_path = write_tamago_conf(
        project_dir=project_dir,
        name=args.name,
        profile_dir=profile_dir,
        remote=args.remote,
        tts=args.tts,
        memory_sync=memory_sync,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0

    print(f"\n🥚 Hatched! Profile created at: {profile_dir}")
    print(f"   tamago.conf written to: {conf_path}")

    if args.install:
        print(f"\n▶ Running: tamago install")
        result = subprocess.run(["tamago", "install"], cwd=str(project_dir), check=False)
        if result.returncode != 0:
            print("warning: tamago install returned a non-zero exit code.", file=sys.stderr)
            return result.returncode
        print(f"\n✅ Agent '{args.name}' installed. Restart Claude to activate.")
    else:
        print("\nNext steps:")
        print(f"  1. cd {project_dir}")
        print(f"  2. tamago install")
        print(f"  3. Restart Claude to activate the '{args.name}' agent")

    if args.remote:
        print(f"\n📡 Don't forget to push the profile repo:")
        print(f"     cd {profile_dir} && git push -u origin main")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
