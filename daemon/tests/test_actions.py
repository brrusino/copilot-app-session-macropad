# SPDX-License-Identifier: MIT
"""Tests for deep links and keystroke chord parsing."""

import pytest

from macropad_daemon import actions


def test_deep_link_format():
    """The verified scheme is ghapp://sessions/<id>."""
    assert actions.session_deep_link("abc-123") == "ghapp://sessions/abc-123"


def test_focus_rejects_empty_session_id():
    assert actions.focus_session("") is False


@pytest.mark.parametrize(
    "chord,expected",
    [
        ("enter", ([], 0x0D)),
        ("escape", ([], 0x1B)),
        ("esc", ([], 0x1B)),
        ("ctrl+n", ([0x11], ord("N"))),
        ("control+n", ([0x11], ord("N"))),
        ("ctrl+shift+p", ([0x11, 0x10], ord("P"))),
        ("alt+f4", ([0x12], 0x73)),
        ("win+d", ([0x5B], ord("D"))),
        ("CTRL+Enter", ([0x11], 0x0D)),
        ("  ctrl + n  ", ([0x11], ord("N"))),
    ],
)
def test_parse_chord(chord, expected):
    assert actions.parse_chord(chord) == expected


@pytest.mark.parametrize("chord", ["", "   ", "nonsense", "ctrl+nonsense", "bogus+n", "+"])
def test_parse_chord_rejects_garbage(chord):
    assert actions.parse_chord(chord) is None


def test_send_chord_returns_false_on_unparseable():
    assert actions.send_chord("bogus+key") is False

def test_switch_uses_the_shortcut_when_the_app_is_on_screen(monkeypatch):
    """The deep link spawns github.exe to route the URL, measured at ~4.5s.

    The app's own Ctrl+<n> shortcut selects the same session with nothing
    launched, so it is the path that must be taken whenever it can be.
    """
    sent = []
    monkeypatch.setattr(actions, "IS_WINDOWS", True)
    monkeypatch.setattr(actions, "app_window", lambda: 1234)
    monkeypatch.setattr(actions, "_activate", lambda h: True)
    monkeypatch.setattr(actions, "send_chord", lambda c: sent.append(c) or True)
    monkeypatch.setattr(
        actions.ctypes, "windll", type("W", (), {"user32": type("U", (), {"GetForegroundWindow": staticmethod(lambda: 1234)})()})()
    )
    monkeypatch.setattr(actions, "focus_session", lambda s: pytest.fail("deep link used"))

    assert actions.switch_to_slot(2, "sess") is True
    assert sent == ["ctrl+3"]


def test_switch_falls_back_when_the_app_window_is_gone(monkeypatch):
    """No window means no keystroke target; the shell handler still works."""
    monkeypatch.setattr(actions, "IS_WINDOWS", True)
    monkeypatch.setattr(actions, "app_window", lambda: None)
    monkeypatch.setattr(actions, "send_chord", lambda c: pytest.fail("chord sent"))
    used = []
    monkeypatch.setattr(actions, "focus_session", lambda s: used.append(s) or True)

    assert actions.switch_to_slot(2, "sess") is True
    assert used == ["sess"]


def test_slots_past_the_single_digit_shortcuts_use_the_deep_link(monkeypatch):
    """Ctrl+<n> only spans the number row."""
    monkeypatch.setattr(actions, "IS_WINDOWS", True)
    monkeypatch.setattr(actions, "send_chord", lambda c: pytest.fail("chord sent"))
    used = []
    monkeypatch.setattr(actions, "focus_session", lambda s: used.append(s) or True)

    assert actions.switch_to_slot(actions.MAX_SHORTCUT_SLOT, "sess") is True
    assert used == ["sess"]
