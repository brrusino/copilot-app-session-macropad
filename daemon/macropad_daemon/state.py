# SPDX-License-Identifier: MIT
"""Slot state resolution: merges the hook push feed with the database snapshot.

Neither source is sufficient alone:

* **Hooks** are instant and precise about agent activity (``userPromptSubmitted``
  -> working, ``agentStop`` -> done, ``permissionRequest`` -> needs approval,
  ``errorOccurred`` -> error) but they cannot see *you*. Reading a session clears
  its unread badge inside the app and no hook fires.
* **The database** is authoritative for unread and pin order, but we only observe
  it on a poll, so it lags a fast agent by up to one reconcile interval.

Conflict rule
-------------
The database is authoritative for whether a session is *still running*; hooks
may only make the pad react faster to work **starting**, never contradict the
app into idle. ``agentStop`` fires at the end of every agent turn, but a session
working through a long task stays ``is_running`` across many turns -- so letting
the newer hook win made the LED flap blue/white once per turn while the app's
own flag never changed at all.

Everything hooks alone can see -- an approval prompt, an error, output you have
not read yet -- is theirs outright, because the database has no equivalent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .copilot_db import PinnedSession

log = logging.getLogger("macropad")

# These strings are the wire contract with the firmware; they must stay in sync
# with the keys of ``PALETTE`` in keybow/config.py.
EMPTY = "empty"
IDLE = "idle"
WORKING = "working"
UNREAD = "unread"
NEEDS_APPROVAL = "needs_approval"
INTERRUPTED = "interrupted"
ERROR = "error"

ALL_STATES = (EMPTY, IDLE, WORKING, UNREAD, NEEDS_APPROVAL, INTERRUPTED, ERROR)

#: How long a session keeps showing "working" after ``is_running`` goes false.
#:
#: A session working through a task does not stay continuously running: it
#: drops to not-running in the gap between turns, then resumes. Rendering that
#: literally makes the LED cycle blue -> green -> blue every few seconds while
#: the work is plainly still going, which is unreadable at a glance.
#:
#: Derived from measurement, not preference: sampling ``is_running`` every
#: 0.2s for 60s across the pinned sessions caught six such gaps that resumed,
#: the longest 5.8s. The hold must exceed the longest real gap or the flicker
#: survives, so this sits at roughly 1.7x that with headroom for a slower
#: machine. The cost is that a genuinely finished session waits this long
#: before turning green, which is the right trade: a steady light you can read
#: beats a fast one you cannot.
WORKING_HOLD = 10.0

# Hook event names, as emitted by the Copilot CLI hooks system.
EVENT_SESSION_START = "sessionStart"
EVENT_SESSION_END = "sessionEnd"
EVENT_USER_PROMPT = "userPromptSubmitted"
EVENT_PRE_TOOL = "preToolUse"
EVENT_POST_TOOL = "postToolUse"
EVENT_AGENT_STOP = "agentStop"
EVENT_PERMISSION = "permissionRequest"
EVENT_ERROR = "errorOccurred"
EVENT_SUBAGENT_START = "subagentStart"
EVENT_SUBAGENT_STOP = "subagentStop"

WORKING_EVENTS = frozenset(
    {EVENT_USER_PROMPT, EVENT_PRE_TOOL, EVENT_POST_TOOL, EVENT_SUBAGENT_START, EVENT_SUBAGENT_STOP}
)


@dataclass
class SessionOverlay:
    """Hook-derived beliefs about one session."""

    working: bool = False
    #: When ``working`` last changed. Compared against the snapshot time to
    #: decide whether hooks or the database win.
    working_at: float = 0.0
    pending_approval: bool = False
    pending_approval_at: float = 0.0
    error: bool = False
    #: Set on agentStop so the slot can go green before the app records unread.
    unread_hint: bool = False
    unread_hint_at: float = 0.0
    #: Wall-clock time of the last hook showing this session doing work.
    #:
    #: Wall clock rather than monotonic on purpose: this is compared against
    #: timestamps from the app's activity feed, which are absolute.
    worked_wall: float = 0.0

    def clear(self) -> None:
        self.working = False
        self.pending_approval = False
        self.error = False
        self.unread_hint = False


@dataclass
class StateStore:
    """Holds hook overlays plus the latest database snapshot."""

    slot_count: int = 8
    _overlays: dict[str, SessionOverlay] = field(default_factory=dict)
    _sessions: list[PinnedSession] = field(default_factory=list)
    _snapshot_at: float = 0.0
    #: Last time each session was observed running, used to bridge the gaps
    #: between turns. See ``WORKING_HOLD``.
    _last_running_at: dict[str, float] = field(default_factory=dict)
    #: Sessions whose question has already been reported as retired, so the
    #: measurement is logged once rather than on every reconcile tick.
    _retired: set[str] = field(default_factory=set)
    #: Activity counter seen when each question first appeared, keyed by
    #: (session, when it was asked) so a new question is judged on its own.
    _question_activity: dict[tuple, int] = field(default_factory=dict)

    # -- inputs ----------------------------------------------------------

    def overlay_for(self, session_id: str) -> SessionOverlay:
        overlay = self._overlays.get(session_id)
        if overlay is None:
            overlay = SessionOverlay()
            self._overlays[session_id] = overlay
        return overlay

    def apply_hook(self, event_type: str, session_id: str, now: float | None = None) -> None:
        """Fold one hook event into the overlay for its session."""
        if not session_id:
            return
        now = time.monotonic() if now is None else now
        overlay = self.overlay_for(session_id)

        if event_type in WORKING_EVENTS:
            overlay.working = True
            overlay.working_at = now
            # Wall clock too, so this can be compared against the app's
            # activity feed. That is what retires a question: the app writes no
            # new activity item until the whole turn ends, so answering a
            # question leaves agent_asking as the newest item for minutes. A
            # hook is the only prompt evidence that work has resumed.
            overlay.worked_wall = time.time()
            # Fresh activity supersedes a previous failure and clears the
            # optimistic unread we may have set when the last turn ended.
            overlay.error = False
            overlay.unread_hint = False
            # A prompt implies any earlier approval prompt is resolved.
            if event_type == EVENT_USER_PROMPT:
                overlay.pending_approval = False

        elif event_type == EVENT_PERMISSION:
            # This fires before EVERY tool call, not only when a human is
            # asked. Captured payloads confirm there is no field marking one
            # from the other, so whether it means "blocked on you" depends on
            # the session's auto_approve setting -- decided at render time.
            overlay.pending_approval = True
            overlay.pending_approval_at = now
            # A tool is starting, so the session is demonstrably working.
            overlay.working = True
            overlay.working_at = now
            # Deliberately does NOT touch worked_wall.
            #
            # For a session that really asks, this hook *is* the question --
            # it fires as the agent blocks on you, within a second of the app
            # recording agent_asking. Treating it as "work resumed" used the
            # signal that raises the alarm to cancel it, and a question went
            # from orange to blue one second after being asked while still
            # waiting for an answer.
            #
            # Only evidence that cannot coincide with asking may retire a
            # question: a prompt you submitted, or the session doing
            # measurable work afterwards.

        elif event_type == EVENT_AGENT_STOP:
            overlay.working = False
            overlay.working_at = now
            overlay.pending_approval = False
            # Show the "there's something to read" green immediately rather
            # than waiting for the app to write its unread list.
            overlay.unread_hint = True
            overlay.unread_hint_at = now

        elif event_type == EVENT_ERROR:
            overlay.error = True
            overlay.working = False
            overlay.working_at = now

        elif event_type == EVENT_SESSION_START:
            overlay.clear()
            overlay.working_at = now

        elif event_type == EVENT_SESSION_END:
            overlay.clear()
            overlay.working_at = now

    def apply_snapshot(self, sessions: list[PinnedSession], now: float | None = None) -> None:
        """Replace the database view of the pinned slots."""
        self._sessions = list(sessions)[: self.slot_count]
        self._snapshot_at = time.monotonic() if now is None else now

        for session in self._sessions:
            if not session.session_id:
                continue
            if session.is_running:
                self._last_running_at[session.session_id] = self._snapshot_at
            if session.asking:
                # Remember how much work the session had done when the question
                # first appeared, so later movement can retire it. Recorded here
                # rather than at render time so the reading is taken exactly
                # once, when the question arrives.
                self._question_activity.setdefault(
                    (session.session_id, session.asking_at), session.activity
                )
            else:
                self._forget_question(session.session_id)

        # Drop overlays for sessions that are no longer pinned so the dict
        # cannot grow without bound over a long uptime.
        live = {s.session_id for s in self._sessions if s.session_id}
        for session_id in list(self._overlays):
            if session_id not in live:
                del self._overlays[session_id]
        for session_id in list(self._last_running_at):
            if session_id not in live:
                del self._last_running_at[session_id]

    # -- outputs ---------------------------------------------------------

    def session_for_slot(self, slot: int) -> PinnedSession | None:
        if 0 <= slot < len(self._sessions):
            return self._sessions[slot]
        return None

    def resolve(self, session: PinnedSession | None) -> str:
        """Decide the single state string for one slot."""
        if session is None or session.session_id is None:
            return EMPTY

        overlay = self._overlays.get(session.session_id)

        # Approval and error have no database equivalent, so hooks own them
        # outright. Approval outranks error: it is the one state that blocks
        # progress until you act.
        if overlay is not None:
            if overlay.pending_approval and not session.auto_approve:
                # Only sessions that actually ask can be blocked on you. When a
                # session auto-approves, permissionRequest fires on every tool
                # call and means nothing more than "work is happening" --
                # rendering that as "needs approval" made the LED blink orange
                # continuously throughout normal operation.
                return NEEDS_APPROVAL
            if overlay.error:
                return ERROR

        # Asking outranks working, and must: a session sitting on a question
        # still reports is_running, so letting working win meant a slot that was
        # blocked on you showed as busy and you never knew it wanted an answer.
        if self._is_asking(session, overlay):
            return NEEDS_APPROVAL

        # An interrupted session has stopped part-way and will not resume until
        # you nudge it, so it must outrank working too -- a slot frozen
        # mid-task otherwise sits there looking busy indefinitely.
        if session.was_interrupted:
            return INTERRUPTED

        if self._is_working(session, overlay):
            return WORKING

        if session.unread or self._unread_hinted(overlay):
            return UNREAD

        return IDLE

    def _is_asking(self, session: PinnedSession, overlay: SessionOverlay | None) -> bool:
        """Whether a slot is still waiting on you to answer something.

        The database says a question was asked but cannot say it was answered:
        the app writes no new activity item until the whole turn finishes, so
        ``agent_asking`` stays the newest item for as long as the answer takes
        to work through -- measured at over twenty minutes on a long turn. Left
        at that, the slot blinks orange the entire time you are watching it
        work, which is precisely the false alarm this state exists to avoid.

        Two independent pieces of evidence retire it, and both are needed.

        A hook -- specifically a prompt you submitted or a tool result, never
        ``permissionRequest``. That one fires *as* the agent blocks on you, so
        counting it would use the signal that raises the alarm to cancel it.
        Hooks also only fire for sessions started after the hook file was
        installed, so every older session has none, and those are exactly the
        long-lived ones a pad key is likely to be pointed at.

        So also: the session's token totals advancing. Those move while an
        agent is executing, which it cannot be while waiting on you, and they
        move for every session regardless of hooks. Measured at 103s of false
        orange on a hookless session before this was added.
        """
        if not session.asking:
            return False

        if overlay is not None and overlay.worked_wall > session.asking_at:
            self._note_retired(session, "a hook showed work resuming")
            return False

        # Compare against the activity recorded when this question first
        # appeared, which apply_snapshot captures. Keyed on the question
        # itself, so a *new* question from the same session starts a fresh
        # comparison rather than inheriting the last one's answer.
        baseline = self._question_activity.get((session.session_id, session.asking_at))
        if baseline is not None and session.activity != baseline:
            self._note_retired(session, "the session did more work")
            return False
        return True

    def _note_retired(self, session: PinnedSession, why: str) -> None:
        if session.session_id in self._retired:
            return
        self._retired.add(session.session_id)
        log.info(
            "%s: question retired after %.0fs -- %s",
            session.name,
            max(0.0, time.time() - session.asking_at),
            why,
        )

    def _forget_question(self, session_id: str | None) -> None:
        self._retired.discard(session_id)
        for key in [k for k in self._question_activity if k[0] == session_id]:
            del self._question_activity[key]

    def _is_working(self, session: PinnedSession, overlay: SessionOverlay | None) -> bool:
        """Whether a slot should show as working.

        The database is **authoritative** for "still running"; hooks may only
        make us react *faster* to work starting, never contradict the app into
        idle. That asymmetry matters: ``agentStop`` fires at the end of every
        agent turn, but a session mid-task stays ``is_running`` across turns.
        Letting the newer hook win made the LED flap blue/white every few
        seconds throughout a long task -- measured as roughly one flip per turn
        while ``is_running`` never changed once in 45 seconds.

        ``is_running`` itself is not continuous either. A session working
        through a task drops to not-running in the gap between turns, so
        following it literally makes the LED cycle blue/green/white while the
        work is in fact still going. Those gaps are bridged by ``WORKING_HOLD``.
        """
        if session.is_running:
            return True
        if session.session_id:
            last = self._last_running_at.get(session.session_id)
            if last is not None and (self._snapshot_at - last) < WORKING_HOLD:
                return True
        if overlay is None:
            return False
        # A hook saw work start more recently than our last snapshot, so the
        # database simply has not caught up yet.
        return overlay.working and overlay.working_at >= self._snapshot_at

    def _unread_hinted(self, overlay: SessionOverlay | None) -> bool:
        if overlay is None or not overlay.unread_hint:
            return False
        # Once a newer snapshot has been taken and it did not report unread,
        # believe the app: you have evidently already read it.
        return overlay.unread_hint_at >= self._snapshot_at

    def slot_states(self) -> list[str]:
        """State string per slot, padded to ``slot_count`` with ``empty``."""
        states = [self.resolve(self.session_for_slot(i)) for i in range(self.slot_count)]
        return states
