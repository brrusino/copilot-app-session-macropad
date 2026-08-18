# SPDX-License-Identifier: MIT
"""Keybow 2040 macropad firmware for the GitHub Copilot app.

Responsibilities, in priority order:

1. **Dictation chord (HID).** Two adjacent keys drive one Ctrl+Win push-to-talk
   chord for Wispr Flow. This is handled entirely on-device -- it never round
   trips to the host -- so it has no added latency and keeps working when the
   daemon is not running.
2. **Key reporting.** Every press/release is emitted to the host over the USB CDC
   *data* port as line-delimited JSON, annotated with its role and session slot.
   The firmware is the single source of truth for the physical layout; the host
   never needs its own copy of the key map.
3. **LED state.** The host pushes *semantic* state names ("working", "unread"),
   not pixel values. The pad owns the palette and runs the animation locally, so
   we don't stream frames over serial and the lights keep breathing if the host
   stalls.

Edit ``config.py`` to change layout, colours or timing. Nothing here hardcodes a
physical key number.
"""

import math
import time

import usb_cdc
import usb_hid
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keycode import Keycode

try:
    import json
except ImportError:  # pragma: no cover - CircuitPython always has json
    json = None

from pmk import PMK
from pmk.platform.keybow2040 import Keybow2040 as Hardware

import config

#: Bumped whenever the host/pad protocol gains something the daemon depends on.
#:
#: 1 - initial: state, palette, brightness, key events
#: 2 - `type` (host asks the pad to type a chord) and `busy_done`
FIRMWARE_VERSION = 2
SLOT_COUNT = len(config.SESSION_KEYS)

keybow = PMK(Hardware())
keys = keybow.keys

_keyboard = Keyboard(usb_hid.devices)

# Resolve the configured chord names to real Keycode values once at startup.
# An unknown name is dropped with a REPL warning rather than crashing the
# firmware and taking every other key down with it.
def _resolve_chord(names):
    resolved = []
    for name in names:
        keycode = getattr(Keycode, name, None)
        if keycode is None:
            print("config: unknown DICTATION_CHORD keycode %r, ignoring" % (name,))
            continue
        resolved.append(keycode)
    return tuple(resolved)


_CHORD = _resolve_chord(getattr(config, "DICTATION_CHORD", ("LEFT_CONTROL", "LEFT_GUI")))

# Session keys type the app's own Ctrl+<n> shortcut directly, as a real USB
# keyboard would.
#
# This is deliberately NOT done by the host daemon. The daemon can only
# synthesise input with SendInput, which reaches nothing unless the daemon
# happens to sit on the interactive desktop -- and over RDP it is the client
# machine, not the daemon's machine, that owns the keyboard. Typing the chord
# here is the same path the dictation chord already proved works: the pad is a
# USB keyboard, so RDP forwards its keystrokes like any other.
_SEND_SHORTCUTS = bool(getattr(config, "SEND_SESSION_SHORTCUTS", True))
_SHORTCUT_MODIFIER = Keycode.LEFT_CONTROL
_DIGIT_NAMES = ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")
_DIGITS = tuple(getattr(Keycode, name, None) for name in _DIGIT_NAMES)


def _shortcut_for(key_number):
    """``Ctrl+<n>`` for the nth session slot. None if the key is not a slot."""
    if key_number not in config.SESSION_KEYS:
        return None
    index = config.SESSION_KEYS.index(key_number)
    if 0 <= index < len(_DIGITS) and _DIGITS[index] is not None:
        return _DIGITS[index]
    return None


# Chords the host asks the pad to type on its behalf.
#
# The host cannot type. Its only mechanism is SendInput, which reaches nothing
# unless the daemon happens to run on the interactive desktop, and over RDP the
# keyboard belongs to the client machine regardless. So any action that is
# really a keystroke has to come back here and be typed by the pad.
_CHORD_ALIASES = {
    "ctrl": "LEFT_CONTROL",
    "control": "LEFT_CONTROL",
    "shift": "LEFT_SHIFT",
    "alt": "LEFT_ALT",
    "win": "LEFT_GUI",
    "meta": "LEFT_GUI",
    "cmd": "LEFT_GUI",
    "esc": "ESCAPE",
    "escape": "ESCAPE",
    "enter": "ENTER",
    "return": "ENTER",
    "tab": "TAB",
    "space": "SPACE",
    "backspace": "BACKSPACE",
    "delete": "DELETE",
    "up": "UP_ARROW",
    "down": "DOWN_ARROW",
    "left": "LEFT_ARROW",
    "right": "RIGHT_ARROW",
    "comma": "COMMA",
    "period": "PERIOD",
    "backslash": "BACKSLASH",
}

_DIGIT_BY_CHAR = dict(zip("123456789", _DIGIT_NAMES))


def _parse_chord(text):
    """``"ctrl+shift+escape"`` -> a tuple of keycodes, or () if unusable."""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        return ()
    names = []
    for part in parts:
        name = _CHORD_ALIASES.get(part)
        if name is None and part in _DIGIT_BY_CHAR:
            name = _DIGIT_BY_CHAR[part]
        if name is None and len(part) == 1 and part.isalpha():
            name = part.upper()
        if name is None:
            name = part.upper()
        names.append(name)
    resolved = _resolve_chord(names)
    # All or nothing: a chord missing its modifier is not a safer version of
    # itself, it is a different keystroke sent into whatever has focus.
    return resolved if len(resolved) == len(names) else ()

# --- serial ---------------------------------------------------------------

_serial = usb_cdc.data
if _serial is not None:
    # Never let a full or unread host buffer stall the key/LED loop.
    _serial.timeout = 0
    _serial.write_timeout = 0

#: Inbound byte buffer. Deliberately ``bytes`` rebuilt by slicing rather than a
#: bytearray mutated in place: CircuitPython's bytearray does NOT support slice
#: deletion (``del buf[:n]`` raises TypeError), unlike CPython. That difference
#: crashed the firmware the instant the host sent its first message, which took
#: the LEDs and the dictation chord down with it.
_rx = b""
_RX_LIMIT = 4096


def _send(obj):
    """Best-effort write of one JSON line. Silently drops if the host is gone.

    Deliberately does NOT gate on ``_serial.connected``. That flag reflects DTR,
    and over an RDP-redirected COM port the device never sees the host assert
    it mid-run -- so gating on it left the pad permanently mute unless the host
    happened to already hold the port open when main() started. Writing
    unconditionally costs nothing: with ``write_timeout = 0`` a full buffer
    raises rather than blocking, and that is caught below.
    """
    if _serial is None:
        return
    try:
        _serial.write(json.dumps(obj).encode("utf-8") + b"\n")
    except Exception:
        # A dead or saturated host must never break the input path.
        pass


def _drain_serial(handler):
    """Read whatever is waiting and dispatch each complete line."""
    global _rx

    if _serial is None:
        return
    try:
        waiting = _serial.in_waiting
        if waiting:
            _rx += _serial.read(waiting)
    except Exception:
        return

    # Runaway garbage must not eat all of RAM.
    if len(_rx) > _RX_LIMIT:
        _rx = _rx[-_RX_LIMIT:]

    while True:
        idx = _rx.find(b"\n")
        if idx < 0:
            break
        raw = _rx[:idx]
        _rx = _rx[idx + 1:]
        if not raw.strip():
            continue
        try:
            handler(json.loads(raw.decode("utf-8")))
        except Exception:
            # Malformed frame: skip it, keep the link alive.
            pass


# --- state ----------------------------------------------------------------

# Live palette; starts as the config default and can be replaced at runtime.
_palette = dict(config.PALETTE)
_brightness = config.BRIGHTNESS

# Semantic state per session slot.
_slot_state = ["empty"] * SLOT_COUNT

# Momentary highlight for action keys: key number -> expiry timestamp.
_action_flash = {}
_ACTION_FLASH_SECS = 0.18

#: Keys currently showing a "press registered" pulse, and when it expires.
#: A fixed duration cannot be right here: focusing a session takes seconds and
#: varies, so the host clears this explicitly when the app has actually
#: navigated. The expiry is only a failsafe for a host that never replies.
_press_flash = {}
_PRESS_FLASH_MAX = 12.0
_PRESS_PULSE_PERIOD = 0.5

# Dictation chord hold refcount. The chord engages when the first of the two
# keys goes down and releases only when the last one comes up, so rolling off
# one key mid-sentence does not cut you off.
_dictation_down = set()
_chord_active = False

_host_last_seen = 0.0
_last_heartbeat = 0.0


def _host_connected(now):
    return (now - _host_last_seen) < config.HOST_TIMEOUT


def _press_chord():
    global _chord_active
    if _chord_active or not _CHORD:
        return
    try:
        _keyboard.press(*_CHORD)
        _chord_active = True
    except Exception:
        pass


def _release_chord():
    global _chord_active
    if not _chord_active:
        return
    try:
        _keyboard.release(*_CHORD)
    except Exception:
        pass
    finally:
        _chord_active = False


# --- inbound messages -----------------------------------------------------


def _handle_message(msg):
    global _host_last_seen, _brightness, _palette
    _host_last_seen = time.monotonic()

    kind = msg.get("t")
    if kind == "hb":
        return

    if kind == "state":
        slot = msg.get("k")
        state = msg.get("s")
        if isinstance(slot, int) and 0 <= slot < SLOT_COUNT and state in _palette:
            _slot_state[slot] = state
        return

    if kind == "states":
        values = msg.get("v") or []
        for i in range(min(SLOT_COUNT, len(values))):
            if values[i] in _palette:
                _slot_state[i] = values[i]
        return

    if kind == "busy_done":
        # The host finished acting on a key press, so stop the pulse. Sent
        # once the app has actually navigated, which is the only thing that
        # knows how long that took.
        slot = msg.get("k")
        if slot is None:
            _press_flash.clear()
        elif isinstance(slot, int) and 0 <= slot < SLOT_COUNT:
            _press_flash.pop(config.SESSION_KEYS[slot], None)
        return

    if kind == "type":
        # Type a chord on the host's behalf. See _parse_chord: the host has no
        # working way to synthesise keystrokes itself.
        chord = _parse_chord(msg.get("v") or "")
        if chord:
            try:
                _keyboard.send(*chord)
            except Exception:
                pass
        return

    if kind == "palette":
        for name, spec in (msg.get("v") or {}).items():
            try:
                colour, effect = spec
                _palette[name] = ((int(colour[0]), int(colour[1]), int(colour[2])), str(effect))
            except Exception:
                continue
        return

    if kind == "brightness":
        try:
            _brightness = max(0.0, min(1.0, float(msg.get("v"))))
        except Exception:
            pass
        return


# --- rendering ------------------------------------------------------------


def _effect_scale(effect, now):
    """Brightness multiplier for an animated effect at time ``now``."""
    if effect == "off":
        return 0.0
    if effect == "breathe":
        phase = (now % config.BREATHE_PERIOD) / config.BREATHE_PERIOD
        # Smooth sine, floored so "working" never goes fully dark.
        return 0.35 + 0.65 * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
    if effect == "pulse":
        # Hard square blink: demands attention rather than soothing.
        phase = (now % config.PULSE_PERIOD) / config.PULSE_PERIOD
        return 1.0 if phase < 0.5 else 0.12
    return 1.0


def _resolve(key_number, now, connected):
    """Return the (r, g, b) a physical key should currently show."""
    if key_number in config.DICTATION_KEYS:
        name = "dictation_live" if _chord_active else "dictation"
    elif key_number in config.ACTION_KEYS:
        name = "action_active" if _action_flash.get(key_number, 0) > now else "action"
    elif key_number in config.SESSION_KEYS:
        if _press_flash.get(key_number, 0) > now:
            # Blink white for as long as the navigation is in flight, so the
            # pad visibly stays busy rather than going quiet mid-operation.
            phase = (now % _PRESS_PULSE_PERIOD) / _PRESS_PULSE_PERIOD
            name = "pressed" if phase < 0.5 else "empty"
        elif not connected:
            name = "disconnected"
        else:
            name = _slot_state[config.SESSION_KEYS.index(key_number)]
    else:
        name = "empty"

    colour, effect = _palette.get(name, ((0, 0, 0), "off"))
    scale = _effect_scale(effect, now) * _brightness
    return (
        int(colour[0] * scale),
        int(colour[1] * scale),
        int(colour[2] * scale),
    )


def _render(now, connected):
    for number, key in enumerate(keys):
        r, g, b = _resolve(number, now, connected)
        key.set_led(r, g, b)


# --- key events -----------------------------------------------------------


def _describe(key_number):
    """Role metadata sent with every key event so the host needs no key map."""
    if key_number in config.DICTATION_KEYS:
        return {"role": "dictation"}
    if key_number in config.ACTION_KEYS:
        return {"role": "action", "action": config.ACTION_KEYS[key_number]}
    if key_number in config.SESSION_KEYS:
        return {"role": "session", "slot": config.SESSION_KEYS.index(key_number)}
    return {"role": "free"}


def _on_down(key_number, now):
    if key_number in config.DICTATION_KEYS:
        # Engage on the first key of the pair.
        if not _dictation_down:
            _press_chord()
        _dictation_down.add(key_number)
    elif key_number in config.ACTION_KEYS:
        _action_flash[key_number] = now + _ACTION_FLASH_SECS
    elif key_number in config.SESSION_KEYS:
        # Pulse until the host says the app has actually navigated. Focusing a
        # session takes several seconds, and a brief blip leaves you unsure
        # whether the press registered at all.
        _press_flash[key_number] = now + _PRESS_FLASH_MAX

    typed = False
    if _SEND_SHORTCUTS:
        digit = _shortcut_for(key_number)
        if digit is not None:
            try:
                _keyboard.send(_SHORTCUT_MODIFIER, digit)
                typed = True
            except Exception:
                # A failed keystroke must not break the serial path too, and
                # leaving `typed` false lets the host fall back to doing the
                # switch itself.
                pass

    event = {"t": "down", "k": key_number}
    if typed:
        # Tell the host the switch is already done, so it does not repeat it.
        event["typed"] = True
    event.update(_describe(key_number))
    _send(event)


def _on_up(key_number, now):
    if key_number in config.DICTATION_KEYS:
        _dictation_down.discard(key_number)
        # Release only once BOTH keys are up.
        if not _dictation_down:
            _release_chord()

    event = {"t": "up", "k": key_number}
    event.update(_describe(key_number))
    _send(event)


# --- main loop ------------------------------------------------------------


def main():
    global _last_heartbeat

    previous = [False] * len(keys)
    _send({"t": "hello", "fw": FIRMWARE_VERSION, "slots": SLOT_COUNT})

    while True:
        keybow.update()
        now = time.monotonic()

        _drain_serial(_handle_message)

        for number, key in enumerate(keys):
            pressed = key.pressed
            if pressed and not previous[number]:
                _on_down(number, now)
            elif not pressed and previous[number]:
                _on_up(number, now)
            previous[number] = pressed

        # Belt and braces: never leave the dictation chord stuck down if the
        # physical keys are all up (e.g. after a soft reload mid-press).
        if _chord_active and not _dictation_down:
            _release_chord()

        if now - _last_heartbeat >= config.HEARTBEAT_INTERVAL:
            _send({"t": "hb"})
            _last_heartbeat = now

        _render(now, _host_connected(now))
        time.sleep(0.005)


# Run the firmware, unless a host-side test harness has asked us not to.
#
# CircuitPython does NOT execute code.py with __name__ == "__main__" -- it uses
# the module's own name -- so guarding on that silently skips main(). The pad
# then appears to boot, do nothing, and drop to the REPL with no error at all,
# which is a genuinely confusing failure. Test harnesses set _TEST_IMPORT on the
# config module instead; that attribute never exists on the device.
if not getattr(config, "_TEST_IMPORT", False):
    try:
        main()
    finally:
        # Whatever happens, do not strand a modifier chord on the host OS.
        _release_chord()
