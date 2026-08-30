#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Install the standalone redirected-folder bridge as a per-user LaunchAgent.
#
# Prerequisites:
#   ~/.copilot/pad_bridge.py
#   ~/.pad-bridge-venv/bin/python with pyserial installed
#   ~/CopilotMacropad redirected by Windows App

set -euo pipefail

LABEL="com.github.copilot.macropad-bridge"
BRIDGE="$HOME/.copilot/pad_bridge.py"
PYTHON="$HOME/.pad-bridge-venv/bin/python"
FOLDER="$HOME/CopilotMacropad"
LOG_DIR="$HOME/.copilot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$UID"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "Removed $LABEL"
    exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "Missing bridge Python: $PYTHON" >&2
    echo "Create it with: python3 -m venv ~/.pad-bridge-venv" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import serial' >/dev/null 2>&1; then
    echo "pyserial is not installed in $PYTHON" >&2
    echo "Install it with: $PYTHON -m pip install pyserial" >&2
    exit 1
fi

if [[ ! -f "$BRIDGE" ]]; then
    echo "Missing standalone bridge: $BRIDGE" >&2
    exit 1
fi

mkdir -p "$FOLDER" "$LOG_DIR" "$(dirname "$PLIST")"

cat >"$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$BRIDGE</string>
        <string>--folder</string>
        <string>$FOLDER</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/pad-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/pad-bridge.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Installed $LABEL"
echo "Log: $LOG_DIR/pad-bridge.log"
