#!/usr/bin/env bash
# install_launchd.sh — Generate and install the launchd plist for the Obsidian Bridge.
# Run from the project root: bash install_launchd.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USERNAME="$(whoami)"
PLIST_SRC="$SCRIPT_DIR/com.user.obsidian-bridge.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.obsidian-bridge.plist"

# Use venv python if it exists, otherwise fall back to system python3
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ -f "$VENV_PYTHON" ]; then
    PYTHON3="$VENV_PYTHON"
    echo "Using venv python: $PYTHON3"
else
    PYTHON3="$(which python3)"
    echo "Warning: no .venv found. Using system python3: $PYTHON3"
    echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

# Substitute placeholders
sed \
    -e "s|REPLACE_WITH_ABSOLUTE_PATH|$SCRIPT_DIR|g" \
    -e "s|REPLACE_WITH_USERNAME|$USERNAME|g" \
    -e "s|/usr/bin/python3|$PYTHON3|g" \
    "$PLIST_SRC" > "$PLIST_DEST"

echo "Installed plist to $PLIST_DEST"

# Unload if already running
launchctl unload "$PLIST_DEST" 2>/dev/null || true

# Load
launchctl load "$PLIST_DEST"
echo "Daemon loaded. Logs: ~/Library/Logs/obsidian-bridge.log"
