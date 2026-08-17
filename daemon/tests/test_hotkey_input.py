# SPDX-License-Identifier: MIT
"""Tests for the global-hotkey input transport.

This is the fallback for when the pad's serial port cannot reach the daemon at
all -- the pad types F13-F24 and RDP carries the keystrokes into the session.
"""

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="global hotkeys are a Windows API"
)

from macropad_daemon.hotkey_input import (  # noqa: E402
    ACTIONS,
    SESSION_KEY_COUNT,
    VK_F13,
    HotkeyListener,
    vk_for_action,
    vk_for_slot,
)


def test_slot_keycodes_are_f13_upwards():
    assert vk_for_slot(0) == VK_F13
    assert vk_for_slot(7) == VK_F13 + 7


def test_action_keycodes_follow_the_slots():
    assert vk_for_action(0) == VK_F13 + SESSION_KEY_COUNT
    assert vk_for_action(3) == VK_F13 + SESSION_KEY_COUNT + 3


def test_all_twelve_fit_in_f13_to_f24():
    """F24 is the last usable code, so the layout must not overflow it."""
    highest = vk_for_action(len(ACTIONS) - 1)
    assert highest <= 0x87  # VK_F24


def test_action_order_matches_the_firmware_row():
    assert ACTIONS == ("approve", "interrupt", "next_attention", "new_session")


def test_registers_and_unregisters_cleanly():
    listener = HotkeyListener(lambda kind, index: None)
    listener.start()
    try:
        assert listener.connected is True
        assert listener.failed == []
    finally:
        listener.stop()

    # A second listener can claim the same hotkeys once the first has released
    # them, which proves unregistration actually happened.
    second = HotkeyListener(lambda kind, index: None)
    second.start()
    try:
        assert second.failed == []
    finally:
        second.stop()


def test_send_always_fails_so_leds_show_disconnected():
    """A keyboard has no return path; pretending otherwise would show stale state."""
    listener = HotkeyListener(lambda kind, index: None)
    assert listener.send({"t": "states", "v": ["idle"] * 8}) is False


def test_dispatch_maps_ids_to_sessions_and_actions():
    seen = []
    listener = HotkeyListener(lambda kind, index: seen.append((kind, index)))

    listener._dispatch(0)
    listener._dispatch(SESSION_KEY_COUNT - 1)
    listener._dispatch(SESSION_KEY_COUNT)
    listener._dispatch(SESSION_KEY_COUNT + 3)

    assert seen == [
        ("session", 0),
        ("session", SESSION_KEY_COUNT - 1),
        ("action", 0),
        ("action", 3),
    ]


def test_dispatch_survives_a_failing_handler():
    listener = HotkeyListener(lambda kind, index: 1 / 0)
    listener._dispatch(0)  # must not raise


def test_second_listener_reports_conflicts_rather_than_failing_silently():
    """If another app owns the hotkeys, say so instead of listening for nothing."""
    first = HotkeyListener(lambda kind, index: None)
    first.start()
    try:
        second = HotkeyListener(lambda kind, index: None)
        second.start()
        try:
            assert second.failed, "expected the duplicate registration to be reported"
        finally:
            second.stop()
    finally:
        first.stop()
