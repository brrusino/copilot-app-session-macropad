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


def test_new_session_is_typed_by_the_pad(daemon):
    daemon._run_action("new_session")
    assert daemon.link.sent == [{"t": "type", "v": daemon.cfg.actions.new_session}]


def test_interrupt_acts_on_the_focused_session_without_navigating(daemon, monkeypatch):
    """Row 3 acts on the session in front of you.

    Switching and acting on one press means acting on a session you have not
    looked at -- stopping work you cannot see, or approving a prompt you have
    not read. Navigation is what the next-attention key is for.
    """
    daemon.store.apply_snapshot([session(0, is_running=True), session(1, is_running=True)])
    monkeypatch.setattr(daemon.db, "focused_ids", lambda: {"s-1"})

    daemon._run_action("interrupt")

    assert daemon.link.sent == [{"t": "type", "v": daemon.cfg.actions.interrupt}]


def test_interrupt_does_nothing_when_the_session_is_not_working(daemon, monkeypatch):
    daemon.store.apply_snapshot([session(0)])
    monkeypatch.setattr(daemon.db, "focused_ids", lambda: {"s-0"})

    daemon._run_action("interrupt")

    assert daemon.link.sent == []


def test_approve_acts_on_the_focused_session_without_navigating(daemon, monkeypatch):
    daemon.store.apply_snapshot(
        [session(0, is_running=True), session(1, asking=True, asking_at=1.0)]
    )
    monkeypatch.setattr(daemon.db, "focused_ids", lambda: {"s-1"})

    daemon._run_action("approve")

    assert daemon.link.sent == [{"t": "type", "v": daemon.cfg.actions.approve}]


def test_approve_refuses_when_the_focused_session_is_not_asking(daemon, monkeypatch):
    """The important one. approve types Enter, so aimed at a session whose
    composer holds text it would send that text rather than approve anything.
    """
    daemon.store.apply_snapshot([session(0, is_running=True)])
    monkeypatch.setattr(daemon.db, "focused_ids", lambda: {"s-0"})

    daemon._run_action("approve")

    assert daemon.link.sent == []


def test_nothing_happens_when_the_app_is_not_on_a_pad_session(daemon, monkeypatch):
    daemon.store.apply_snapshot([session(0, asking=True, asking_at=1.0)])
    monkeypatch.setattr(daemon.db, "focused_ids", lambda: {"some-other-session"})

    daemon._run_action("approve")
    daemon._run_action("interrupt")

    assert daemon.link.sent == []


def test_next_attention_switches_through_the_pad(daemon):
    daemon.store.apply_snapshot([session(0), session(1, unread=True)])

    daemon._run_action("next_attention")

    assert daemon.link.sent == [{"t": "type", "v": "ctrl+2"}]


def test_actions_never_call_sendinput(daemon, monkeypatch):
    """The whole point: this process cannot type, so it must not try."""

    def fail(*_a, **_k):
        raise AssertionError("daemon tried to synthesise a keystroke itself")

    monkeypatch.setattr(main_module.actions, "send_chord", fail)
    monkeypatch.setattr(main_module.actions, "focus_then_chord", fail)

    daemon.store.apply_snapshot([session(0, asking=True, asking_at=1.0)])
    for action in ("new_session", "interrupt", "approve", "next_attention"):
        daemon._run_action(action)
