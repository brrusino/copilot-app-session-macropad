# SPDX-License-Identifier: MIT
"""Tests for the on-device firmware logic, with CircuitPython stubbed out.

The dictation chord is the part most worth pinning down: a stuck Ctrl+Win would
be actively unpleasant to be on the receiving end of, and it is the one piece
that talks straight to the host OS.

``code.py`` guards its entry point with ``if __name__ == "__main__"``, which is
true on-device (CircuitPython runs it as ``__main__``) but lets us import it
here without starting the main loop.
"""

from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

FIRMWARE_DIR = Path(__file__).resolve().parents[2] / "keybow"


class FakeKey:
    def __init__(self):
        self.pressed = False
        self.led = (0, 0, 0)

    def set_led(self, r, g, b):
        self.led = (r, g, b)


class FakeKeyboard:
    """Records press/release/send so we can assert the chord is balanced."""

    def __init__(self, *_args):
        self.held = set()
        self.history = []

    def press(self, *keycodes):
        self.held.update(keycodes)
        self.history.append(("press", keycodes))

    def release(self, *keycodes):
        self.held.difference_update(keycodes)
        self.history.append(("release", keycodes))

    def send(self, *keycodes):
        self.history.append(("send", keycodes))


def _install_stubs(monkeypatch):
    """Stub the CircuitPython-only modules the firmware imports."""
    keyboards = []

    usb_cdc = types.ModuleType("usb_cdc")
    usb_cdc.data = None  # no host attached
    monkeypatch.setitem(sys.modules, "usb_cdc", usb_cdc)

    usb_hid = types.ModuleType("usb_hid")
    usb_hid.devices = []
    monkeypatch.setitem(sys.modules, "usb_hid", usb_hid)

    hid_pkg = types.ModuleType("adafruit_hid")
    keyboard_mod = types.ModuleType("adafruit_hid.keyboard")

    def make_keyboard(*args):
        kbd = FakeKeyboard()
        keyboards.append(kbd)
        return kbd

    keyboard_mod.Keyboard = make_keyboard
    keycode_mod = types.ModuleType("adafruit_hid.keycode")

    class Keycode:
        LEFT_CONTROL = "LEFT_CONTROL"
        LEFT_GUI = "LEFT_GUI"
        LEFT_ALT = "LEFT_ALT"
        LEFT_SHIFT = "LEFT_SHIFT"

    # Real HID usage codes for F13-F24, matching adafruit_hid.
    for _n in range(13, 25):
        setattr(Keycode, "F%d" % _n, 0x68 + (_n - 13))

    # Real HID usage codes for the digit row, matching adafruit_hid. These are
    # what Ctrl+<n> is built from, so a wrong value here would silently switch
    # to the wrong session.
    for _i, _name in enumerate(
        ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")
    ):
        setattr(Keycode, _name, 0x1E + _i)

    # Letters and the named keys a host-requested chord may reference, again
    # with adafruit_hid's real values.
    for _i in range(26):
        setattr(Keycode, chr(ord("A") + _i), 0x04 + _i)
    Keycode.ESCAPE = 0x29
    Keycode.ENTER = 0x28
    Keycode.TAB = 0x2B
    Keycode.SPACE = 0x2C
    Keycode.COMMA = 0x36
    Keycode.BACKSLASH = 0x31
    Keycode.BACKSPACE = 0x2A
    Keycode.DELETE = 0x4C
    Keycode.FORWARD_SLASH = 0x38

    keycode_mod.Keycode = Keycode

    # Literal-text typing, recorded on the same keyboard history so a test can
    # assert the whole sequence in order.
    layout_mod = types.ModuleType("adafruit_hid.keyboard_layout_us")

    class KeyboardLayoutUS:
        def __init__(self, keyboard):
            self._keyboard = keyboard

        def write(self, text):
            self._keyboard.history.append(("write", text))

    layout_mod.KeyboardLayoutUS = KeyboardLayoutUS

    monkeypatch.setitem(sys.modules, "adafruit_hid", hid_pkg)
    monkeypatch.setitem(sys.modules, "adafruit_hid.keyboard", keyboard_mod)
    monkeypatch.setitem(sys.modules, "adafruit_hid.keycode", keycode_mod)
    monkeypatch.setitem(
        sys.modules, "adafruit_hid.keyboard_layout_us", layout_mod
    )

    pmk_pkg = types.ModuleType("pmk")
    platform_pkg = types.ModuleType("pmk.platform")
    board_mod = types.ModuleType("pmk.platform.keybow2040")

    class PMK:
        def __init__(self, _hardware):
            self.keys = [FakeKey() for _ in range(16)]

        def update(self):
            pass

    board_mod.Keybow2040 = object
    pmk_pkg.PMK = PMK
    monkeypatch.setitem(sys.modules, "pmk", pmk_pkg)
    monkeypatch.setitem(sys.modules, "pmk.platform", platform_pkg)
    monkeypatch.setitem(sys.modules, "pmk.platform.keybow2040", board_mod)

    return keyboards


@pytest.fixture
def firmware(monkeypatch):
    keyboards = _install_stubs(monkeypatch)
    monkeypatch.syspath_prepend(str(FIRMWARE_DIR))
    for name in ("code", "config"):
        sys.modules.pop(name, None)

    # code.py calls main() on import -- that is exactly what must happen on the
    # device. Flag the config module so the entry point is skipped here instead.
    import config as fw_config

    fw_config._TEST_IMPORT = True

    spec = importlib.util.spec_from_file_location("code", FIRMWARE_DIR / "code.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._test_keyboard = keyboards[-1]
    yield module
    for name in ("code", "config"):
        sys.modules.pop(name, None)


def test_entry_point_is_not_guarded_on_dunder_name():
    """Regression test for a bug that made the pad silently do nothing.

    CircuitPython does not run code.py with __name__ == "__main__", so an
    `if __name__ == "__main__":` guard skips main() entirely. The pad boots,
    prints no error, and drops to the REPL -- with the LEDs frozen on whatever
    was set last, which looks like working hardware.
    """
    source = (FIRMWARE_DIR / "code.py").read_text(encoding="utf-8")
    # Ignore comments: the explanation of this bug naturally mentions the guard.
    code_lines = [
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    ]
    body = "\n".join(code_lines)
    assert "__main__" not in body
    assert "_TEST_IMPORT" in body


def test_no_slice_deletion_on_buffers():
    """CircuitPython's bytearray does not support slice deletion.

    `del buf[:n]` raises TypeError on the device but works fine in CPython, so
    the host test suite cannot catch it by execution. It crashed the firmware
    the instant the daemon sent its first message, taking the LEDs and the
    dictation chord down with it. Assert on the source instead.
    """
    source = (FIRMWARE_DIR / "code.py").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "del _rx" not in body
    # The inbound buffer must be immutable bytes rebuilt by slicing.
    assert "_rx = b\"\"" in body or "_rx = b''" in body


def test_inbound_buffer_reassembles_frames(firmware):
    """The rewritten buffering must still split frames correctly."""
    seen = []
    firmware._rx = b""

    class FakeSerial:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        @property
        def in_waiting(self):
            return len(self.chunks[0]) if self.chunks else 0

        def read(self, _n):
            return self.chunks.pop(0) if self.chunks else b""

    firmware._serial = FakeSerial([b'{"t":"hb"}\n{"t":"sta', b'te","k":1,"s":"idle"}\n'])
    firmware._drain_serial(seen.append)
    firmware._drain_serial(seen.append)

    assert seen[0] == {"t": "hb"}
    assert seen[1] == {"t": "state", "k": 1, "s": "idle"}
    assert firmware._rx == b""


def test_inbound_buffer_is_bounded(firmware):
    """Runaway input must not grow without bound."""
    firmware._rx = b""

    class Flood:
        @property
        def in_waiting(self):
            return 8192

        def read(self, _n):
            return b"x" * 8192

    firmware._serial = Flood()
    firmware._drain_serial(lambda msg: None)
    assert len(firmware._rx) <= firmware._RX_LIMIT


# --- layout ---------------------------------------------------------------


def test_layout_covers_all_sixteen_keys(firmware):
    import config as fw_config

    assigned = set(fw_config.SESSION_KEYS) | set(fw_config.ACTION_KEYS)
    assigned |= set(fw_config.DICTATION_KEYS) | set(fw_config.FREE_KEYS)
    assigned |= set(fw_config.TYPING_KEYS)
    assert assigned == set(range(16))


def test_no_key_has_two_roles(firmware):
    import config as fw_config

    groups = [
        list(fw_config.SESSION_KEYS),
        list(fw_config.ACTION_KEYS),
        list(fw_config.DICTATION_KEYS),
        list(fw_config.TYPING_KEYS),
        list(fw_config.FREE_KEYS),
    ]
    flat = [k for group in groups for k in group]
    assert len(flat) == len(set(flat))


def test_eight_session_slots(firmware):
    assert firmware.SLOT_COUNT == 8


def test_dictation_keys_are_adjacent_on_the_bottom_row(firmware):
    import config as fw_config

    bottom = list(fw_config.ROWS[3])
    positions = sorted(bottom.index(k) for k in fw_config.DICTATION_KEYS)
    assert positions[1] - positions[0] == 1


# --- dictation chord ------------------------------------------------------


def test_chord_engages_on_first_key_down(firmware):
    import config as fw_config

    first, _ = fw_config.DICTATION_KEYS
    firmware._on_down(first, 0.0)
    assert firmware._chord_active is True
    assert firmware._test_keyboard.held == {"LEFT_CONTROL", "LEFT_GUI"}


def test_chord_releases_when_the_only_key_comes_up(firmware):
    import config as fw_config

    first, _ = fw_config.DICTATION_KEYS
    firmware._on_down(first, 0.0)
    firmware._on_up(first, 0.1)
    assert firmware._chord_active is False
    assert firmware._test_keyboard.held == set()


def test_rolling_off_one_key_does_not_cut_dictation(firmware):
    """The whole point of the two-key hold refcount."""
    import config as fw_config

    first, second = fw_config.DICTATION_KEYS
    firmware._on_down(first, 0.0)
    firmware._on_down(second, 0.1)
    firmware._on_up(first, 0.2)
    # Still talking: one key is down.
    assert firmware._chord_active is True
    firmware._on_up(second, 0.3)
    assert firmware._chord_active is False


def test_chord_pressed_once_not_twice(firmware):
    """Both keys down must not double-press the modifiers."""
    import config as fw_config

    first, second = fw_config.DICTATION_KEYS
    firmware._on_down(first, 0.0)
    firmware._on_down(second, 0.1)
    presses = [e for e in firmware._test_keyboard.history if e[0] == "press"]
    assert len(presses) == 1


def test_chord_is_balanced_after_a_messy_sequence(firmware):
    """However you mash them, nothing may be left held."""
    import config as fw_config

    first, second = fw_config.DICTATION_KEYS
    for step in [
        (firmware._on_down, first),
        (firmware._on_down, second),
        (firmware._on_up, second),
        (firmware._on_down, second),
        (firmware._on_up, first),
        (firmware._on_up, second),
    ]:
        step[0](step[1], 0.0)
    assert firmware._chord_active is False
    assert firmware._test_keyboard.held == set()


def test_release_chord_is_idempotent(firmware):
    firmware._release_chord()
    firmware._release_chord()
    assert firmware._chord_active is False


def test_chord_comes_from_config(firmware):
    """The chord is data, not hardcoded, so it can follow the dictation tool."""
    import config as fw_config

    assert fw_config.DICTATION_CHORD == ("LEFT_CONTROL", "LEFT_GUI")
    assert firmware._CHORD == ("LEFT_CONTROL", "LEFT_GUI")


def test_chord_resolution_drops_unknown_keycodes(firmware):
    """A typo in config must not brick every other key on the pad."""
    resolved = firmware._resolve_chord(("LEFT_CONTROL", "NOT_A_REAL_KEY", "LEFT_ALT"))
    assert resolved == ("LEFT_CONTROL", "LEFT_ALT")


def test_empty_chord_does_not_press_anything(firmware, monkeypatch):
    monkeypatch.setattr(firmware, "_CHORD", ())
    firmware._press_chord()
    assert firmware._chord_active is False
    assert firmware._test_keyboard.held == set()


# --- LED resolution -------------------------------------------------------


def test_session_keys_show_disconnected_without_a_host(firmware):
    import config as fw_config

    key = fw_config.SESSION_KEYS[0]
    colour = firmware._resolve(key, 0.0, connected=False)
    expected_base = fw_config.PALETTE["disconnected"][0]
    assert colour == tuple(int(c * fw_config.BRIGHTNESS) for c in expected_base)


def test_session_key_reflects_pushed_state(firmware):
    import config as fw_config

    firmware._handle_message({"t": "states", "v": ["error"] + ["empty"] * 7})
    key = fw_config.SESSION_KEYS[0]
    r, g, b = firmware._resolve(key, 0.0, connected=True)
    assert r > g and r > b


def test_empty_slot_is_dark(firmware):
    import config as fw_config

    firmware._handle_message({"t": "states", "v": ["empty"] * 8})
    assert firmware._resolve(fw_config.SESSION_KEYS[0], 0.0, connected=True) == (0, 0, 0)


def test_dictation_key_lights_while_chord_is_live(firmware):
    import config as fw_config

    key = fw_config.DICTATION_KEYS[0]
    idle_colour = firmware._resolve(key, 0.0, connected=True)
    firmware._on_down(key, 0.0)
    live_colour = firmware._resolve(key, 0.0, connected=True)
    assert sum(live_colour) > sum(idle_colour)


def test_working_breathes_between_frames(firmware):
    """A breathing effect must actually change over time."""
    import config as fw_config

    firmware._handle_message({"t": "states", "v": ["working"] + ["empty"] * 7})
    key = fw_config.SESSION_KEYS[0]
    a = firmware._resolve(key, 0.0, connected=True)
    b = firmware._resolve(key, fw_config.BREATHE_PERIOD / 2, connected=True)
    assert a != b


def test_needs_approval_pulses(firmware):
    import config as fw_config

    firmware._handle_message({"t": "states", "v": ["needs_approval"] + ["empty"] * 7})
    key = fw_config.SESSION_KEYS[0]
    bright = firmware._resolve(key, 0.0, connected=True)
    dim = firmware._resolve(key, fw_config.PULSE_PERIOD * 0.75, connected=True)
    assert sum(bright) > sum(dim)


# --- protocol -------------------------------------------------------------


def test_palette_override_applied(firmware):
    import config as fw_config

    firmware._handle_message({"t": "palette", "v": {"idle": [[10, 20, 30], "solid"]}})
    firmware._handle_message({"t": "states", "v": ["idle"] + ["empty"] * 7})
    colour = firmware._resolve(fw_config.SESSION_KEYS[0], 0.0, connected=True)
    assert colour == tuple(int(c * fw_config.BRIGHTNESS) for c in (10, 20, 30))


def test_brightness_message_scales_output(firmware):
    import config as fw_config

    firmware._handle_message({"t": "states", "v": ["idle"] + ["empty"] * 7})
    firmware._handle_message({"t": "brightness", "v": 0.0})
    assert firmware._resolve(fw_config.SESSION_KEYS[0], 0.0, connected=True) == (0, 0, 0)


def test_unknown_state_name_is_ignored(firmware):
    firmware._handle_message({"t": "states", "v": ["idle"] + ["empty"] * 7})
    firmware._handle_message({"t": "states", "v": ["bogus"] + ["empty"] * 7})
    assert firmware._slot_state[0] == "idle"


def test_out_of_range_slot_is_ignored(firmware):
    firmware._handle_message({"t": "state", "k": 99, "s": "error"})
    assert all(s != "error" for s in firmware._slot_state)


def test_malformed_messages_do_not_raise(firmware):
    for message in [{}, {"t": "state"}, {"t": "palette", "v": {"idle": "nope"}},
                    {"t": "brightness", "v": "abc"}, {"t": "unknown"}]:
        firmware._handle_message(message)


def test_key_events_carry_role_metadata(firmware):
    import config as fw_config

    assert firmware._describe(fw_config.SESSION_KEYS[2]) == {"role": "session", "slot": 2}
    assert firmware._describe(fw_config.DICTATION_KEYS[0]) == {"role": "dictation"}
    assert firmware._describe(list(fw_config.TYPING_KEYS)[0]) == {"role": "typing"}
    action_key = list(fw_config.ACTION_KEYS)[0]
    described = firmware._describe(action_key)
    assert described["role"] == "action"
    assert described["action"] == fw_config.ACTION_KEYS[action_key]


# --- the app's Ctrl+<n> shortcut ------------------------------------------
# The pad types the switch itself. The daemon cannot: SendInput reaches
# nothing unless it happens to run on the interactive desktop, and over RDP
# the keyboard belongs to the client machine anyway.


def test_shortcuts_on_by_default(firmware):
    import config as fw_config

    assert fw_config.SEND_SESSION_SHORTCUTS is True


def test_session_slots_map_to_the_digit_row(firmware):
    import config as fw_config

    assert firmware._shortcut_for(fw_config.SESSION_KEYS[0]) == 0x1E   # 1
    assert firmware._shortcut_for(fw_config.SESSION_KEYS[7]) == 0x25   # 8


def test_every_slot_maps_to_a_distinct_digit(firmware):
    """Two slots sharing a digit would silently switch to the wrong session."""
    import config as fw_config

    codes = [firmware._shortcut_for(k) for k in fw_config.SESSION_KEYS]
    assert None not in codes
    assert len(set(codes)) == len(codes)


def test_non_session_keys_send_no_shortcut(firmware):
    """Ctrl+<n> means "switch session"; nothing else may claim it."""
    import config as fw_config

    assert firmware._shortcut_for(fw_config.DICTATION_KEYS[0]) is None
    assert firmware._shortcut_for(list(fw_config.TYPING_KEYS)[0]) is None
    assert firmware._shortcut_for(fw_config.ROWS[2][0]) is None


def test_pressing_a_session_key_types_the_shortcut(firmware):
    import config as fw_config

    firmware._on_down(fw_config.SESSION_KEYS[3], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", ("LEFT_CONTROL", 0x21))]   # left ctrl + 4


def test_the_press_event_says_the_pad_already_typed_it(firmware):
    """Otherwise the daemon switches a second time, or fires the slow link."""
    import config as fw_config

    sent = []
    firmware._send = lambda msg: sent.append(msg)
    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    assert sent and sent[-1].get("typed") is True


def test_a_failed_keystroke_leaves_the_host_to_do_it(firmware):
    """If the pad could not type it, the daemon must not assume it did."""
    import config as fw_config

    def boom(*_a, **_k):
        raise RuntimeError("no HID")

    firmware._keyboard.send = boom
    sent = []
    firmware._send = lambda msg: sent.append(msg)
    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    assert sent and "typed" not in sent[-1]


def test_shortcuts_can_be_turned_off(firmware, monkeypatch):
    import config as fw_config

    monkeypatch.setattr(firmware, "_SEND_SHORTCUTS", False)
    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == []
# --- typing a chord the host asked for ------------------------------------


def test_host_can_ask_the_pad_to_type_a_chord(firmware):
    """The host cannot type: SendInput reaches nothing off the interactive
    desktop, and over RDP the keyboard belongs to the client machine."""
    firmware._handle_message({"t": "type", "v": "ctrl+escape"})
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", ("LEFT_CONTROL", 0x29))]


def test_a_chord_missing_a_key_is_not_typed_at_all(firmware):
    """A chord missing its modifier is a different keystroke, not a safer one.

    Sending the bare key into whatever has focus is worse than doing nothing.
    """
    firmware._handle_message({"t": "type", "v": "ctrl+notarealkey"})
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == []


def test_digits_and_letters_both_parse(firmware):
    assert firmware._parse_chord("ctrl+3") == ("LEFT_CONTROL", 0x20)
    assert firmware._parse_chord("ctrl+n") == ("LEFT_CONTROL", 0x11)


def test_an_empty_chord_types_nothing(firmware):
    firmware._handle_message({"t": "type", "v": ""})
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == []

# --- raising the app before acting on it ----------------------------------
# The daemon cannot do this: Windows refuses SetForegroundWindow from a process
# that did not receive the last input event, verified returning False here. The
# pad is a keyboard, so it can.


def test_no_focus_chord_when_the_app_is_already_in_front(firmware):
    """Win+<n> toggles -- sent while the app is focused it MINIMISES it."""
    import config as fw_config

    firmware._app_focused = True
    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", ("LEFT_CONTROL", 0x1E))]


def test_the_app_is_raised_first_when_it_is_not_in_front(firmware):
    import config as fw_config

    firmware._app_focused = False
    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", ("LEFT_GUI", 0x24))]   # win+7


def test_the_keystroke_waits_for_the_app_to_come_forward(firmware):
    """Typing straight after Win+<n> races the window coming up, so the
    keystroke would land wherever focus still was."""
    import config as fw_config

    firmware._app_focused = False
    firmware._on_down(fw_config.SESSION_KEYS[2], 0.0)
    assert firmware._pending_chords

    firmware._handle_message({"t": "focus", "v": True})
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends[-1] == ("send", ("LEFT_CONTROL", 0x20))   # ctrl+3
    assert not firmware._pending_chords


def test_a_stale_pending_keystroke_is_dropped(firmware):
    """Firing it later, into whatever you moved on to, is worse than dropping."""
    import config as fw_config

    firmware._app_focused = False
    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    assert firmware._pending_chords

    firmware._discard_pending()
    firmware._handle_message({"t": "focus", "v": True})
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", ("LEFT_GUI", 0x24))]   # only the focus chord


def test_dictation_never_steals_focus(firmware):
    """It types into whatever you are already using; that is the point."""
    import config as fw_config

    assert fw_config.DICTATION_KEYS[0] not in fw_config.FOCUS_KEYS
    assert fw_config.DICTATION_KEYS[1] not in fw_config.FOCUS_KEYS


def test_every_other_key_raises_the_app(firmware):
    import config as fw_config

    expected = set(range(16)) - set(fw_config.DICTATION_KEYS)
    assert set(fw_config.FOCUS_KEYS) == expected


def test_a_pad_with_no_daemon_assumes_the_app_is_focused(firmware):
    """Assuming the opposite makes an unattended pad send Win+<n> on every
    press, which toggles -- it would minimise the app as often as raise it."""
    assert firmware._app_focused is True

def test_the_rubber_duck_key_types_the_command(firmware):
    """The app has a first-class /rubber-duck command, so type that rather
    than a sentence asking for the same thing."""
    import config as fw_config

    firmware._app_focused = True
    firmware._on_down(fw_config.ROWS[2][1], 0.0)
    history = firmware._test_keyboard.history
    assert history == [("write", "/rubber-duck")]
    assert firmware._type_queue, "the Enter is held back"


def test_the_rubber_duck_binding_is_a_slash_command(firmware):
    """A sentence would depend on an agent reading the wording and deciding
    what was meant; a command is dispatched directly."""
    import config as fw_config

    assert fw_config.RUBBER_DUCK_COMMAND.startswith("/")


def test_the_mode_key_cycles_with_shift_tab(firmware):
    """Verified enabled on this install: modeCycleShiftTabEnabled is true."""
    import config as fw_config

    firmware._app_focused = True
    firmware._on_down(fw_config.ROWS[2][2], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", ("LEFT_SHIFT", 0x2B))]


def test_the_compact_key_waits_before_submitting(firmware):
    """Typing "/" opens the app's command menu and each letter filters it,
    which is async webview work. Sent back-to-back over USB, the Enter arrived
    before the menu had settled and left "/compact" sitting in the composer
    unsent. The text lands fine at full speed, so only the Enter waits.
    """
    import config as fw_config

    firmware._app_focused = True
    firmware._on_down(fw_config.ROWS[2][3], 0.0)
    assert firmware._test_keyboard.history == [("write", "/compact")]
    assert firmware._type_queue, "the Enter is held back"


def test_the_held_enter_is_sent_once_the_menu_has_settled(firmware):
    import config as fw_config

    firmware._app_focused = True
    firmware._on_down(fw_config.ROWS[2][3], 0.0)
    firmware._test_keyboard.history.clear()

    # Too early: nothing yet.
    firmware._drain_type_queue(firmware._type_ready_at - 0.01)
    assert [e for e in firmware._test_keyboard.history if e[0] == "send"] == []

    firmware._drain_type_queue(firmware._type_ready_at)
    sends = [e[1] for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [(0x28,)]   # enter
    assert not firmware._type_queue


def test_the_pause_does_not_stall_the_key_loop(firmware):
    """Sleeping through it would freeze key scanning and the LED animation,
    and could hold the dictation chord down past its release."""
    import config as fw_config

    firmware._app_focused = True
    before = time.monotonic()
    firmware._on_down(fw_config.ROWS[2][3], 0.0)
    assert time.monotonic() - before < 0.1
