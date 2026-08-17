#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Copies the macropad firmware onto a Keybow 2040 running CircuitPython.
# macOS/Linux counterpart of flash-firmware.ps1.
#
# Usage:
#   ./flash-firmware.sh              # copy the real firmware
#   ./flash-firmware.sh --fetch-libs # also download the 3 required libraries
#   ./flash-firmware.sh --calibrate  # copy calibrate.py as code.py instead
#   ./flash-firmware.sh --drive /Volumes/CIRCUITPY
#
# Two things worth knowing:
#   * boot.py only takes effect on a hard reset. After the first install,
#     unplug and replug the pad so the USB serial data port appears -- a soft
#     reload is not enough.
#   * The firmware needs three libraries in <CIRCUITPY>/lib:
#         pmk                  Pimoroni, drives the keys and LEDs
#         adafruit_hid         the dictation keystroke chord
#         adafruit_is31fl3731  the LED matrix driver PMK sits on top of
#     Pass --fetch-libs to download all three, or install them yourself first.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$REPO_ROOT/keybow"

DRIVE=""
CALIBRATE=0
FETCH_LIBS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --calibrate) CALIBRATE=1; shift ;;
        --fetch-libs) FETCH_LIBS=1; shift ;;
        --drive) DRIVE="${2:-}"; shift 2 ;;
        -h|--help) sed -n '3,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

REQUIRED_LIBS=(pmk adafruit_hid adafruit_is31fl3731)

fetch_libs() {
    local dest="$1/lib"
    local tmp
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN

    mkdir -p "$dest"
    echo "Downloading libraries into $dest ..."

    # Source (.py) rather than the bundled .mpy: no version-matching against the
    # CircuitPython release, and these three are small enough for the RP2040.
    local -a sources=(
        "pmk|https://github.com/pimoroni/pmk-circuitpython/archive/refs/heads/main.tar.gz|pmk-circuitpython-main/lib/pmk"
        "adafruit_hid|https://github.com/adafruit/Adafruit_CircuitPython_HID/archive/refs/heads/main.tar.gz|Adafruit_CircuitPython_HID-main/adafruit_hid"
        "adafruit_is31fl3731|https://github.com/adafruit/Adafruit_CircuitPython_IS31FL3731/archive/refs/heads/main.tar.gz|Adafruit_CircuitPython_IS31FL3731-main/adafruit_is31fl3731"
    )

    for entry in "${sources[@]}"; do
        local name="${entry%%|*}"
        local rest="${entry#*|}"
        local url="${rest%%|*}"
        local subdir="${rest#*|}"

        echo "  fetching $name ..."
        if ! curl -fsSL "$url" -o "$tmp/$name.tar.gz"; then
            echo "    FAILED to download $name from $url" >&2
            return 1
        fi
        tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
        if [[ ! -d "$tmp/$subdir" ]]; then
            echo "    FAILED: expected $subdir inside the archive" >&2
            return 1
        fi
        rm -rf "${dest:?}/$name"
        cp -R "$tmp/$subdir" "$dest/$name"
        echo "    installed $name"
    done

    sync
    echo "Libraries installed."
    echo
}

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

if [[ $FETCH_LIBS -eq 1 ]]; then
    fetch_libs "$DRIVE"
fi

# Warn rather than fail: the libraries may be vendored some other way.
missing=0
for lib in "${REQUIRED_LIBS[@]}"; do
    if [[ ! -d "$DRIVE/lib/$lib" ]]; then
        echo "  WARNING: $lib not found in $DRIVE/lib" >&2
        missing=1
    fi
done
if [[ $missing -eq 1 ]]; then
    echo "  The firmware will not run without every library above." >&2
    echo "  Re-run with --fetch-libs to download them automatically." >&2
    echo >&2
fi

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
