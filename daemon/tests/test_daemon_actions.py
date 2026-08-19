# SPDX-License-Identifier: MIT
"""Row 3 actions must be typed by the pad, never by this process.

Regression tests for a failure that looked like the app ignoring shortcuts.
The daemon's only way to synthesise a keystroke is SendInput, which reaches
nothing unless the process happens to sit on the interactive desktop -- sending
Win+R from the daemon's context produced no Run dialog at all. Over RDP it is
worse in principle: the keyboard belongs to the client machine, not the machine
the daemon runs on. The pad is a real USB keyboard, so it is the only thing in
the system that can actually type.
"""

from __future__ import annotations

import pytest

from macropad_daemon import config as config_module
from macropad_daemon import main as main_module
from macropad_daemon.copilot_db import PinnedSession


class FakeLink:
    connected = True

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def set_on_connect(self, callback) -> None:
        self._on_connect = callback

    def send(self, message: dict) -> None:
        self.sent.append(message)


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    cfg = config_module.Config()
    cfg.copilot_home = tmp_path

    link = FakeLink()
    monkeypatch.setattr(main_module.Daemon, "_build_link", lambda self, c: link)
    monkeypatch.setattr(main_module, "hook_server_for", lambda c, cb: object())

    d = main_module.Daemon(cfg)
    d.link = link
    return d


def session(slot: int, **kwargs) -> PinnedSession:
    return PinnedSession(
        slot=slot,
        workspace_id=f"ws-{slot}",
        session_id=f"s-{slot}",
        name=f"session {slot}",
        is_running=kwargs.get("is_running", False),
        unread=kwargs.get("unread", False),
        was_interrupted=kwargs.get("was_interrupted", False),
        asking=kwargs.get("asking", False),
        asking_at=kwargs.get("asking_at", 0.0),
        auto_approve=True,
    )


def test_next_attention_goes_to_the_session_that_wants_you(daemon):
    """The one action a session key cannot replace: rows 1 and 2 already give
    random access to every pin, so this has to act on state instead."""
    daemon.store.apply_snapshot(
        [session(0), session(1, unread=True), session(2)]
    )

    daemon._run_action("next_attention")

    assert daemon.link.sent == [{"t": "type", "v": "ctrl+2"}]


def test_a_question_outranks_unread(daemon):
    """Priority mirrors urgency: a question blocks the agent outright."""
    daemon.store.apply_snapshot(
        [session(0, unread=True), session(1, asking=True, asking_at=1.0)]
    )

    daemon._run_action("next_attention")

    assert daemon.link.sent == [{"t": "type", "v": "ctrl+2"}]


def test_repeated_presses_walk_the_list(daemon):
    """Otherwise it sticks on the first match and you can never reach the
    second thing that wants you."""
    daemon.store.apply_snapshot(
        [session(0, unread=True), session(1, unread=True)]
    )

    daemon._run_action("next_attention")
    daemon._run_action("next_attention")

    assert daemon.link.sent == [
        {"t": "type", "v": "ctrl+1"},
        {"t": "type", "v": "ctrl+2"},
    ]


def test_nothing_needing_attention_does_nothing(daemon):
    daemon.store.apply_snapshot([session(0), session(1)])
    daemon._run_action("next_attention")
    assert daemon.link.sent == []


def test_actions_never_call_sendinput(daemon, monkeypatch):
    """The whole point: this process cannot type, so it must not try."""

    def fail(*_a, **_k):
        raise AssertionError("daemon tried to synthesise a keystroke itself")

    monkeypatch.setattr(main_module.actions, "send_chord", fail)
    monkeypatch.setattr(main_module.actions, "focus_then_chord", fail)

    daemon.store.apply_snapshot([session(0), session(1, unread=True)])
    daemon._run_action("next_attention")


def test_an_old_pad_is_reported_once(daemon, caplog):
    """An old pad ignores messages it does not recognise, so the actions that
    depend on them fail silently and look like the app ignoring its shortcuts."""
    import logging

    with caplog.at_level(logging.WARNING):
        daemon._on_pad_event({"t": "hb", "fw": 1})
        daemon._on_pad_event({"t": "hb", "fw": 1})

    warnings = [r for r in caplog.records if "reflash" in r.getMessage()]
    assert len(warnings) == 1


def test_a_current_pad_says_nothing(daemon, caplog):
    import logging

    from macropad_daemon.main import REQUIRED_FIRMWARE

    with caplog.at_level(logging.WARNING):
        daemon._on_pad_event({"t": "hb", "fw": REQUIRED_FIRMWARE})

    assert [r for r in caplog.records if "reflash" in r.getMessage()] == []


def test_the_version_is_learned_from_a_heartbeat_not_only_hello(daemon):
    """The pad is powered by the machine it plugs into, so it usually booted
    long before the daemon started and its hello is already gone."""
    daemon._on_pad_event({"t": "hb", "fw": 1})
    assert daemon._pad_firmware == 1

def test_focus_is_pushed_only_when_it_changes(daemon, monkeypatch):
    """The pad needs this to decide whether to raise the app, and Win+<n>
    toggles -- so a wrong answer minimises the app instead of raising it."""
    state = {"focused": False}
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: state["focused"])

    daemon._push_focus()
    daemon._push_focus()
    assert daemon.link.sent == [{"t": "focus", "v": False}]

    state["focused"] = True
    daemon._push_focus()
    assert daemon.link.sent[-1] == {"t": "focus", "v": True}


def test_focus_is_resent_on_connect(daemon, monkeypatch):
    """A pad that just came up assumes the app is focused, which is wrong as
    often as it is right."""
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: False)

    daemon._push_focus()
    daemon.link.sent.clear()
    daemon._push_focus(force=True)

    assert daemon.link.sent == [{"t": "focus", "v": False}]
