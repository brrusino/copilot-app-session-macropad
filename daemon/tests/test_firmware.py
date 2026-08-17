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

    keycode_mod.Keycode = Keycode
    monkeypatch.setitem(sys.modules, "adafruit_hid", hid_pkg)
    monkeypatch.setitem(sys.modules, "adafruit_hid.keyboard", keyboard_mod)
    monkeypatch.setitem(sys.modules, "adafruit_hid.keycode", keycode_mod)

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

    spec = importlib.util.spec_from_file_location("code", FIRMWARE_DIR / "code.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._test_keyboard = keyboards[-1]
    yield module
    for name in ("code", "config"):
        sys.modules.pop(name, None)


# --- layout ---------------------------------------------------------------


def test_layout_covers_all_sixteen_keys(firmware):
    import config as fw_config

    assigned = set(fw_config.SESSION_KEYS) | set(fw_config.ACTION_KEYS)
    assigned |= set(fw_config.DICTATION_KEYS) | set(fw_config.FREE_KEYS)
    assert assigned == set(range(16))


def test_no_key_has_two_roles(firmware):
    import config as fw_config

    groups = [
        list(fw_config.SESSION_KEYS),
        list(fw_config.ACTION_KEYS),
        list(fw_config.DICTATION_KEYS),
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
    assert firmware._describe(fw_config.FREE_KEYS[0]) == {"role": "free"}
    action_key = list(fw_config.ACTION_KEYS)[0]
    described = firmware._describe(action_key)
    assert described["role"] == "action"
    assert described["action"] == fw_config.ACTION_KEYS[action_key]


# --- function-key fallback ------------------------------------------------
# Used when the daemon cannot see the pad's serial port; the pad types F13-F24
# and RDP carries the keystrokes into the remote session.


def test_function_keys_off_by_default(firmware):
    import config as fw_config

    assert fw_config.SEND_FUNCTION_KEYS is False


def test_session_slots_map_to_f13_upwards(firmware):
    import config as fw_config

    first = firmware._function_key_for(fw_config.SESSION_KEYS[0])
    eighth = firmware._function_key_for(fw_config.SESSION_KEYS[7])
    assert first == 0x68           # F13
    assert eighth == 0x68 + 7      # F20


def test_actions_continue_after_the_slots(firmware):
    import config as fw_config

    first_action = firmware._function_key_for(fw_config.ROWS[2][0])
    last_action = firmware._function_key_for(fw_config.ROWS[2][3])
    assert first_action == 0x68 + 8   # F21
    assert last_action == 0x68 + 11   # F24


def test_every_mapped_key_is_unique(firmware):
    """Two keys sharing an F-key would silently trigger the wrong thing."""
    import config as fw_config

    keys = list(fw_config.SESSION_KEYS) + list(fw_config.ROWS[2])
    codes = [firmware._function_key_for(k) for k in keys]
    assert None not in codes
    assert len(set(codes)) == len(codes)


def test_dictation_and_free_keys_send_no_function_key(firmware):
    import config as fw_config

    assert firmware._function_key_for(fw_config.DICTATION_KEYS[0]) is None
    assert firmware._function_key_for(fw_config.FREE_KEYS[0]) is None


def test_no_keystroke_sent_when_disabled(firmware):
    import config as fw_config

    firmware._on_down(fw_config.SESSION_KEYS[0], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == []


def test_keystroke_sent_when_enabled(firmware, monkeypatch):
    import config as fw_config

    monkeypatch.setattr(firmware, "_SEND_FKEYS", True)
    firmware._on_down(fw_config.SESSION_KEYS[3], 0.0)
    sends = [e for e in firmware._test_keyboard.history if e[0] == "send"]
    assert sends == [("send", (0x68 + 3,))]
