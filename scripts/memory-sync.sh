#!/bin/bash
# Hammer Mei — memory sync script
# Usage: memory-sync.sh --init [harness] | --pull | --push
#
# Invoked by Claude Code hooks (settings/claude/settings.json):
#   SessionStart     → --init claude-code  (once per session: env dir + visited.md)
#   UserPromptSubmit → --pull              (pull latest memory from remote)
#   Stop             → --push             (commit + push local memory changes)
#
# ASSISTANT_SETUP_REPO: path to tamago repo (default: ~/.tamago)
# LAOMEI_MEMORY_SYNC:   set to 0 to disable all sync (offline/emergency)
#
# Profile repo discovery (priority order):
#   1. PROFILE_REPO env var
#   2. PROFILE_REPO= line in $REPO/local.conf  (written by setup.py install --profile)
#   3. Fall back to $REPO itself (legacy single-repo mode)

REPO="${ASSISTANT_SETUP_REPO:-$HOME/.tamago}"
MEMORY_PATH="agents/memory"

# Resolve PROFILE_REPO and MEMORY_SYNC.
# Priority: env var → project-scoped .tamago/tamago.conf → global local.conf → fallback
# Hooks run with CWD = project dir, so $PWD/.tamago/tamago.conf is project-specific.
PROJECT_CONF="$PWD/.tamago/tamago.conf"
if [ -f "$PROJECT_CONF" ]; then
  [ -z "$PROFILE_REPO" ] && \
    PROFILE_REPO=$(grep "^PROFILE_REPO=" "$PROJECT_CONF" 2>/dev/null | cut -d= -f2-)
  [ -z "$MEMORY_SYNC" ] && \
    MEMORY_SYNC=$(grep "^MEMORY_SYNC=" "$PROJECT_CONF" 2>/dev/null | cut -d= -f2-)
elif [ -f "$REPO/local.conf" ]; then
  [ -z "$PROFILE_REPO" ] && \
    PROFILE_REPO=$(grep "^PROFILE_REPO=" "$REPO/local.conf" 2>/dev/null | cut -d= -f2-)
  [ -z "$MEMORY_SYNC" ] && \
    MEMORY_SYNC=$(grep "^MEMORY_SYNC=" "$REPO/local.conf" 2>/dev/null | cut -d= -f2-)
fi
MEMORY_REPO="${PROFILE_REPO:-$REPO}"

# Sync disabled — env var (LAOMEI_MEMORY_SYNC=0) or local.conf (MEMORY_SYNC=0)
if [ "${LAOMEI_MEMORY_SYNC:-1}" = "0" ] || [ "${MEMORY_SYNC:-1}" = "0" ]; then
    exit 0
fi

case "$1" in
  --init)
    HOSTNAME=$(hostname)

    # Determine agent name from profile's settings.json (falls back to hammer.mei)
    AGENT_SETTINGS="$MEMORY_REPO/settings/claude/settings.json"
    if [ -f "$AGENT_SETTINGS" ]; then
      AGENT_NAME=$(python3 -c "import json; print(json.load(open('$AGENT_SETTINGS')).get('agent','hammer.mei'))" 2>/dev/null || echo "hammer.mei")
    else
      AGENT_NAME="hammer.mei"
    fi

    ENV_DIR="$MEMORY_REPO/$MEMORY_PATH/$AGENT_NAME/env-$HOSTNAME"

    # 1. Ensure env dir exists
    if [ ! -d "$ENV_DIR" ]; then
      mkdir -p "$ENV_DIR"
      printf "# Environment Memory — %s\n\n首次見面：%s\n" "$HOSTNAME" "$(date +%Y-%m-%d)" > "$ENV_DIR/MEMORY.md"
    fi

    # 2. Harness — use explicit argument if provided, otherwise best-effort detect
    if [ -n "$2" ]; then
      HARNESS="$2"
    elif [ "${CLAUDECODE:-0}" = "1" ]; then
      HARNESS="claude-code"
    elif [ "${OPENCODE:-0}" = "1" ] || echo "$PATH" | grep -q "/.opencode/bin"; then
      HARNESS="opencode"
    else
      HARNESS="unknown"
    fi

    # 3. Detect model (best effort — check profile settings first, then tamago common)
    PROFILE_SETTINGS="$MEMORY_REPO/settings/claude/settings.json"
    COMMON_SETTINGS="$REPO/settings/claude/settings.json"
    if [ -f "$PROFILE_SETTINGS" ]; then
      MODEL=$(python3 -c "import json; print(json.load(open('$PROFILE_SETTINGS')).get('defaultModel','unknown'))" 2>/dev/null || echo "unknown")
    elif [ -f "$COMMON_SETTINGS" ]; then
      MODEL=$(python3 -c "import json; print(json.load(open('$COMMON_SETTINGS')).get('defaultModel','unknown'))" 2>/dev/null || echo "unknown")
    else
      MODEL="unknown"
    fi

    # 4. Append to visited.md only if harness+model changed
    VISITED="$ENV_DIR/visited.md"
    LAST=$(tail -1 "$VISITED" 2>/dev/null || echo "")
    if ! echo "$LAST" | grep -q "| $HARNESS | $MODEL"; then
      if [ ! -f "$VISITED" ]; then
        printf "# Visited Log — %s\n\nRecords when harness or model changes. Append-only.\nFormat: \`- {YYYY-MM-DD} | {harness} | {model}\`\n\n" "$HOSTNAME" > "$VISITED"
      fi
      printf -- "- %s | %s | %s\n" "$(date +%Y-%m-%d)" "$HARNESS" "$MODEL" >> "$VISITED"

      # 5. Commit + push immediately so UserPromptSubmit pull sees a clean working tree
      cd "$MEMORY_REPO" && \
        git add "$MEMORY_PATH" && \
        git commit -m "auto: session init footprint" --quiet && \
        (git pull --rebase --quiet && git push --quiet) 2>/dev/null || true
    fi
    ;;

  --pull)
    # Skip if no remote is configured
    git -C "$MEMORY_REPO" remote get-url origin &>/dev/null 2>&1 || exit 0
    # Pull latest memory; surface warning to Claude context on failure
    if ! (cd "$MEMORY_REPO" && timeout 5 git pull --rebase --quiet) 2>/dev/null; then
      echo "Memory sync warning: git pull failed — you may be out of sync with other 分身. Consider running 'git pull --rebase' in $MEMORY_REPO manually."
    fi
    ;;

  --push)
    cd "$MEMORY_REPO" || exit 0

    # Nothing to sync
    git diff --quiet "$MEMORY_PATH" 2>/dev/null && exit 0

    # Commit local changes
    git add "$MEMORY_PATH" && \
      git commit -m "auto: sync memory on turn end" --quiet || exit 0

    # Skip push if no remote is configured (commit is kept locally)
    git remote get-url origin &>/dev/null 2>&1 || exit 0

    # Pull --rebase then push; surface systemMessage to user on failure
    if ! (git pull --rebase --quiet && git push --quiet) 2>/dev/null; then
      printf '{"systemMessage": "⚠️ Memory sync failed: push rejected. Run: cd %s && git pull --rebase && git push"}\n' "$MEMORY_REPO"
    fi
    ;;

  *)
    echo "Usage: $0 --init [harness] | --pull | --push" >&2
    exit 1
    ;;
esac
