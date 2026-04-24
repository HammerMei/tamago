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

# ─── Profile resolution (mirrors memory-sync.sh 3-step discovery) ────────────
# 1. Project-scoped machine.env — v2 format, written by `tamago install`
PROJECT_ENV="$PROJECT_DIR/.tamago/machine.env"
if [ -f "$PROJECT_ENV" ]; then
  # shellcheck source=/dev/null
  . "$PROJECT_ENV"
fi

# 2. Legacy fallback: grep-parse tamago.conf (KEY=VALUE, pre-v2 format)
if [ -z "${PROFILE_REPO:-}" ]; then
  PROJECT_CONF="$PROJECT_DIR/.tamago/tamago.conf"
  if [ -f "$PROJECT_CONF" ]; then
    [ -z "${PROFILE_REPO:-}" ] && \
      PROFILE_REPO=$(grep "^PROFILE_REPO=" "$PROJECT_CONF" 2>/dev/null | cut -d= -f2-)
    MEMORY_SYNC=$(grep "^MEMORY_SYNC=" "$PROJECT_CONF" 2>/dev/null | cut -d= -f2-)
  fi
fi

# 3. Global fallback: ~/.tamago/machine.env (globally-installed agents)
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

# ─── Agent name (dynamic — read from profile settings) ───────────────────────

AGENT_SETTINGS="$PROFILE_REPO/settings/claude/settings.json"
if [ -f "$AGENT_SETTINGS" ]; then
  AGENT_NAME=$(python3 -c "import json; print(json.load(open('$AGENT_SETTINGS')).get('agent',''))" 2>/dev/null || echo "")
else
  AGENT_NAME=""
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

_json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

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
elif [ -n "$AGENT_NAME" ]; then
  [ -d "$PROFILE_REPO/agents/memory/$AGENT_NAME" ] \
    && pass "memory dir" "$AGENT_NAME/" \
    || fail "memory dir" "not found: $PROFILE_REPO/agents/memory/$AGENT_NAME"
else
  warn "memory dir" "no agent name in profile settings — skipping"
fi

# ─── 3. Global Settings (patch+merge — not symlinks) ─────────────────────────

section "3. Global Settings"

check_patched "$HOME/.claude/settings.json"      "$HOME/.claude/.tamago-manifest.json"    "~/.claude/settings.json"
check_patched "$HOME/.opencode/opencode.json"    "$HOME/.opencode/.tamago-manifest.json"  "~/.opencode/opencode.json"

# ─── 4. Project Symlinks ──────────────────────────────────────────────────────

section "4. Project Symlinks  ($PROJECT_DIR)"

if [ -d "$PROJECT_DIR" ]; then
  check_symlink "$PROJECT_DIR/.claude/settings.json"         ".claude/settings.json"
  check_symlink "$PROJECT_DIR/.claude/skills/text-to-speech" ".claude/skills/text-to-speech"
  check_symlink "$PROJECT_DIR/.opencode/opencode.json"       ".opencode/opencode.json"

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

  # Check profile-specific skills (if profile has a skills/ dir)
  if [ "$HAS_PROFILE" = true ] && [ -d "$PROFILE_REPO/skills" ]; then
    for skill_dir in "$PROFILE_REPO/skills"/*/; do
      skill_name=$(basename "$skill_dir")
      check_symlink "$PROJECT_DIR/.claude/skills/$skill_name" ".claude/skills/$skill_name"
    done
  fi

  if [ "$HAS_PROFILE" = false ]; then
    pass "agent symlinks" "no profile configured — skipping"
  elif [ -n "$AGENT_NAME" ]; then
    check_generated "$PROJECT_DIR/.claude/agents/$AGENT_NAME.md"    ".claude/agents/$AGENT_NAME.md"
    check_symlink   "$PROJECT_DIR/.claude/agent-memory/$AGENT_NAME" ".claude/agent-memory/$AGENT_NAME"
    check_generated "$PROJECT_DIR/.opencode/agents/$AGENT_NAME.md"  ".opencode/agents/$AGENT_NAME.md"
  else
    warn "agent symlinks" "no agent name in profile settings — skipping"
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
