#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Copies the macropad firmware onto a Keybow 2040 running CircuitPython.
# macOS/Linux counterpart of flash-firmware.ps1.
#
# Usage:
#   ./flash-firmware.sh --install-circuitpython   # step 1: put CircuitPython on
#   ./flash-firmware.sh --fetch-libs              # step 2: firmware + libraries
#   ./flash-firmware.sh --calibrate               # copy calibrate.py as code.py
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
INSTALL_CP=0

# Pinned so a future CircuitPython release can't silently change behaviour.
CIRCUITPYTHON_VERSION="10.2.1"
CIRCUITPYTHON_UF2="https://downloads.circuitpython.org/bin/pimoroni_keybow2040/en_US/adafruit-circuitpython-pimoroni_keybow2040-en_US-${CIRCUITPYTHON_VERSION}.uf2"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --calibrate) CALIBRATE=1; shift ;;
        --fetch-libs) FETCH_LIBS=1; shift ;;
        --install-circuitpython) INSTALL_CP=1; shift ;;
        --drive) DRIVE="${2:-}"; shift 2 ;;
        -h|--help) sed -n '3,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

REQUIRED_LIBS=(pmk adafruit_hid adafruit_is31fl3731)

install_circuitpython() {
    # A .uf2 is not a program you run. The RP2040 chip has a permanent
    # bootloader: hold BOOT while plugging in and it pretends to be a USB stick
    # called RPI-RP2. Copying a .uf2 onto that stick makes it flash itself and
    # reboot. That is the whole mechanism.
    local boot_vol=""
    for base in /Volumes "/media/${USER:-}" "/run/media/${USER:-}" /media; do
        [[ -d "$base" ]] || continue
        while IFS= read -r path; do
            [[ -n "$path" ]] && boot_vol="$path"
        done < <(find "$base" -maxdepth 2 -iname 'RPI-RP2' 2>/dev/null || true)
    done

    if [[ -z "$boot_vol" ]]; then
        cat >&2 <<'EOF'
Could not find the RPI-RP2 volume.

To get the pad into bootloader mode:
  1. Unplug the pad.
  2. Press and HOLD the small BOOT button on the board.
  3. While still holding it, plug the USB-C cable back in.
  4. Let go. A drive named RPI-RP2 should appear.

Then run this command again.
EOF
        return 1
    fi

    echo "Found bootloader volume: $boot_vol"
    local tmp
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN

    echo "Downloading CircuitPython ${CIRCUITPYTHON_VERSION} ..."
    if ! curl -fsSL "$CIRCUITPYTHON_UF2" -o "$tmp/cp.uf2"; then
        echo "Download failed: $CIRCUITPYTHON_UF2" >&2
        return 1
    fi

    echo "Copying it to the pad ..."
    # The board reboots the instant it has the file, so the copy often reports
    # an error or "disk not ejected properly". That is expected, not a failure.
    cp "$tmp/cp.uf2" "$boot_vol/" 2>/dev/null || true
    sync 2>/dev/null || true

    echo
    echo "CircuitPython is being written. The pad will reboot on its own."
    echo "Ignore any 'disk was not ejected properly' warning -- that is the"
    echo "board rebooting mid-copy, and is normal."
    echo
    echo "Wait for a drive named CIRCUITPY to appear, then run:"
    echo "    ./scripts/flash-firmware.sh --fetch-libs"
    return 0
}

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

if [[ $INSTALL_CP -eq 1 ]]; then
    install_circuitpython
    exit $?
fi

if [[ -z "$DRIVE" ]]; then
    DRIVE="$(find_circuitpy || true)"
fi

if [[ -z "$DRIVE" ]]; then
    cat >&2 <<'EOF'
No CIRCUITPY volume found.

If you have not installed CircuitPython on the pad yet, do that first:
    ./scripts/flash-firmware.sh --install-circuitpython

If you have, check that the pad is plugged in and that a drive named
CIRCUITPY appears in Finder. If you see RPI-RP2 instead, the board is still
in bootloader mode -- run the command above.
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
