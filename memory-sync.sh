#!/bin/bash
# memory-sync.sh — Standalone agent memory sync for Claude Code and OpenCode
#
# ─── CLAUDE CODE — configure in .claude/settings.json ─────────────────────────
#
#   {
#     "hooks": {
#       "SessionStart": [{
#         "hooks": [{"type": "command", "command": "/abs/path/to/memory-sync.sh --init claude-code"}]
#       }],
#       "UserPromptSubmit": [{
#         "hooks": [{"type": "command", "command": "/abs/path/to/memory-sync.sh --pull"}]
#       }],
#       "Stop": [{
#         "hooks": [{"type": "command", "command": "/abs/path/to/memory-sync.sh --push"}]
#       }]
#     }
#   }
#
# Replace /abs/path/to/memory-sync.sh with the absolute path to this script.
# Hooks run with CWD = project dir, so use absolute paths here.
#
# ─── OPENCODE — configure via the memory-bootstrap plugin ─────────────────────
#
# OpenCode uses a TypeScript plugin instead of shell hooks.  The plugin:
#   - Calls --init opencode once per session (first message)
#   - Calls --pull before every user message
#   - Calls --push before the next user message (push-before-pull pattern)
#   - Injects MEMORY.md content into the system prompt automatically
#
# To use it:
#   1. Copy memory-bootstrap.ts from this repo's settings/opencode/plugins/
#      into your project's .opencode/plugins/ directory.
#   2. Edit the MEMORY_REPO and AGENT_NAME constants at the top of that file
#      to match your setup (same values as in this script's config section).
#   3. Point memory-bootstrap.ts to this script:
#        const SYNC_SCRIPT = "/abs/path/to/memory-sync.sh";
#   4. Restart OpenCode — plugins are auto-discovered from .opencode/plugins/.
#
# ──────────────────────────────────────────────────────────────────────────────
#
# Usage: memory-sync.sh --init [claude-code|opencode] | --pull | --push
#
#   --init [harness]  Run once per session: bootstraps env dir + visited log.
#                     Pass "claude-code" or "opencode" to record the harness.
#                     Auto-detects from env vars when omitted.
#   --pull            Pull latest memory from remote   (before each user message)
#   --push            Commit + push memory changes     (after each agent reply)
#
# To disable sync temporarily (e.g. offline): set MEMORY_SYNC=0 in your shell
# or export it in the hook command / plugin environment.
#
# Memory repo structure expected under MEMORY_REPO:
#
#   $MEMORY_REPO/
#   └── $MEMORY_PATH/
#       └── $AGENT_NAME/
#           ├── MEMORY.md           ← index file (agent reads this)
#           ├── <topic>.md          ← topic memory files
#           └── env-<hostname>/
#               ├── MEMORY.md       ← machine-local memory index
#               └── visited.md      ← session log (auto-managed by --init)

set -euo pipefail

# ─── CONFIGURATION — edit these to match your setup ──────────────────────────
MEMORY_REPO="$HOME/my-agent-memory"   # Absolute path to your memory git repo
AGENT_NAME="my-agent"                 # Agent name — becomes the memory subdirectory
MEMORY_PATH="agents/memory"           # Subdir inside MEMORY_REPO where memory lives
# ─────────────────────────────────────────────────────────────────────────────

# Bail early if sync is disabled
if [ "${MEMORY_SYNC:-1}" = "0" ]; then
    exit 0
fi

case "$1" in
  # ── --init ──────────────────────────────────────────────────────────────────
  # Called once per session.
  # Creates the per-machine env dir if needed, then appends to visited.md
  # whenever the harness or model changes.  Commits + pushes so the next
  # --pull sees a clean working tree.
  --init)
    HOSTNAME=$(hostname)
    ENV_DIR="$MEMORY_REPO/$MEMORY_PATH/$AGENT_NAME/env-$HOSTNAME"

    # Bootstrap env dir on first run on this machine
    if [ ! -d "$ENV_DIR" ]; then
      mkdir -p "$ENV_DIR"
      printf "# Environment Memory — %s\n\nFirst seen: %s\n" "$HOSTNAME" "$(date +%Y-%m-%d)" \
        > "$ENV_DIR/MEMORY.md"
    fi

    # Harness — use explicit argument if provided, otherwise best-effort detect
    if [ -n "${2:-}" ]; then
      HARNESS="$2"
    elif [ "${CLAUDECODE:-0}" = "1" ]; then
      HARNESS="claude-code"
    elif [ "${OPENCODE:-0}" = "1" ] || echo "$PATH" | grep -q "/.opencode/bin"; then
      HARNESS="opencode"
    else
      HARNESS="unknown"
    fi

    # Best-effort model detection via env var; falls back to "unknown"
    MODEL="${CLAUDE_MODEL:-unknown}"

    # Append to visited.md only when harness+model combo is new
    VISITED="$ENV_DIR/visited.md"
    LAST=$(tail -1 "$VISITED" 2>/dev/null || echo "")
    TODAY=$(date +%Y-%m-%d)
    if ! echo "$LAST" | grep -q "^- $TODAY | $HARNESS | $MODEL"; then
      if [ ! -f "$VISITED" ]; then
        printf "# Visited Log — %s\n\nAppend-only. Format: - {date} | {harness} | {model}\n\n" \
          "$HOSTNAME" > "$VISITED"
      fi
      printf -- "- %s | %s | %s\n" "$TODAY" "$HARNESS" "$MODEL" >> "$VISITED"

      # Commit + push so --pull sees a clean working tree
      cd "$MEMORY_REPO" && \
        git add "$MEMORY_PATH" && \
        git commit -m "auto: session init footprint" --quiet && \
        (git pull --rebase --quiet && git push --quiet) 2>/dev/null || true
    fi
    ;;

  # ── --pull ──────────────────────────────────────────────────────────────────
  # Called before every user message.  Fetches latest memory so multi-machine
  # setups (e.g. laptop + desktop) stay in sync.
  --pull)
    git -C "$MEMORY_REPO" remote get-url origin &>/dev/null || exit 0
    if ! (cd "$MEMORY_REPO" && timeout 5 git pull --rebase --quiet) 2>/dev/null; then
      echo "Memory sync warning: git pull failed — run: cd $MEMORY_REPO && git pull --rebase"
    fi
    ;;

  # ── --push ──────────────────────────────────────────────────────────────────
  # Called after each agent reply (Claude Code: Stop hook; OpenCode: start of
  # next turn via push-before-pull pattern in memory-bootstrap.ts).
  # Commits any memory changes the agent made and pushes to remote.
  --push)
    cd "$MEMORY_REPO" || exit 0

    # Nothing changed — skip
    git diff --quiet "$MEMORY_PATH" 2>/dev/null && \
      git diff --cached --quiet "$MEMORY_PATH" 2>/dev/null && \
      git ls-files --others --exclude-standard "$MEMORY_PATH" | grep -q . 2>/dev/null \
      || { git diff --quiet "$MEMORY_PATH" && exit 0; }

    git add "$MEMORY_PATH" && \
      git commit -m "auto: sync memory on turn end" --quiet || exit 0

    git remote get-url origin &>/dev/null || exit 0

    if ! (git pull --rebase --quiet && git push --quiet) 2>/dev/null; then
      printf '{"systemMessage": "⚠️ Memory sync failed — run: cd %s && git pull --rebase && git push"}\n' \
        "$MEMORY_REPO"
    fi
    ;;

  *)
    echo "Usage: $0 --init [claude-code|opencode] | --pull | --push" >&2
    exit 1
    ;;
esac
