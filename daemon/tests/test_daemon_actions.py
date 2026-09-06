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
from macropad_daemon.copilot_db import PinnedSession, Section


class FakeLink:
    connected = True

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def set_on_connect(self, callback) -> None:
        self._on_connect = callback

    def send(self, message: dict) -> bool:
        self.sent.append(message)
        return True


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


def test_both_transport_builds_serial_and_network_links(tmp_path):
    from macropad_daemon.multi_link import MultiLink
    from macropad_daemon.network_link import NetworkLink
    from macropad_daemon.serial_link import SerialLink

    cfg = config_module.Config(pad_transport="both", copilot_home=tmp_path)
    uninitialized = object.__new__(main_module.Daemon)

    link = uninitialized._build_link(cfg)

    assert isinstance(link, MultiLink)
    assert any(isinstance(child, SerialLink) for child in link._links)
    assert any(isinstance(child, NetworkLink) for child in link._links)
    assert (tmp_path / "macropad.token").is_file()


def test_next_attention_goes_to_the_session_that_wants_you(daemon):
    """The one action a session key cannot replace: rows 1 and 2 already give
    random access to every pin, so this has to act on state instead."""
    daemon.store.apply_snapshot(
        [session(0), session(1, unread=True), session(2)]
    )

    daemon._run_action("next_attention")

    assert daemon.link.sent == [{"t": "type", "v": "ctrl+2"}]


def test_session_key_clicks_the_exact_parent_workspace(daemon, monkeypatch):
    target = session(2)
    selected = []
    daemon.store.apply_snapshot([session(0), session(1), target])
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: True)
    monkeypatch.setattr(
        main_module.actions,
        "focus_pinned_session",
        lambda workspace_id, session_id, collapsed_group=None: selected.append(
            (workspace_id, session_id)
        )
        or True,
    )
    monkeypatch.setattr(daemon, "_await_navigation", lambda slot, item: None)

    daemon._focus_parent_session(2, target)

    assert selected == [("ws-2", "s-2")]


def test_exact_parent_click_falls_back_to_the_session_deep_link(daemon, monkeypatch):
    target = session(1)
    deep_links = []
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: True)
    monkeypatch.setattr(
        main_module.actions, "focus_pinned_session", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        main_module.actions,
        "focus_session",
        lambda session_id: deep_links.append(session_id) or True,
    )
    monkeypatch.setattr(daemon, "_await_navigation", lambda slot, item: None)

    daemon._focus_parent_session(1, target)

    assert deep_links == ["s-1"]


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


def test_brightness_levels_are_pushed_on_connect(daemon, monkeypatch):
    """So they can be retuned in the config the pad is sitting next to,
    rather than by reflashing it from another machine."""
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: True)
    daemon.cfg.brightness_levels = [0.35, 1.0, 1.6]

    daemon._on_pad_connect()

    assert {"t": "levels", "v": [0.35, 1.0, 1.6]} in daemon.link.sent


def test_no_levels_configured_pushes_nothing(daemon, monkeypatch):
    """An empty list would leave the pad with no level to step to."""
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: True)

    daemon._on_pad_connect()

    assert not [m for m in daemon.link.sent if m.get("t") == "levels"]


# --- sections: navigation, colours, and the two section-nav LEDs -----------
# Row 3 key 1 ("section_up") steps back and names the section on screen;
# row 3 key 2 ("section_down") steps forward and rolls up attention from every
# OTHER section. Neither wraps at a boundary.


def test_section_indicator_is_the_resting_colour_on_pinned(daemon):
    daemon._sections = [Section(name="Pinned", sessions=())]
    daemon._section_index = 0
    assert daemon._section_indicator_state() == "action"


def test_section_indicator_is_a_distinct_colour_per_group(daemon):
    daemon._sections = [
        Section(name="Pinned", sessions=()),
        Section(name="Group A", sessions=()),
        Section(name="Group B", sessions=()),
    ]

    daemon._section_index = 1
    assert daemon._section_indicator_state() == "section_color_0"
    daemon._section_index = 2
    assert daemon._section_indicator_state() == "section_color_1"


def test_section_indicator_cycles_when_more_groups_than_colours(daemon):
    daemon.cfg.section_colors = [(1, 2, 3), (4, 5, 6)]
    daemon._sections = [
        Section(name="Pinned", sessions=()),
        Section(name="A", sessions=()),
        Section(name="B", sessions=()),
        Section(name="C", sessions=()),
    ]

    daemon._section_index = 3  # group index 2, wraps to colour 0
    assert daemon._section_indicator_state() == "section_color_0"


def test_section_indicator_falls_back_with_no_configured_colours(daemon):
    daemon.cfg.section_colors = []
    daemon._sections = [Section(name="Pinned", sessions=()), Section(name="A", sessions=())]
    daemon._section_index = 1
    assert daemon._section_indicator_state() == "action"


def test_elsewhere_attention_is_resting_colour_with_only_one_section(daemon):
    daemon._sections = [Section(name="Pinned", sessions=(session(0),))]
    daemon._section_index = 0
    assert daemon._elsewhere_attention_state() == "action"


def test_elsewhere_attention_pools_every_other_section_both_directions(daemon):
    """A group needing you could sit on either side of the current section,
    not just the one already-visited direction."""
    daemon._sections = [
        Section(name="Pinned", sessions=(session(0),)),
        Section(name="Above", sessions=(session(1, unread=True),)),
        Section(name="Current", sessions=(session(2),)),
        Section(name="Below", sessions=(session(3, asking=True, asking_at=1.0),)),
    ]
    daemon._section_index = 2

    assert daemon._elsewhere_attention_state() == "needs_approval"


def test_elsewhere_attention_ignores_the_current_section(daemon):
    """Attention already on screen is not 'elsewhere'."""
    daemon._sections = [
        Section(name="Pinned", sessions=(session(0, asking=True, asking_at=1.0),)),
        Section(name="Other", sessions=(session(1),)),
    ]
    daemon._section_index = 0

    assert daemon._elsewhere_attention_state() == "action"


def test_elsewhere_attention_retires_an_answered_question_like_a_key_would(daemon):
    """The database still says agent_asking long after you answered; on a key
    a hook retires that. The elsewhere key blinked orange for a session that
    was plainly working because it read the raw database flag instead."""
    pinned = Section(name="Pinned", sessions=(session(0),))
    other = Section(name="Other", sessions=(session(1, asking=True, asking_at=1.0, is_running=True),))
    daemon._sections = [pinned, other]
    daemon._section_index = 0
    daemon.store.apply_snapshot(pinned.sessions, offscreen=daemon._offscreen_sessions())
    assert daemon._elsewhere_attention_state() == "needs_approval"

    daemon.store.apply_hook("userPromptSubmitted", "s-1")
    daemon.store.apply_snapshot(pinned.sessions, offscreen=daemon._offscreen_sessions())

    assert daemon._elsewhere_attention_state() == "working"


def test_section_nav_leds_are_pushed_by_action_name(daemon):
    daemon._sections = [
        Section(name="Pinned", sessions=()),
        Section(name="Group A", sessions=(session(0, unread=True),)),
    ]
    daemon._section_index = 0

    daemon._push_section_leds()

    assert {"t": "action_states", "v": {"section_up": "action", "section_down": "unread"}} in (
        daemon.link.sent
    )


def test_section_nav_leds_are_not_resent_unchanged(daemon):
    daemon._sections = [Section(name="Pinned", sessions=())]
    daemon._section_index = 0

    daemon._push_section_leds()
    daemon.link.sent.clear()
    daemon._push_section_leds()

    assert daemon.link.sent == []


def test_section_colours_are_registered_on_connect(daemon, monkeypatch):
    monkeypatch.setattr(main_module.actions, "app_is_foreground", lambda: True)
    daemon.cfg.section_colors = [(1, 2, 3), (4, 5, 6)]

    daemon._on_pad_connect()

    palette_messages = [m for m in daemon.link.sent if m.get("t") == "palette"]
    assert palette_messages
    sent_palette = palette_messages[0]["v"]
    assert sent_palette["section_color_0"] == [[1, 2, 3], "solid"]
    assert sent_palette["section_color_1"] == [[4, 5, 6], "solid"]


def test_change_section_steps_forward_and_back(daemon):
    daemon._sections = [
        Section(name="Pinned", sessions=(session(0),)),
        Section(name="Group A", sessions=(session(1),)),
    ]
    daemon._section_index = 0

    daemon._change_section(1)
    assert daemon._section_index == 1
    assert daemon.store.session_for_slot(0).name == "session 1"

    daemon._change_section(-1)
    assert daemon._section_index == 0
    assert daemon.store.session_for_slot(0).name == "session 0"


def test_change_section_does_not_wrap_past_either_end(daemon):
    daemon._sections = [Section(name="Pinned", sessions=(session(0),))]
    daemon._section_index = 0

    daemon._change_section(-1)
    assert daemon._section_index == 0

    daemon._change_section(1)
    assert daemon._section_index == 0


def test_section_up_and_down_actions_change_section(daemon):
    daemon._sections = [
        Section(name="Pinned", sessions=()),
        Section(name="Group A", sessions=()),
    ]
    daemon._section_index = 0

    daemon._run_action("section_down")
    assert daemon._section_index == 1

    daemon._run_action("section_up")
    assert daemon._section_index == 0


def test_section_change_rereads_the_database(daemon, monkeypatch):
    """Switching sections must not re-apply the last reconcile's snapshot:
    stale child questions and unread flags flashed orange/green on every
    switch back to Pinned until the next reconcile corrected them."""
    stale = [
        Section(name="Pinned", sessions=(session(0, asking=True, asking_at=1.0),)),
        Section(name="Group A", sessions=()),
    ]
    fresh = [
        Section(name="Pinned", sessions=(session(0, is_running=True),)),
        Section(name="Group A", sessions=()),
    ]
    daemon._sections = stale
    daemon._section_index = 1
    monkeypatch.setattr(daemon.db, "sections", lambda slot_count: fresh)

    daemon._run_action("section_up")

    assert daemon._sections is fresh
    assert daemon.store.slot_states()[0] == "working"


def test_section_change_keeps_the_other_sections_hook_evidence(daemon, monkeypatch):
    """A question retired by a hook must stay retired across a round trip
    through another section. Pruning the off-screen session's overlay made
    the stale agent_asking win again on every switch back, so the key blinked
    orange until that session's next hook."""
    pinned = Section(name="Pinned", sessions=(session(0, asking=True, asking_at=1.0),))
    group = Section(name="Group A", sessions=(session(1),))
    sections = [pinned, group]
    daemon._sections = sections
    daemon._section_index = 0
    monkeypatch.setattr(daemon.db, "sections", lambda slot_count: sections)
    daemon.store.apply_snapshot(pinned.sessions, offscreen=daemon._offscreen_sessions())
    daemon.store.apply_hook("userPromptSubmitted", "s-0")
    assert daemon.store.slot_states()[0] == "working"

    daemon._run_action("section_down")
    daemon._run_action("section_up")

    assert daemon.store.slot_states()[0] == "working"


def test_status_reports_the_current_section_and_key_colours(tmp_path, capsys):
    """--status must show the same section info the LEDs would, since that's
    what it exists for when there is no pad plugged in to read."""
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_copilot_db import build_db

    build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[
            ("ws-1", "First", "s-1", None),
            ("ws-2", "Grouped", "s-2", None),
        ],
        sessions=[("s-1", "a", 0, 0, None), ("s-2", "b", 0, 0, None)],
        groups=[{"id": "g-1", "name": "Touchstone Pilots", "members": ["ws-2"]}],
    )
    cfg = config_module.Config(copilot_home=tmp_path)

    result = main_module._print_status(cfg)

    out = capsys.readouterr().out
    assert result == 0
    assert "section  : Pinned (1/2)" in out
    assert "section_down" in out
    assert "section_up" in out
