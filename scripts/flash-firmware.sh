#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Copies the macropad firmware onto a Keybow 2040 running CircuitPython.
# macOS/Linux counterpart of flash-firmware.ps1.
#
# Usage:
#   ./flash-firmware.sh              # copy the real firmware
#   ./flash-firmware.sh --calibrate  # copy calibrate.py as code.py instead
#   ./flash-firmware.sh --drive /Volumes/CIRCUITPY
#
# Two things worth knowing:
#   * boot.py only takes effect on a hard reset. After the first install,
#     unplug and replug the pad so the USB serial data port appears -- a soft
#     reload is not enough.
#   * The PMK and adafruit_hid libraries are NOT copied by this script.
#     Install them into <CIRCUITPY>/lib first; see the README.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$REPO_ROOT/keybow"

DRIVE=""
CALIBRATE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --calibrate) CALIBRATE=1; shift ;;
        --drive) DRIVE="${2:-}"; shift 2 ;;
        -h|--help) sed -n '3,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

find_circuitpy() {
    # macOS mounts removable volumes under /Volumes; most Linux desktops use
    # /media/$USER or /run/media/$USER.
    local candidates=()
    for base in /Volumes "/media/${USER:-}" "/run/media/${USER:-}" /media; do
        [[ -d "$base" ]] || continue
        while IFS= read -r path; do
            [[ -n "$path" ]] && candidates+=("$path")
        done < <(find "$base" -maxdepth 2 -iname 'CIRCUITPY' 2>/dev/null || true)
    done

    if [[ ${#candidates[@]} -eq 1 ]]; then
        printf '%s' "${candidates[0]}"
    elif [[ ${#candidates[@]} -gt 1 ]]; then
        echo "Multiple CIRCUITPY volumes found; pass --drive to pick one:" >&2
        printf '  %s\n' "${candidates[@]}" >&2
        exit 1
    fi
}

if [[ -z "$DRIVE" ]]; then
    DRIVE="$(find_circuitpy || true)"
fi

if [[ -z "$DRIVE" ]]; then
    cat >&2 <<'EOF'
No CIRCUITPY volume found.

Check that:
  1. The Keybow 2040 is plugged in.
  2. It is running CircuitPython, not the stock firmware.
     Flash the CircuitPython .uf2 by holding BOOT while plugging in, then
     copying the .uf2 onto the RPI-RP2 volume that appears.
EOF
    exit 1
fi

[[ -d "$DRIVE" ]] || { echo "Drive $DRIVE is not accessible." >&2; exit 1; }

echo "Target: $DRIVE"

# Warn rather than fail: the libraries may be vendored some other way.
for lib in pmk adafruit_hid; do
    if [[ ! -d "$DRIVE/lib/$lib" ]]; then
        echo "  WARNING: $lib not found in $DRIVE/lib -- firmware will not run without it." >&2
    fi
done

for file in boot.py config.py; do
    cp "$FIRMWARE_DIR/$file" "$DRIVE/$file"
    echo "  copied $file"
done

if [[ $CALIBRATE -eq 1 ]]; then
    MAIN_SOURCE="calibrate.py"
else
    MAIN_SOURCE="code.py"
fi
cp "$FIRMWARE_DIR/$MAIN_SOURCE" "$DRIVE/code.py"
echo "  copied $MAIN_SOURCE -> code.py"

# Flush to the device before anyone yanks it out.
sync

echo
if [[ $CALIBRATE -eq 1 ]]; then
    cat <<'EOF'
Calibration firmware installed.
Open the CircuitPython REPL and press keys to read their numbers:

    screen /dev/tty.usbmodem*      (ctrl-a k to quit)

Put the results into ROWS in keybow/config.py, then re-run without --calibrate.
EOF
else
    cat <<'EOF'
Firmware installed.
If this is the first install, UNPLUG AND REPLUG the pad now so boot.py can
enable the USB serial data port.
EOF
fi
