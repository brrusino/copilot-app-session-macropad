# SPDX-License-Identifier: MIT
"""Tests for slot state resolution and the hook/database conflict rule."""

import pytest

from dataclasses import replace

from macropad_daemon.copilot_db import PinnedSession
from macropad_daemon.state import (
    EMPTY,
    ERROR,
    IDLE,
    NEEDS_APPROVAL,
    INTERRUPTED,
    UNREAD,
    WORKING,
    WORKING_HOLD,
    StateStore,
)


def session(
    slot=0,
    session_id="sess-a",
    is_running=False,
    unread=False,
    name="demo",
    auto_approve=False,
):
    return PinnedSession(
        slot=slot,
        workspace_id=f"ws-{slot}",
        session_id=session_id,
        name=name,
        is_running=is_running,
        unread=unread,
        was_interrupted=False,
        auto_approve=auto_approve,
    )


def store_with(*sessions, slot_count=8, at=100.0):
    store = StateStore(slot_count=slot_count)
    store.apply_snapshot(list(sessions), now=at)
    return store


# --- basic resolution -----------------------------------------------------


def test_unfilled_slots_are_empty():
    store = store_with(session())
    states = store.slot_states()
    assert states[0] == IDLE
    assert states[1:] == [EMPTY] * 7


def test_workspace_without_session_is_empty():
    store = store_with(session(session_id=None))
    assert store.slot_states()[0] == EMPTY


def test_running_session_from_database_is_working():
    store = store_with(session(is_running=True))
    assert store.slot_states()[0] == WORKING


def test_unread_from_database():
    store = store_with(session(unread=True))
    assert store.slot_states()[0] == UNREAD


# --- hook-driven states ---------------------------------------------------


def test_permission_request_shows_needs_approval():
    """A session that actually asks should go amber."""
    store = store_with(session(auto_approve=False))
    store.apply_hook("permissionRequest", "sess-a", now=101.0)
    assert store.slot_states()[0] == NEEDS_APPROVAL


def test_auto_approving_session_never_shows_needs_approval():
    """Regression test for the LED blinking orange throughout normal work.

    permissionRequest fires before EVERY tool call, and captured payloads carry
    no field distinguishing "a human must answer" from "auto-approved". When a
    session auto-approves, nobody is ever blocked, so the only honest reading is
    that work is happening.
    """
    store = store_with(session(auto_approve=True))
    store.apply_hook("permissionRequest", "sess-a", now=101.0)
    assert store.slot_states()[0] == WORKING


def test_permission_request_implies_work_is_happening():
    """A tool is starting, so the session is demonstrably active."""
    store = store_with(session(auto_approve=True, is_running=False), at=100.0)
    store.apply_hook("permissionRequest", "sess-a", now=101.0)
    assert store.slot_states()[0] == WORKING


def test_error_event_shows_error():
    store = store_with(session())
    store.apply_hook("errorOccurred", "sess-a", now=101.0)
    assert store.slot_states()[0] == ERROR


def test_approval_outranks_error():
    """An approval prompt blocks progress; it must win over a past failure."""
    store = store_with(session())
    store.apply_hook("errorOccurred", "sess-a", now=101.0)
    store.apply_hook("permissionRequest", "sess-a", now=102.0)
    assert store.slot_states()[0] == NEEDS_APPROVAL


def test_prompt_clears_previous_error():
    store = store_with(session())
    store.apply_hook("errorOccurred", "sess-a", now=101.0)
    store.apply_hook("userPromptSubmitted", "sess-a", now=102.0)
    assert store.slot_states()[0] == WORKING


def test_prompt_clears_pending_approval():
    store = store_with(session())
    store.apply_hook("permissionRequest", "sess-a", now=101.0)
    store.apply_hook("userPromptSubmitted", "sess-a", now=102.0)
    assert store.slot_states()[0] == WORKING


def test_agent_stop_clears_pending_approval():
    store = store_with(session())
    store.apply_hook("permissionRequest", "sess-a", now=101.0)
    store.apply_hook("agentStop", "sess-a", now=102.0)
    assert store.slot_states()[0] != NEEDS_APPROVAL


def test_session_end_resets_state():
    store = store_with(session())
    store.apply_hook("errorOccurred", "sess-a", now=101.0)
    store.apply_hook("sessionEnd", "sess-a", now=102.0)
    assert store.slot_states()[0] == IDLE


def test_hook_for_unknown_session_is_ignored():
    store = store_with(session())
    store.apply_hook("errorOccurred", "sess-other", now=101.0)
    assert store.slot_states()[0] == IDLE


def test_empty_session_id_is_ignored():
    store = store_with(session())
    store.apply_hook("errorOccurred", "", now=101.0)
    assert store.slot_states()[0] == IDLE


# --- the conflict rule ----------------------------------------------------
# The database is authoritative for "still running". Hooks may only make us
# react faster to work STARTING, never contradict the app into idle.


def test_agent_stop_does_not_override_a_still_running_session():
    """Regression test for the LED flapping blue/white once per agent turn.

    agentStop fires at the end of every turn, but a session working through a
    long task stays is_running across many turns. Measured on real hardware:
    roughly one flip per turn while is_running never changed once in 45s.
    """
    store = store_with(session(is_running=True), at=100.0)
    store.apply_hook("agentStop", "sess-a", now=101.0)
    assert store.slot_states()[0] == WORKING


def test_agent_stop_settles_once_the_app_agrees():
    """When the app finally reports not-running, the turn's output is unread.

    Settling waits out ``WORKING_HOLD``: a stop is only believed once the
    session has stayed stopped longer than the gap between two turns.
    """
    store = store_with(session(is_running=True), at=100.0)
    store.apply_hook("agentStop", "sess-a", now=101.0)
    assert store.slot_states()[0] == WORKING
    # The app records the finished turn: no longer running, output unread.
    store.apply_snapshot([session(is_running=False, unread=True)], now=102.0)
    assert store.slot_states()[0] == WORKING, "a brief stop is just a turn boundary"
    store.apply_snapshot(
        [session(is_running=False, unread=True)], now=102.0 + WORKING_HOLD
    )
    assert store.slot_states()[0] == UNREAD


def test_app_saying_not_unread_is_believed():
    """If the app cleared unread, you have read it -- do not keep showing green."""
    store = store_with(session(is_running=True), at=100.0)
    store.apply_hook("agentStop", "sess-a", now=101.0)
    store.apply_snapshot(
        [session(is_running=False, unread=False)], now=102.0 + WORKING_HOLD
    )
    assert store.slot_states()[0] == IDLE


def test_snapshot_after_hook_beats_stale_hook():
    """A stale hook must not pin a slot to a state forever."""
    store = StateStore()
    store.apply_hook("agentStop", "sess-a", now=99.0)
    store.apply_snapshot([session(is_running=True)], now=100.0)
    assert store.slot_states()[0] == WORKING


def test_prompt_before_snapshot_catches_up():
    """Hook says working before the database has caught up."""
    store = store_with(session(is_running=False), at=100.0)
    store.apply_hook("userPromptSubmitted", "sess-a", now=101.0)
    assert store.slot_states()[0] == WORKING


def test_stale_working_hook_does_not_pin_the_slot():
    """Once a newer snapshot disagrees, the database wins."""
    store = StateStore()
    store.apply_hook("userPromptSubmitted", "sess-a", now=99.0)
    store.apply_snapshot([session(is_running=False)], now=100.0)
    assert store.slot_states()[0] == IDLE


def test_unread_hint_shows_green_before_database_agrees():
    store = store_with(session(unread=False), at=100.0)
    store.apply_hook("agentStop", "sess-a", now=101.0)
    assert store.slot_states()[0] == UNREAD


def test_unread_hint_cleared_by_newer_snapshot():
    """Once you've read it, the app clears unread and so must we."""
    store = store_with(session(unread=False), at=100.0)
    store.apply_hook("agentStop", "sess-a", now=101.0)
    assert store.slot_states()[0] == UNREAD
    store.apply_snapshot([session(unread=False)], now=102.0)
    assert store.slot_states()[0] == IDLE


# --- housekeeping ---------------------------------------------------------


def test_overlays_pruned_when_session_unpinned():
    """Overlay dict must not grow without bound over a long uptime."""
    store = store_with(session(session_id="sess-a"))
    store.apply_hook("errorOccurred", "sess-a", now=101.0)
    assert "sess-a" in store._overlays
    store.apply_snapshot([session(session_id="sess-b")], now=102.0)
    assert "sess-a" not in store._overlays


def test_snapshot_truncated_to_slot_count():
    many = [session(slot=i, session_id=f"s{i}") for i in range(12)]
    store = StateStore(slot_count=8)
    store.apply_snapshot(many)
    assert len(store.slot_states()) == 8
    assert store.session_for_slot(8) is None


# --- attention routing ----------------------------------------------------


def test_next_attention_prioritises_approval_over_error_and_unread():
    store = StateStore()
    store.apply_snapshot(
        [
            session(slot=0, session_id="s0", unread=True),
            session(slot=1, session_id="s1"),
            session(slot=2, session_id="s2"),
        ],
        now=100.0,
    )
    store.apply_hook("errorOccurred", "s1", now=101.0)
    store.apply_hook("permissionRequest", "s2", now=101.0)
    assert store.next_attention_slot() == 2


def test_next_attention_falls_through_to_unread():
    store = store_with(session(slot=0, session_id="s0", unread=True))
    assert store.next_attention_slot() == 0


def test_next_attention_none_when_all_idle():
    store = store_with(session())
    assert store.next_attention_slot() is None


def test_next_attention_cycles_past_the_previous_slot():
    store = StateStore()
    store.apply_snapshot(
        [
            session(slot=0, session_id="s0", unread=True),
            session(slot=1, session_id="s1", unread=True),
        ],
        now=100.0,
    )
    assert store.next_attention_slot() == 0
    assert store.next_attention_slot(after=0) == 1
    # Wraps back around rather than dead-ending.
    assert store.next_attention_slot(after=1) == 0


@pytest.mark.parametrize("event", ["preToolUse", "postToolUse", "subagentStart"])
def test_optional_activity_events_still_understood(event):
    """We don't register these, but handling them must not break if added."""
    store = store_with(session())
    store.apply_hook(event, "sess-a", now=101.0)
    assert store.slot_states()[0] == WORKING

def test_working_survives_the_gap_between_turns():
    """A session mid-task drops to not-running between turns.

    Following that literally made the LED cycle blue -> green -> blue every few
    seconds while work was plainly still going. Measured gaps reached 5.8s.
    """
    store = StateStore(slot_count=2)
    running = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=True, was_interrupted=False,
    )
    stopped = replace(running, is_running=False)

    store.apply_snapshot([running], now=100.0)
    assert store.resolve(running) == WORKING

    # Between turns: still working, even though the app says not running.
    store.apply_snapshot([stopped], now=105.0)
    assert store.resolve(stopped) == WORKING


def test_working_gives_way_once_the_session_really_stops():
    """The hold is a bridge, not a latch: a finished session must go green."""
    store = StateStore(slot_count=2)
    running = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=True, was_interrupted=False,
    )
    stopped = replace(running, is_running=False)

    store.apply_snapshot([running], now=100.0)
    store.apply_snapshot([stopped], now=100.0 + WORKING_HOLD + 1)
    assert store.resolve(stopped) == UNREAD


def test_read_session_settles_to_idle_after_it_stops():
    """Blue while working, green when unread and stopped, white once read."""
    store = StateStore(slot_count=2)
    running = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=True, was_interrupted=False,
    )
    read_and_stopped = replace(running, is_running=False, unread=False)

    store.apply_snapshot([running], now=100.0)
    store.apply_snapshot([read_and_stopped], now=100.0 + WORKING_HOLD + 1)
    assert store.resolve(read_and_stopped) == IDLE

def test_asking_outranks_working():
    """A session sitting on a question still reports is_running.

    Letting working win meant a slot blocked on you showed as busy, so you
    never learned it wanted an answer.
    """
    store = StateStore(slot_count=2)
    waiting = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=False, was_interrupted=False, asking=True,
    )
    store.apply_snapshot([waiting], now=100.0)
    assert store.resolve(waiting) == NEEDS_APPROVAL

def test_answering_a_question_retires_it_before_the_app_notices():
    """The app writes no activity item until a whole turn ends.

    So agent_asking stays the newest item for as long as the answer takes to
    work through -- observed at over twenty minutes. Without hook evidence the
    slot blinks orange the entire time you watch it work, which is exactly the
    false alarm the state exists to prevent.
    """
    asked_at = 1_000_000.0
    store = StateStore(slot_count=2)
    waiting = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=False, was_interrupted=False,
        asking=True, asking_at=asked_at,
    )
    store.apply_snapshot([waiting], now=100.0)
    assert store.resolve(waiting) == NEEDS_APPROVAL

    # You answer: a tool call fires after the question was asked.
    overlay = store.overlay_for("s")
    overlay.worked_wall = asked_at + 5
    overlay.working = True
    overlay.working_at = 100.0
    assert store.resolve(waiting) == WORKING


def test_work_from_before_the_question_does_not_retire_it():
    """Only activity *after* the question counts as having answered it."""
    asked_at = 1_000_000.0
    store = StateStore(slot_count=2)
    waiting = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=False, was_interrupted=False,
        asking=True, asking_at=asked_at,
    )
    store.apply_snapshot([waiting], now=100.0)
    store.overlay_for("s").worked_wall = asked_at - 30
    assert store.resolve(waiting) == NEEDS_APPROVAL


def test_interrupted_outranks_working():
    """A slot frozen mid-task otherwise sits there looking busy forever."""
    store = StateStore(slot_count=2)
    stopped = PinnedSession(
        slot=0, workspace_id="w", session_id="s", name="n",
        is_running=True, unread=False, was_interrupted=True,
    )
    store.apply_snapshot([stopped], now=100.0)
    assert store.resolve(stopped) == INTERRUPTED
