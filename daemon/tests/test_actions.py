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
