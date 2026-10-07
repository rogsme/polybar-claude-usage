#!/usr/bin/env bash
# Installs polybar-claude-usage to ~/.local/bin (or $BIN_DIR) and puts a starter config in
# ~/.config/polybar-claude-usage unless one is already there.
set -euo pipefail

cd "$(dirname "$0")"

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/polybar-claude-usage"

install -Dm755 polybar_claude_usage.py "$BIN_DIR/polybar-claude-usage"
echo "==> Installed $BIN_DIR/polybar-claude-usage"

if [[ ! -e "$CONFIG_DIR/config.ini" ]]; then
  install -Dm644 config.example.ini "$CONFIG_DIR/config.ini"
  echo "==> Wrote $CONFIG_DIR/config.ini (every setting commented out at its default)"
fi

cat <<MODULE

Add this module to your polybar config and list claude-usage in modules-left/center/right:

  [module/claude-usage]
  type = custom/script
  exec = $BIN_DIR/polybar-claude-usage
  tail = true
  click-left = $BIN_DIR/polybar-claude-usage --notify
  click-right = kill -USR1 %pid%

If polybar is already running the module, restart polybar to pick up the new version.
MODULE
