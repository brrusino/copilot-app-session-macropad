#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Install the Dev Tunnel client and Keybow bridge as per-user LaunchAgents.
# Both restart independently at login, so neither process needs an open Terminal.

set -euo pipefail

TUNNEL_LABEL="com.github.copilot.macropad-tunnel"
BRIDGE_LABEL="com.github.copilot.macropad-bridge"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
COPILOT_HOME="$HOME/.copilot"
PYTHON="$HOME/.pad-bridge-venv/bin/python"
BRIDGE="$COPILOT_HOME/pad_bridge.py"
TOKEN="$COPILOT_HOME/macropad.token"
TUNNEL_PLIST="$LAUNCH_AGENTS/$TUNNEL_LABEL.plist"
BRIDGE_PLIST="$LAUNCH_AGENTS/$BRIDGE_LABEL.plist"
DOMAIN="gui/$UID"
BRIDGE_URL="https://raw.githubusercontent.com/brrusino/copilot-app-session-macropad/brrusino-keybow-2040-macropad/scripts/pad_bridge.py"

usage() {
    echo "usage: $0 --tunnel-id ID | --uninstall | --status" >&2
}

TUNNEL_ID=""
MODE="install"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tunnel-id) TUNNEL_ID="${2:-}"; shift 2 ;;
        --uninstall) MODE="uninstall"; shift ;;
        --status) MODE="status"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

if [[ "$MODE" == "uninstall" ]]; then
    launchctl bootout "$DOMAIN/$BRIDGE_LABEL" >/dev/null 2>&1 || true
    launchctl bootout "$DOMAIN/$TUNNEL_LABEL" >/dev/null 2>&1 || true
    rm -f "$BRIDGE_PLIST" "$TUNNEL_PLIST"
    echo "Removed macropad LaunchAgents"
    exit 0
fi

if [[ "$MODE" == "status" ]]; then
    launchctl print "$DOMAIN/$TUNNEL_LABEL" 2>/dev/null | grep -E 'state =|pid =' || true
    launchctl print "$DOMAIN/$BRIDGE_LABEL" 2>/dev/null | grep -E 'state =|pid =' || true
    exit 0
fi

if [[ -z "$TUNNEL_ID" ]]; then
    usage
    exit 2
fi

mkdir -p "$COPILOT_HOME" "$LAUNCH_AGENTS"

if ! command -v devtunnel >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        brew install --cask devtunnel
    else
        curl -sL https://aka.ms/DevTunnelCliInstall | bash
    fi
fi
DEVTUNNEL="$(command -v devtunnel)"

if [[ ! -x "$PYTHON" ]]; then
    python3 -m venv "$HOME/.pad-bridge-venv"
fi
if ! "$PYTHON" -c 'import serial' >/dev/null 2>&1; then
    "$PYTHON" -m pip install pyserial
fi

curl -fsSL "$BRIDGE_URL" -o "$BRIDGE"
chmod 700 "$BRIDGE"

if [[ ! -s "$TOKEN" ]]; then
    echo "Missing bridge token: $TOKEN" >&2
    exit 1
fi
chmod 600 "$TOKEN"

write_plist() {
    local path="$1"
    local label="$2"
    local log_path="$3"
    shift 3
    "$PYTHON" - "$path" "$label" "$log_path" "$@" <<'PY'
import plistlib
import sys

path, label, log_path, *arguments = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": arguments,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle)
PY
    plutil -lint "$path" >/dev/null
}

write_plist \
    "$TUNNEL_PLIST" \
    "$TUNNEL_LABEL" \
    "$COPILOT_HOME/macropad-tunnel-client.log" \
    "$DEVTUNNEL" connect "$TUNNEL_ID"

write_plist \
    "$BRIDGE_PLIST" \
    "$BRIDGE_LABEL" \
    "$COPILOT_HOME/pad-bridge.log" \
    "$PYTHON" "$BRIDGE" \
    --host 127.0.0.1 \
    --port 7831 \
    --token-file "$TOKEN"

launchctl bootout "$DOMAIN/$BRIDGE_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$DOMAIN/$TUNNEL_LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$TUNNEL_PLIST"
launchctl bootstrap "$DOMAIN" "$BRIDGE_PLIST"
launchctl kickstart -k "$DOMAIN/$TUNNEL_LABEL"
launchctl kickstart -k "$DOMAIN/$BRIDGE_LABEL"

echo "Installed persistent macropad bridge for tunnel $TUNNEL_ID"
echo "Status: $0 --status"
echo "Logs:   $COPILOT_HOME/macropad-tunnel-client.log"
echo "        $COPILOT_HOME/pad-bridge.log"
