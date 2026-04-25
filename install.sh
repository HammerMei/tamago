#!/bin/bash
# tamago — one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/HammerMei/tamago/main/install.sh | bash
#
# What it does:
#   1. Clones (or updates) tamago to ~/.tamago
#   2. Runs setup.py install-global  (global settings + tamago CLI in ~/.local/bin)
#
# After install, restart your shell (or: source ~/.zshrc), then:
#   tamago install --profile-repo <your-profile-repo-url>

set -e

TAMAGO_DIR="${ASSISTANT_SETUP_REPO:-$HOME/.tamago}"
REPO_URL="https://github.com/HammerMei/tamago.git"

echo "🥚 tamago installer"
echo ""

# ── 1. Clone or pull ─────────────────────────────────────────────────────────
if [ -d "$TAMAGO_DIR/.git" ]; then
    echo "▸ tamago already installed at $TAMAGO_DIR — pulling latest..."
    git -C "$TAMAGO_DIR" pull --rebase --quiet
    echo "  ✅ up to date"
else
    echo "▸ Cloning tamago to $TAMAGO_DIR ..."
    git clone --quiet "$REPO_URL" "$TAMAGO_DIR"
    echo "  ✅ cloned"
fi

echo ""

# ── 2. Global setup (settings + CLI symlink + PATH) ──────────────────────────
echo "▸ Running install-global..."
python3 "$TAMAGO_DIR/setup.py" install-global

echo ""
echo "✅ tamago installed!"
echo ""
echo "Next steps:"
echo "  1. Reload your shell:  source ~/.zshrc"
echo "  2. Open Claude Code in your project directory"
echo "  3. Ask Claude: 'Help me hatch a new agent'"
echo "     Claude will guide you through creating your agent profile,"
echo "     then run \`tamago install\` automatically."
echo ""
echo "  Or hatch directly from the CLI:"
echo "     python3 ~/.tamago/skills/hatch/hatch.py \\"
echo "       --name your.agent --display-name 'Your Agent' \\"
echo "       --description 'What your agent does' \\"
echo "       --profile-dir ~/.tamago/your.agent-profile \\"
echo "       --install"
echo ""
echo "  Health check after install:  tamago doctor"
