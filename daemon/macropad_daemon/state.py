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
When the two disagree about whether a session is *working*, the more recent
observation wins: a hook event with a timestamp newer than the last database
snapshot overrides that snapshot, and vice versa. This is what stops a finished
agent from staying blue until the next poll, and stops a stale hook from pinning
a slot to "working" forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .copilot_db import PinnedSession

# These strings are the wire contract with the firmware; they must stay in sync
# with the keys of ``PALETTE`` in keybow/config.py.
EMPTY = "empty"
IDLE = "idle"
WORKING = "working"
UNREAD = "unread"
NEEDS_APPROVAL = "needs_approval"
ERROR = "error"

ALL_STATES = (EMPTY, IDLE, WORKING, UNREAD, NEEDS_APPROVAL, ERROR)

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
    error: bool = False
    #: Set on agentStop so the slot can go green before the app records unread.
    unread_hint: bool = False
    unread_hint_at: float = 0.0

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
            # Fresh activity supersedes a previous failure and clears the
            # optimistic unread we may have set when the last turn ended.
            overlay.error = False
            overlay.unread_hint = False
            # A prompt implies any earlier approval prompt is resolved.
            if event_type == EVENT_USER_PROMPT:
                overlay.pending_approval = False

        elif event_type == EVENT_PERMISSION:
            overlay.pending_approval = True

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

        # Drop overlays for sessions that are no longer pinned so the dict
        # cannot grow without bound over a long uptime.
        live = {s.session_id for s in self._sessions if s.session_id}
        for session_id in list(self._overlays):
            if session_id not in live:
                del self._overlays[session_id]

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
            if overlay.pending_approval:
                return NEEDS_APPROVAL
            if overlay.error:
                return ERROR

        if self._is_working(session, overlay):
            return WORKING

        if session.unread or self._unread_hinted(overlay):
            return UNREAD

        return IDLE

    def _is_working(self, session: PinnedSession, overlay: SessionOverlay | None) -> bool:
        if overlay is None or overlay.working_at == 0.0:
            return session.is_running
        # More recent observation wins.
        if overlay.working_at >= self._snapshot_at:
            return overlay.working
        return session.is_running

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

    def next_attention_slot(self, after: int | None = None) -> int | None:
        """First slot wanting attention, in priority order, cycling from ``after``.

        Priority mirrors urgency: an approval prompt blocks the agent, an error
        needs diagnosis, unread output merely wants reading.
        """
        states = self.slot_states()
        order = (NEEDS_APPROVAL, ERROR, UNREAD)
        start = 0 if after is None else (after + 1) % self.slot_count
        rotation = [(start + i) % self.slot_count for i in range(self.slot_count)]

        for wanted in order:
            for slot in rotation:
                if states[slot] == wanted:
                    return slot
        return None
