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

REPO="${ASSISTANT_SETUP_REPO:-$HOME/workspace/tamago}"

if [ -z "${PROFILE_REPO:-}" ] && [ -f "$REPO/local.conf" ]; then
  PROFILE_REPO=$(grep "^PROFILE_REPO=" "$REPO/local.conf" 2>/dev/null | cut -d= -f2-)
fi
PROFILE_REPO="${PROFILE_REPO:-$REPO}"

# ─── Agent name (dynamic — read from profile settings) ───────────────────────

AGENT_SETTINGS="$PROFILE_REPO/settings/claude/settings.json"
if [ -f "$AGENT_SETTINGS" ]; then
  AGENT_NAME=$(python3 -c "import json; print(json.load(open('$AGENT_SETTINGS')).get('agent',''))" 2>/dev/null || echo "")
else
  AGENT_NAME=""
fi

# ─── Args ─────────────────────────────────────────────────────────────────────

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
      echo "  --project <dir>  Project dir to check (default: ~/workspace/assistant)"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

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
  printf "\n${BOLD}🔨 Hammer Mei — Deployment Health Check${NC}\n"
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

[ -f "$REPO/local.conf" ] \
  && pass "local.conf" "PROFILE_REPO=$(grep '^PROFILE_REPO=' "$REPO/local.conf" | cut -d= -f2-)" \
  || warn "local.conf" "missing — PROFILE_REPO may not resolve correctly"

[ -d "$HOME/.kpx-keys" ] \
  && pass "~/.kpx-keys" "$HOME/.kpx-keys" \
  || warn "~/.kpx-keys" "directory missing — KeePass key files location" "mkdir -p $HOME/.kpx-keys"

if [ -n "$AGENT_NAME" ]; then
  [ -d "$PROFILE_REPO/agents/memory/$AGENT_NAME" ] \
    && pass "memory dir" "$AGENT_NAME/" \
    || fail "memory dir" "not found: $PROFILE_REPO/agents/memory/$AGENT_NAME"
else
  warn "memory dir" "no agent name in profile settings — skipping"
fi

# ─── 3. Global Symlinks ───────────────────────────────────────────────────────

section "3. Global Symlinks"

check_symlink "$HOME/.claude/settings.json"  "~/.claude/settings.json"

# ─── 4. Project Symlinks ──────────────────────────────────────────────────────

section "4. Project Symlinks  ($PROJECT_DIR)"

if [ -d "$PROJECT_DIR" ]; then
  check_symlink "$PROJECT_DIR/.claude/settings.json"         ".claude/settings.json"
  check_symlink "$PROJECT_DIR/.claude/skills/text-to-speech" ".claude/skills/text-to-speech"
  check_symlink "$PROJECT_DIR/.claude/skills/daily-briefing" ".claude/skills/daily-briefing"
  check_symlink "$PROJECT_DIR/.claude/skills/restart-cli"    ".claude/skills/restart-cli"
  check_symlink "$PROJECT_DIR/.opencode/opencode.json"       ".opencode/opencode.json"

  if [ -n "$AGENT_NAME" ]; then
    check_symlink "$PROJECT_DIR/.claude/agents/$AGENT_NAME.md"    ".claude/agents/$AGENT_NAME.md"
    check_symlink "$PROJECT_DIR/.claude/agent-memory/$AGENT_NAME" ".claude/agent-memory/$AGENT_NAME"
    check_symlink "$PROJECT_DIR/.opencode/agents/$AGENT_NAME.md"  ".opencode/agents/$AGENT_NAME.md"
  else
    warn "agent symlinks" "no agent name in profile — skipping agent-specific symlink checks"
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

if [ -d "$PROFILE_REPO/.git" ]; then
  remote=$(cd "$PROFILE_REPO" && git remote get-url origin 2>/dev/null || echo "no remote")
  if (cd "$PROFILE_REPO" && run_timed git ls-remote --exit-code origin HEAD &>/dev/null); then
    pass "profile remote" "$remote"
  else
    warn "profile remote" "unreachable — $remote"
  fi
fi

# ─── 6. Memory Sync ───────────────────────────────────────────────────────────

section "6. Memory Sync"

SYNC_OUT=$("$REPO/scripts/memory-sync.sh" --pull 2>&1)
SYNC_EXIT=$?
if [ $SYNC_EXIT -ne 0 ] || echo "$SYNC_OUT" | grep -qi "warning\|failed\|error"; then
  fail "memory-sync --pull" "${SYNC_OUT:-exit $SYNC_EXIT}"
else
  pass "memory-sync --pull" "ok"
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
  elif [ "$WARN" -gt 0 ]; then
    printf "${YELLOW}Health check passed with warnings.${NC}"
    [ "$FIX" = false ] && printf " Run with --fix to auto-fix where possible."
    printf "\n\n"
  else
    printf "${GREEN}All checks passed 🎉${NC}\n\n"
  fi
fi

[ "$FAIL" -eq 0 ]
