#!/bin/bash
# health-check.sh — Hammer Mei deployment health checker
#
# Usage:
#   bash scripts/health-check.sh [--fix] [--json] [--project <dir>]
#
# Flags:
#   --fix            Auto-fix simple issues (brew install, mkdir)
#   --json           Machine-readable JSON output
#   --project <dir>  Project dir to check symlinks in (default: ~/workspace/assistant)
#
# Exit code: 0 = pass (or warn only), 1 = any failures

# ─── Path Resolution (mirrors memory-sync.sh) ─────────────────────────────────

REPO="${ASSISTANT_SETUP_REPO:-$HOME/.tamago}"

# ─── Args (parsed first so PROJECT_DIR is known for config resolution) ────────

FIX=false
JSON=false
PROJECT_DIR="$(pwd)"

while [[ $# -gt 0 ]]; do
  case $1 in
    --fix)     FIX=true ;;
    --json)    JSON=true ;;
    --project) PROJECT_DIR="$2"; shift ;;
    -h|--help)
      echo "Usage: $0 [--fix] [--json] [--project <dir>]"
      echo "  --fix            Auto-fix simple issues (brew install, mkdir)"
      echo "  --json           Machine-readable JSON output"
      echo "  --project <dir>  Project dir to check (default: cwd)"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

# ─── Profile resolution (mirrors memory-sync.sh 2-step discovery) ────────────
# 1. Project-scoped machine.env — written by `tamago install`, shell-sourceable
PROJECT_ENV="$PROJECT_DIR/.tamago/machine.env"
if [ -f "$PROJECT_ENV" ]; then
  # shellcheck source=/dev/null
  . "$PROJECT_ENV"
fi

# 2. Global fallback: ~/.tamago/machine.env (globally-installed agents)
if [ -z "${PROFILE_REPO:-}" ]; then
  GLOBAL_ENV="$HOME/.tamago/machine.env"
  if [ -f "$GLOBAL_ENV" ]; then
    # shellcheck source=/dev/null
    . "$GLOBAL_ENV"
  fi
fi

PROFILE_REPO="${PROFILE_REPO:-$REPO}"
PROJECT_CONF="$PROJECT_DIR/.tamago/tamago.conf"

# Whether a separate profile repo is configured
if [ "$PROFILE_REPO" != "$REPO" ]; then
  HAS_PROFILE=true
else
  HAS_PROFILE=false
fi

# ─── Agent name and scope — read directly from tamago.conf ───────────────────

AGENT_NAME=""
AGENT_SCOPE="project"
AGENT_SOURCE="tamago"   # "profile" for persona agents, "tamago" for built-ins
if [ -f "$PROJECT_CONF" ]; then
  _agent_out=$(python3 - "$PROJECT_CONF" <<'PY' 2>/dev/null
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
conf_path = sys.argv[1]
with open(conf_path, 'rb') as f:
    conf = tomllib.load(f)
agents = conf.get('agents', [])
if agents:
    # TODO: multi-agent configs (len(agents) > 1) only check the first agent's scope.
    a = agents[0]
    print(a.get('name', ''), a.get('scope', 'project'), a.get('source', 'tamago'))
# else: print nothing — no agents configured; AGENT_NAME stays empty
PY
  ) || _agent_out=""
  if [ -n "$_agent_out" ]; then
    read -r AGENT_NAME AGENT_SCOPE AGENT_SOURCE <<< "$_agent_out"
  fi
fi

# ─── Colors (only when printing to terminal, not in JSON mode) ────────────────

if [ "$JSON" = false ] && [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; NC=''
fi

# ─── State ────────────────────────────────────────────────────────────────────

PASS=0; WARN=0; FAIL=0
JSON_RESULTS=()

# ─── Helpers ──────────────────────────────────────────────────────────────────

_json_escape() {
  # Escape \, ", then replace control chars (newline/tab/CR) in a portable way.
  # awk handles multi-line input natively, avoiding GNU-only sed extensions.
  printf '%s' "$1" \
    | sed 's/\\/\\\\/g; s/"/\\"/g' \
    | awk '{
        gsub(/\t/, "\\t")
        gsub(/\r/, "\\r")
        if (NR > 1) printf "\\n"
        printf "%s", $0
      }'
}

_record() {
  local status="$1" name="$2" msg="$3"
  JSON_RESULTS+=("{\"name\":\"$(_json_escape "$name")\",\"status\":\"$status\",\"msg\":\"$(_json_escape "$msg")\"}")
}

pass() {
  local name="$1" msg="${2:-}"
  ((PASS++))
  _record pass "$name" "$msg"
  [ "$JSON" = false ] && printf "${GREEN}✅${NC}  %-44s ${GREEN}%s${NC}\n" "$name" "$msg"
}

warn() {
  local name="$1" msg="${2:-}" fix="${3:-}"
  ((WARN++))
  _record warn "$name" "$msg"
  if [ "$JSON" = false ]; then
    printf "${YELLOW}⚠️ ${NC}  %-44s ${YELLOW}%s${NC}\n" "$name" "$msg"
    [ -n "$fix" ] && printf "     ${BLUE}↳ fix:${NC} %s\n" "$fix"
  fi
  if [ "$FIX" = true ] && [ -n "$fix" ]; then
    printf "\n   ${BOLD}🔧 Running: %s${NC}\n" "$fix"
    # NOTE: fix strings must be hardcoded literals — never include user-derived
    # values (e.g. PROFILE_REPO paths) to avoid eval-based shell injection.
    eval "$fix" && printf "   ${GREEN}✓ done${NC}\n\n" || printf "   ${RED}✗ failed${NC}\n\n"
  fi
}

fail() {
  local name="$1" msg="${2:-}" fix="${3:-}"
  ((FAIL++))
  _record fail "$name" "$msg"
  if [ "$JSON" = false ]; then
    printf "${RED}❌${NC}  %-44s ${RED}%s${NC}\n" "$name" "$msg"
    [ -n "$fix" ] && printf "     ${BLUE}↳ fix:${NC} %s\n" "$fix"
  fi
  if [ "$FIX" = true ] && [ -n "$fix" ]; then
    printf "\n   ${BOLD}🔧 Running: %s${NC}\n" "$fix"
    eval "$fix" && printf "   ${GREEN}✓ done${NC}\n\n" || printf "   ${RED}✗ failed${NC}\n\n"
  fi
}

section() { [ "$JSON" = false ] && printf "\n${BOLD}── %s${NC}\n" "$1"; }

check_symlink() {
  local link="$1" name="$2"
  if [ -L "$link" ]; then
    local target; target=$(readlink "$link")
    if [ -e "$link" ]; then
      pass "$name" "→ $(basename "$target")"
    else
      fail "$name" "broken symlink → $target"
    fi
  elif [ -e "$link" ]; then
    warn "$name" "exists but not a symlink (manual file?)"
  else
    fail "$name" "missing"
  fi
}

# For patch+merge files (global Claude/OpenCode settings) — existence of the
# tamago manifest is the signal that tamago manages the file.
check_patched() {
  local path="$1" manifest="$2" name="$3"
  if [ -f "$manifest" ]; then
    pass "$name" "patched ✓"
  elif [ -f "$path" ]; then
    warn "$name" "exists but no tamago manifest — run: tamago install-global"
  else
    fail "$name" "missing — run: tamago install-global"
  fi
}

# For merged/generated agent files (not symlinks — written by setup.py)
check_generated() {
  local file="$1" name="$2"
  if [ -f "$file" ]; then
    if grep -q "<!-- TAMAGO GENERATED" "$file" 2>/dev/null; then
      pass "$name" "generated ✓"
    else
      warn "$name" "exists but missing TAMAGO GENERATED header (manual file?)"
    fi
  else
    fail "$name" "missing — run: setup.py install --profile <profile>"
  fi
}

# Use timeout if available; otherwise run directly (may hang on network issues)
run_timed() {
  if command -v timeout &>/dev/null; then
    timeout 5 "$@"
  else
    "$@"
  fi
}

# ─── Header ───────────────────────────────────────────────────────────────────

if [ "$JSON" = false ]; then
  printf "\n${BOLD}🥚 tamago — Deployment Health Check${NC}\n"
  printf "tamago:  %s\n" "$REPO"
  printf "profile: %s\n" "$PROFILE_REPO"
  printf "project: %s\n" "$PROJECT_DIR"
fi

# ─── 1. Required Tools ────────────────────────────────────────────────────────

section "1. Required Tools"

command -v git &>/dev/null \
  && pass "git" "$(git --version)" \
  || fail "git" "not found"

command -v python3 &>/dev/null \
  && pass "python3" "$(python3 --version 2>&1)" \
  || fail "python3" "not found"

command -v jq &>/dev/null \
  && pass "jq" "$(jq --version)" \
  || warn "jq" "not found — statusline will be broken" "brew install jq"

command -v say &>/dev/null \
  && pass "say (TTS)" "/usr/bin/say" \
  || fail "say (TTS)" "not found — TTS unavailable (macOS only)"

command -v timeout &>/dev/null \
  && pass "timeout" "$(command -v timeout)" \
  || fail "timeout" "not found — memory-sync --pull always fails" "brew install coreutils"

command -v keepassxc-cli &>/dev/null \
  && pass "keepassxc-cli" "$(command -v keepassxc-cli)" \
  || warn "keepassxc-cli" "not found — secrets vault unavailable" "brew install keepassxc"

# ─── 2. Repos & Paths ─────────────────────────────────────────────────────────

section "2. Repos & Paths"

[ -d "$REPO" ] \
  && pass "tamago repo" "$REPO" \
  || fail "tamago repo" "not found: $REPO"

[ -d "$PROFILE_REPO" ] \
  && pass "profile repo" "$PROFILE_REPO" \
  || fail "profile repo" "not found: $PROFILE_REPO"

if [ "$HAS_PROFILE" = true ]; then
  [ -f "$PROJECT_CONF" ] \
    && pass "tamago.conf" "$PROJECT_CONF" \
    || warn "tamago.conf" "missing — run: tamago install"
else
  pass "tamago.conf" "no profile configured — skipping"
fi

[ -d "$HOME/.kpx-keys" ] \
  && pass "~/.kpx-keys" "$HOME/.kpx-keys" \
  || warn "~/.kpx-keys" "directory missing — KeePass key files location" "mkdir -p $HOME/.kpx-keys"

if [ "$HAS_PROFILE" = false ]; then
  pass "memory dir" "no profile configured — skipping"
elif [ -n "$AGENT_NAME" ] && [ "$AGENT_SOURCE" = "profile" ]; then
  [ -d "$PROFILE_REPO/agents/memory/$AGENT_NAME" ] \
    && pass "memory dir" "$AGENT_NAME/" \
    || fail "memory dir" "not found: $PROFILE_REPO/agents/memory/$AGENT_NAME"
elif [ -n "$AGENT_NAME" ]; then
  pass "memory dir" "tamago built-in agent ($AGENT_NAME) — no memory dir"
else
  pass "memory dir" "no agent in tamago.conf — skipping"
fi

# ─── 3. Global Settings (patch+merge — not symlinks) ─────────────────────────

section "3. Global Settings"

check_patched "$HOME/.claude/settings.json"      "$HOME/.claude/.tamago-manifest.json"    "~/.claude/settings.json"
check_patched "$HOME/.opencode/opencode.json"    "$HOME/.opencode/.tamago-manifest.json"  "~/.opencode/opencode.json"

# Agent-scoped settings (patch+merge, separate from tamago layer) — global agent only
if [ "$HAS_PROFILE" = true ] && [ "$AGENT_SCOPE" = "global" ]; then
  check_patched "$HOME/.claude/settings.json"   "$HOME/.claude/.tamago-agent-manifest.json"   "~/.claude/settings.json (agent layer)"
  check_patched "$HOME/.opencode/opencode.json" "$HOME/.opencode/.tamago-agent-manifest.json" "~/.opencode/opencode.json (agent layer)"
fi

# ─── 4. Project Symlinks ──────────────────────────────────────────────────────

section "4. Project Symlinks  ($PROJECT_DIR)"

if [ -d "$PROJECT_DIR" ]; then
  # Agent settings (patch+merge — not symlinks) — project agent only
  if [ "$HAS_PROFILE" = true ] && [ "$AGENT_SCOPE" != "global" ]; then
    check_patched "$PROJECT_DIR/.claude/settings.json"   "$PROJECT_DIR/.claude/.tamago-agent-manifest.json"   ".claude/settings.json"
    check_patched "$PROJECT_DIR/.opencode/opencode.json" "$PROJECT_DIR/.opencode/.tamago-agent-manifest.json" ".opencode/opencode.json"
  fi

  # machine.env — v2 shell bridge, written by `tamago install`
  if [ -f "$PROJECT_DIR/.tamago/machine.env" ]; then
    pass ".tamago/machine.env" "present"
  else
    fail ".tamago/machine.env" "missing — run: tamago install"
  fi

  # machine.toml — install-time state (Slice E); warn only (pre-Slice-E installs lack it)
  if [ -f "$PROJECT_DIR/.tamago/machine.toml" ]; then
    pass ".tamago/machine.toml" "present"
  else
    warn ".tamago/machine.toml" "missing — re-run: tamago install  (pre-Slice-E install)"
  fi

  # Check skills — scope is determined by which tamago.conf file the skill is listed in:
  #   global conf  (~/.tamago/tamago.conf)  → ~/.claude/skills/ (always, install_global_from_conf)
  #   project conf (.tamago/tamago.conf)    → depends on skill source and agent scope:
  #       tamago built-in            → ~/.claude/skills/ (built-ins always global)
  #       profile skill + global agent → ~/.claude/skills/
  #       profile skill + project agent → <project>/.claude/skills/
  #   when a profile skill shadows a tamago built-in, profile routing rules apply
  # Only skills listed in [[skills]] (in either conf) are expected — others are silently skipped.
  # External (URL-sourced) skills are intentionally excluded from health checks; their symlinks
  # are optional user-managed additions, not required by tamago.
  if [ -d "$REPO/skills" ] || { [ "$HAS_PROFILE" = true ] && [ -d "$PROFILE_REPO/skills" ]; }; then

    # Helper: emit one skill name per line from a tamago.conf, skipping external (URL) skills.
    _read_skill_names() {
      python3 - "$1" <<'PY' 2>/dev/null
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open(sys.argv[1], 'rb') as f:
    conf = tomllib.load(f)
for s in conf.get('skills', []):
    src = s.get('source', 'tamago')
    if src in ('tamago', 'profile'):
        print(s['name'])
PY
    }

    # Read skills from global conf (~/.tamago/tamago.conf) — these are always globally installed.
    GLOBAL_CONF="$REPO/tamago.conf"
    GLOBAL_CONF_SKILLS=()
    ENABLED_SKILLS=()
    HAS_SKILLS_IN_CONF=false

    if [ -f "$GLOBAL_CONF" ]; then
      _gcs=$(_read_skill_names "$GLOBAL_CONF") || _gcs=""
      while IFS= read -r _line; do
        [ -n "$_line" ] || continue
        GLOBAL_CONF_SKILLS+=("$_line")
        ENABLED_SKILLS+=("$_line")
        HAS_SKILLS_IN_CONF=true
      done <<< "$_gcs"
    fi

    # Read skills from project conf (.tamago/tamago.conf) — routing per source + agent scope.
    if [ -f "$PROJECT_CONF" ]; then
      _pss=$(_read_skill_names "$PROJECT_CONF") || _pss=""
      while IFS= read -r _line; do
        [ -n "$_line" ] || continue
        ENABLED_SKILLS+=("$_line")
        HAS_SKILLS_IN_CONF=true
      done <<< "$_pss"
    fi

    # _is_enabled: true if the skill is listed in either conf; false if neither conf has [[skills]]
    _is_enabled() {
      [ "$HAS_SKILLS_IN_CONF" = false ] && return 1
      local _name="$1"
      for _s in "${ENABLED_SKILLS[@]+"${ENABLED_SKILLS[@]}"}"; do
        [ "$_s" = "$_name" ] && return 0
      done
      return 1
    }

    # _is_global_conf_skill: true if the skill is listed in the global conf
    _is_global_conf_skill() {
      local _name="$1"
      for _s in "${GLOBAL_CONF_SKILLS[@]+"${GLOBAL_CONF_SKILLS[@]}"}"; do
        [ "$_s" = "$_name" ] && return 0
      done
      return 1
    }

    # Collect profile skill names (to detect when a profile skill shadows a tamago built-in)
    PROFILE_SKILL_NAMES=()
    if [ "$HAS_PROFILE" = true ] && [ -d "$PROFILE_REPO/skills" ]; then
      for _d in "$PROFILE_REPO/skills"/*/; do
        [ -d "$_d" ] && PROFILE_SKILL_NAMES+=("$(basename "$_d")")
      done
    fi

    _is_profile_skill() {
      local _name="$1"
      for _s in "${PROFILE_SKILL_NAMES[@]+"${PROFILE_SKILL_NAMES[@]}"}"; do
        [ "$_s" = "$_name" ] && return 0
      done
      return 1
    }

    # _check_skill NAME IS_PROFILE_SKILL
    #   Scope routing (matches setup.py install behavior exactly):
    #     global conf skill         → always ~/.claude/skills/
    #     project conf + profile skill + project agent → <project>/.claude/skills/
    #     project conf + tamago built-in or global agent → ~/.claude/skills/
    #   Skills not listed in any conf are silently skipped.
    _check_skill() {
      local _name="$1" _is_profile="${2:-false}"
      if ! _is_enabled "$_name"; then
        return
      fi
      if _is_global_conf_skill "$_name"; then
        check_symlink "$HOME/.claude/skills/$_name" "~/.claude/skills/$_name"
      elif [ "$_is_profile" = "true" ] && [ "$AGENT_SCOPE" != "global" ]; then
        check_symlink "$PROJECT_DIR/.claude/skills/$_name" ".claude/skills/$_name"
      else
        check_symlink "$HOME/.claude/skills/$_name" "~/.claude/skills/$_name"
      fi
    }

    # Check tamago built-in skills.
    # If a profile skill shadows a tamago built-in (same name), apply profile routing.
    if [ -d "$REPO/skills" ]; then
      for _skill_dir in "$REPO/skills"/*/; do
        [ -d "$_skill_dir" ] || continue
        _sname="$(basename "$_skill_dir")"
        if _is_profile_skill "$_sname"; then
          _check_skill "$_sname" "true"   # profile shadows tamago: profile routing
        else
          _check_skill "$_sname" "false"  # pure tamago built-in: always global
        fi
      done
    fi

    # Check profile-only skills (those not already covered by the tamago loop above)
    if [ "$HAS_PROFILE" = true ] && [ -d "$PROFILE_REPO/skills" ]; then
      for _skill_dir in "$PROFILE_REPO/skills"/*/; do
        [ -d "$_skill_dir" ] || continue
        _sname="$(basename "$_skill_dir")"
        [ -d "$REPO/skills/$_sname" ] && continue  # already checked in tamago loop
        _check_skill "$_sname" "true"
      done
    fi
  fi

  if [ -n "$AGENT_NAME" ] && [ "$AGENT_SOURCE" != "profile" ]; then
    # Tamago built-in agent: files are symlinks (not generated); no memory dir.
    if [ "$AGENT_SCOPE" = "global" ]; then
      check_symlink "$HOME/.claude/agents/$AGENT_NAME.md"   "~/.claude/agents/$AGENT_NAME.md"
      check_symlink "$HOME/.opencode/agents/$AGENT_NAME.md" "~/.opencode/agents/$AGENT_NAME.md"
    else
      check_symlink "$PROJECT_DIR/.claude/agents/$AGENT_NAME.md"   ".claude/agents/$AGENT_NAME.md"
      check_symlink "$PROJECT_DIR/.opencode/agents/$AGENT_NAME.md" ".opencode/agents/$AGENT_NAME.md"
    fi
  elif [ "$HAS_PROFILE" = false ]; then
    pass "agent symlinks" "no profile configured — skipping"
  elif [ -n "$AGENT_NAME" ]; then
    # Persona agent (source=profile): files are generated; has memory dir symlink.
    if [ "$AGENT_SCOPE" = "global" ]; then
      check_generated "$HOME/.claude/agents/$AGENT_NAME.md"    "~/.claude/agents/$AGENT_NAME.md"
      check_symlink   "$HOME/.claude/agent-memory/$AGENT_NAME" "~/.claude/agent-memory/$AGENT_NAME"
      check_generated "$HOME/.opencode/agents/$AGENT_NAME.md"  "~/.opencode/agents/$AGENT_NAME.md"
      # Profile settings (e.g. agent-emojis.json) — also installed globally for global agents
      if [ -d "$PROFILE_REPO/settings/claude" ]; then
        for _json_file in "$PROFILE_REPO/settings/claude"/*.json; do
          [ -e "$_json_file" ] || continue
          _json_name=$(basename "$_json_file")
          [ "$_json_name" = "settings.json" ] && continue  # managed by patch_global_settings
          check_symlink "$HOME/.claude/$_json_name" "~/.claude/$_json_name"
        done
      fi
    else
      check_generated "$PROJECT_DIR/.claude/agents/$AGENT_NAME.md"    ".claude/agents/$AGENT_NAME.md"
      check_symlink   "$PROJECT_DIR/.claude/agent-memory/$AGENT_NAME" ".claude/agent-memory/$AGENT_NAME"
      check_generated "$PROJECT_DIR/.opencode/agents/$AGENT_NAME.md"  ".opencode/agents/$AGENT_NAME.md"
    fi
  else
    pass "agent symlinks" "no agent in tamago.conf — skipping"
  fi
else
  warn "project dir" "not found: $PROJECT_DIR — skipping symlink checks"
fi

# ─── 5. Git Remotes ───────────────────────────────────────────────────────────

section "5. Git Remotes"

if [ -d "$REPO/.git" ]; then
  remote=$(cd "$REPO" && git remote get-url origin 2>/dev/null || echo "no remote")
  if (cd "$REPO" && run_timed git ls-remote --exit-code origin HEAD &>/dev/null); then
    pass "tamago remote" "$remote"
  else
    warn "tamago remote" "unreachable — $remote"
  fi
fi

PROFILE_REMOTE_OK=false
if [ "$HAS_PROFILE" = true ] && [ -d "$PROFILE_REPO/.git" ]; then
  remote=$(cd "$PROFILE_REPO" && git remote get-url origin 2>/dev/null || echo "")
  if [ -z "$remote" ]; then
    warn "profile remote" "no remote configured — memory sync disabled"
  elif (cd "$PROFILE_REPO" && run_timed git ls-remote --exit-code origin HEAD &>/dev/null); then
    pass "profile remote" "$remote"
    PROFILE_REMOTE_OK=true
  else
    warn "profile remote" "unreachable — $remote"
  fi
fi

# ─── 6. Memory Sync ───────────────────────────────────────────────────────────

section "6. Memory Sync"

if [ "$HAS_PROFILE" = false ]; then
  pass "memory-sync --pull" "no profile configured — skipping"
elif [ "${MEMORY_SYNC:-1}" = "0" ]; then
  pass "memory-sync --pull" "disabled (MEMORY_SYNC=0 in tamago.conf) — skipping"
elif [ "$PROFILE_REMOTE_OK" = false ]; then
  pass "memory-sync --pull" "no reachable remote — skipping"
else
  SYNC_OUT=$("$REPO/scripts/memory-sync.sh" --pull 2>&1)
  SYNC_EXIT=$?
  if [ $SYNC_EXIT -ne 0 ] || echo "$SYNC_OUT" | grep -qi "warning\|failed\|error"; then
    fail "memory-sync --pull" "${SYNC_OUT:-exit $SYNC_EXIT}"
  else
    pass "memory-sync --pull" "ok"
  fi
fi

# ─── 7. Profile Extra Checks (optional) ──────────────────────────────────────

EXTRA="$PROFILE_REPO/scripts/health-check-extra.sh"
if [ -f "$EXTRA" ]; then
  section "7. Profile Extra Checks"
  bash "$EXTRA" \
    --tamago "$REPO" \
    --profile "$PROFILE_REPO" \
    --project "$PROJECT_DIR" \
    $([ "$FIX"  = true ] && echo "--fix") \
    $([ "$JSON" = true ] && echo "--json") \
    2>&1 || warn "health-check-extra.sh" "script exited with error"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

if [ "$JSON" = true ]; then
  IFS=,
  printf '{"pass":%d,"warn":%d,"fail":%d,"results":[%s]}\n' \
    "$PASS" "$WARN" "$FAIL" "${JSON_RESULTS[*]}"
  unset IFS
else
  printf "\n${BOLD}── Summary${NC}\n"
  printf "  ${GREEN}✅ pass: %d${NC}   ${YELLOW}⚠️  warn: %d${NC}   ${RED}❌ fail: %d${NC}\n\n" \
    "$PASS" "$WARN" "$FAIL"
  if [ "$FAIL" -gt 0 ]; then
    printf "${RED}Health check FAILED — fix errors above before proceeding.${NC}\n\n"
    printf "${RED}🔨💥🥚 Hammer Mei swung — the tamago broke. Fix it!${NC}\n"
    printf "${RED}       鐵鎚老妹鎚下去，蛋碎了。趕快修！${NC}\n"
    printf "${RED}       ハンマー妹が叩いた。たまごが割れた。早く直して！${NC}\n\n"
  elif [ "$WARN" -gt 0 ]; then
    printf "${YELLOW}Health check passed with warnings.${NC}"
    [ "$FIX" = false ] && printf " Run with --fix to auto-fix where possible."
    printf "\n\n"
    printf "${YELLOW}🔨🥚  Hammer Mei swung — a few cracks, but close enough.${NC}\n"
    printf "${YELLOW}       鐵鎚老妹鎚過，有點壞但還能用。${NC}\n"
    printf "${YELLOW}       ハンマー妹が叩いた。少しひびが入ったが、まだ使える。${NC}\n\n"
  else
    printf "${GREEN}All checks passed 🎉${NC}\n\n"
    printf "${GREEN}🔨🥚  Hammer Mei swung — no cracks. This tamago is certified!${NC}\n"
    printf "${GREEN}       鐵鎚老妹鎚過，沒有壞，認證通過！${NC}\n"
    printf "${GREEN}       ハンマー妹が叩いた。割れなかった。合格！${NC}\n\n"
  fi
fi

[ "$FAIL" -eq 0 ]
