# SPDX-License-Identifier: MIT
"""Read-only access to the Copilot app's local state.

Hard rule: this module **never writes** to ``data.db``. The database is live and
in WAL mode while the app is running, so every connection is opened with
``mode=ro`` and a busy timeout. All mutations in this project go through
surfaces the app actually owns (``ghapp://`` deep links and OS keystrokes),
never through its storage.

Verified layout of the bits we depend on:

``app_state['sidebar-project-groups']``
    JSON ``{"state": {"pinnedWorkspaceIds": [...], "collapsedExplicitGroupIds":
    [...], ...}, "version": N}``. ``pinnedWorkspaceIds`` is an ordered array of
    workspace ids -- slot *i* of the macropad mirrors entry *i*, once entries
    belonging to an explicit group are set aside for that group's own section.
    ``collapsedExplicitGroupIds`` lists the ids of groups the sidebar is not
    currently rendering the members of.

``app_state['session-groups']``
    JSON ``{"state": {"groups": [{"id", "name", "members": [{"kind", "id"},
    ...]}, ...]}}``. Any pinned item that is a member of one of these groups is
    removed from "Pinned" and rendered under the group instead, so the macropad
    mirrors that with one section per group. ``kind`` has only ever been
    observed as ``"workspace"``; a member's ``id`` is trusted regardless.

``app_state['workspace-unread']``
    Bare JSON array of workspace ids with unread agent output.

``workspaces``
    ``id``, ``name``, ``session_id``, ``archived_at``, ``updated_at``,
    ``creator_session_id``, ``coordinating_creator_session_id``.

``workspace_parent_links``
    ``child_workspace_id`` -> ``parent_workspace_id``. A workspace appearing as
    a child here was spawned by another session; those are excluded so the pad
    shows only top-level work. This is not the only way a workspace can be a
    child -- see ``creator_session_id`` below.

    An agent-spawned child can also be linked to its parent purely through
    ``workspaces.creator_session_id`` (or ``coordinating_creator_session_id``)
    equalling the *parent's* session id, with no row in this table at all.
    That id may name another workspace's session, or a session pinned as a
    standalone chat -- either way, the spawned workspace is still a subagent's
    worktree and gets the same treatment.

``sessions``
    ``id``, ``title``, ``is_running``, ``was_interrupted``, ``archived_at``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PINS_KEY = "sidebar-project-groups"
UNREAD_KEY = "workspace-unread"
GROUPS_KEY = "session-groups"

#: Sidebar sort mode that orders by recent activity rather than stored order.
ACTIVITY_SORT = "activity"


@dataclass(frozen=True)
class PinnedSession:
    """One pinned entry, resolved to everything a LED slot needs.

    ``workspace_id`` is ``None`` for a pinned chat session, which has no
    workspace of its own.
    """

    slot: int
    workspace_id: str | None
    session_id: str | None
    name: str
    is_running: bool
    unread: bool
    was_interrupted: bool
    #: Whether this session approves tool use without asking. When it does, a
    #: ``permissionRequest`` hook never blocks on a human, so it must not be
    #: rendered as "needs approval".
    #: Whether this session is waiting on you to answer something. Sourced from
    #: the app's own activity feed rather than from hooks: ``permissionRequest``
    #: fires on every tool call and cannot tell a real question apart from an
    #: auto-approval, whereas the app records ``agent_asking`` only when the
    #: agent actually stops to ask.
    asking: bool = False
    #: When that question was asked, as epoch seconds, so it can be retired
    #: once hooks show the session working again.
    asking_at: float = 0.0
    #: Monotonically-ish increasing counter of work done by this session.
    #:
    #: Token totals advance while an agent is executing, so a change means work
    #: is happening *now*. This is the only such signal available for a session
    #: that fires no hooks, which is every session started before the hook file
    #: was installed.
    activity: int = 0
    auto_approve: bool = True

    @property
    def focusable(self) -> bool:
        return self.session_id is not None


@dataclass(frozen=True)
class Section:
    """One screen's worth of slots: "Pinned", or one explicit sidebar group.

    Mirrors the sidebar exactly -- a pinned item that belongs to a group is
    rendered under that group instead of "Pinned", so the pad's "Pinned"
    section excludes it too rather than showing a key for a row the group
    view has already claimed.
    """

    name: str
    sessions: tuple[PinnedSession, ...]


#: Columns whose sum tracks work in progress. Token totals advance while an
#: agent is executing, which is what lets the pad see that a session resumed
#: without needing a hook from it.
ACTIVITY_COLUMNS = (
    "total_input_tokens",
    "total_output_tokens",
    "context_current_tokens",
)

#: SQL expression summing those, tolerating the NULLs a fresh session has.
ACTIVITY_SUM = " + ".join(f"COALESCE(s.{c}, 0)" for c in ACTIVITY_COLUMNS)
ACTIVITY_SUM_BARE = " + ".join(f"COALESCE({c}, 0)" for c in ACTIVITY_COLUMNS)


class CopilotDB:
    """Read-only reader for the Copilot app database."""

    def __init__(self, db_path: Path, busy_timeout_ms: int = 2000) -> None:
        self.db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    @classmethod
    def default(cls, copilot_home: Path | None = None) -> "CopilotDB":
        home = Path(copilot_home) if copilot_home else Path.home() / ".copilot"
        return cls(home / "data.db")

    def _connect(self) -> sqlite3.Connection:
        # as_uri() handles Windows drive letters and spaces correctly.
        uri = f"{self.db_path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self._busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn

    def available(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM app_state LIMIT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # -- raw app_state helpers ------------------------------------------

    @staticmethod
    def _load_json(raw: str | None):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _app_state(self, conn: sqlite3.Connection, key: str):
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return self._load_json(row["value"]) if row else None

    def pinned_workspace_ids(self, conn: sqlite3.Connection) -> list[str]:
        blob = self._app_state(conn, PINS_KEY) or {}
        pinned = (blob.get("state") or {}).get("pinnedWorkspaceIds") or []
        return [w for w in pinned if isinstance(w, str)]

    def collapsed_group_ids(self, conn: sqlite3.Connection) -> set[str]:
        blob = self._app_state(conn, PINS_KEY) or {}
        ids = (blob.get("state") or {}).get("collapsedExplicitGroupIds") or []
        return {i for i in ids if isinstance(i, str)}

    def groups(self, conn: sqlite3.Connection) -> list[dict]:
        """Explicit sidebar groups, in stored order.

        Each entry is ``{"id", "name", "members"}``, ``members`` being the
        ordered list of member ids. A member's ``kind`` has only ever been
        observed as ``"workspace"``; it is not checked, since a group is just
        another ordered list of slot candidates regardless of what its member
        turns out to resolve to.
        """
        blob = self._app_state(conn, GROUPS_KEY) or {}
        raw_groups = (blob.get("state") or {}).get("groups") or []
        result = []
        for g in raw_groups:
            if not isinstance(g, dict) or not g.get("id"):
                continue
            members = [
                m.get("id")
                for m in (g.get("members") or [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
            result.append(
                {"id": g["id"], "name": str(g.get("name") or "(group)"), "members": members}
            )
        return result

    def grouped_member_ids(self, conn: sqlite3.Connection) -> set[str]:
        """Every id that belongs to some explicit group.

        A pinned item in this set is rendered under its group in the sidebar,
        not under "Pinned" -- so the Pinned section must exclude it too, or a
        key would silently duplicate a row the group section already shows.
        """
        return {m for g in self.groups(conn) for m in g["members"]}

    def collapsed_group_for(self, workspace_id: str) -> str | None:
        """Display name of the collapsed group containing ``workspace_id``.

        None both when the workspace is in no group and when its group is
        currently expanded -- either way there is nothing to expand before
        looking for its row. The *name* is what identifies a group's header in
        the accessibility tree: group headers carry no automation id, only a
        ``ButtonControl.Name`` set to the group's display name.
        """
        if not workspace_id:
            return None
        try:
            with self._connect() as conn:
                collapsed = self.collapsed_group_ids(conn)
                for g in self.groups(conn):
                    if workspace_id in g["members"]:
                        return g["name"] if g["id"] in collapsed else None
        except sqlite3.Error:
            # Same tolerance as focused_ids(): this runs on the navigation
            # path, where a transient DB hiccup must fall through to a plain
            # (non-collapsed) lookup rather than take down the click.
            return None
        return None

    def focused_ids(self) -> set[str]:
        """Identifiers of whatever the app currently has open.

        Used to tell when a deep-link navigation has actually landed. Both the
        active workspace and the raw route are included because a pin may be a
        workspace id *or* a session id -- chat sessions have no workspace, so
        matching on workspace alone would never resolve for them.
        """
        found: set[str] = set()
        try:
            with self._connect() as conn:
                for key in ("activeWorkspaceId", "lastRoutePath"):
                    row = conn.execute(
                        "SELECT value FROM app_state WHERE key = ?", (key,)
                    ).fetchone()
                    if not row or not row["value"]:
                        continue
                    raw = str(row["value"]).strip().strip('"')
                    found.add(raw)
                    # The route is a path; its last segment is the id.
                    if "/" in raw:
                        found.add(raw.rsplit("/", 1)[-1])
        except sqlite3.Error:
            return found
        return found

    def sort_mode(self, conn: sqlite3.Connection) -> str:
        """The app's own workspace sort mode, e.g. ``activity``.

        Not applied to pinned slots: pins are drag-ordered, so their stored
        order is the order you see. Exposed for diagnostics only.
        """
        blob = self._app_state(conn, PINS_KEY) or {}
        return str((blob.get("state") or {}).get("workspaceSortMode") or "")

    def child_workspace_ids(
        self, conn: sqlite3.Connection, pinned_chat_session_ids: Iterable[str] = ()
    ) -> set[str]:
        """Workspaces spawned by another session.

        These are the app's child sessions; they are excluded so a key always
        addresses a top-level piece of work rather than a subagent's worktree.
        Their *activity* is still rolled up into the parent -- see
        :meth:`descendants_of`.

        Two independent mechanisms name a parent, and a workspace counts as a
        child if either says so. ``workspace_parent_links`` is the older,
        explicit table. ``creator_session_id`` (or
        ``coordinating_creator_session_id``) links purely through session ids,
        with no linking row at all -- so a workspace whose creator is another
        workspace's session, or a session pinned as a standalone chat, is a
        child too even though nothing here ever recorded it as one.
        """
        ids: set[str] = set()
        try:
            rows = conn.execute(
                "SELECT child_workspace_id FROM workspace_parent_links"
            )
            ids |= {row[0] for row in rows if row[0]}
        except sqlite3.Error:
            # Older schema without parent links: nothing is a child there.
            pass
        ids |= self._creator_session_children(conn, pinned_chat_session_ids)
        return ids

    @staticmethod
    def _creator_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        try:
            return conn.execute(
                "SELECT id, session_id, creator_session_id,"
                " coordinating_creator_session_id FROM workspaces"
            ).fetchall()
        except sqlite3.Error:
            # Older schema predating these columns: nothing is a child there.
            return []

    def _creator_session_children(
        self, conn: sqlite3.Connection, pinned_chat_session_ids: Iterable[str] = ()
    ) -> set[str]:
        """Workspaces linked to a parent purely by session id.

        A workspace's creator session belongs to a "parent" of one of two
        shapes: another workspace (found by matching every workspace's own
        ``session_id``, not only pinned ones -- an agent-spawned child is
        rarely itself pinned), or a session pinned as a standalone chat, which
        has no workspace of its own to match against.
        """
        rows = self._creator_rows(conn)
        parent_session_ids = {r["session_id"] for r in rows if r["session_id"]}
        parent_session_ids |= {s for s in pinned_chat_session_ids if s}
        children: set[str] = set()
        for r in rows:
            for creator in (r["creator_session_id"], r["coordinating_creator_session_id"]):
                if creator and creator != r["session_id"] and creator in parent_session_ids:
                    children.add(r["id"])
                    break
        return children

    def _child_map(
        self, conn: sqlite3.Connection, pinned_chat_session_ids: Iterable[str] = ()
    ) -> dict[str, list[str]]:
        """parent id -> direct children, keyed by whatever a pin for that
        parent would actually be: a workspace id for a workspace parent, or
        the session id itself for a chat-session parent.
        """
        mapping: dict[str, list[str]] = {}
        try:
            rows = conn.execute(
                "SELECT parent_workspace_id, child_workspace_id"
                " FROM workspace_parent_links"
            )
            for parent, child in rows:
                if parent and child:
                    mapping.setdefault(parent, []).append(child)
        except sqlite3.Error:
            pass

        creator_rows = self._creator_rows(conn)
        session_to_workspace = {
            r["session_id"]: r["id"] for r in creator_rows if r["session_id"]
        }
        chat_parents = {s for s in pinned_chat_session_ids if s}
        for r in creator_rows:
            for creator in (r["creator_session_id"], r["coordinating_creator_session_id"]):
                if not creator:
                    continue
                parent_ws = session_to_workspace.get(creator)
                if parent_ws and parent_ws != r["id"]:
                    bucket = mapping.setdefault(parent_ws, [])
                    if r["id"] not in bucket:
                        bucket.append(r["id"])
                    break
                if creator in chat_parents:
                    bucket = mapping.setdefault(creator, [])
                    if r["id"] not in bucket:
                        bucket.append(r["id"])
                    break
        return mapping

    @classmethod
    def descendants_of(
        cls, child_map: dict[str, list[str]], root: str
    ) -> set[str]:
        """Every workspace beneath ``root``, at any depth.

        Guards against cycles, which a corrupted or hand-edited link table
        could otherwise turn into an infinite walk.
        """
        found: set[str] = set()
        frontier = [root]
        while frontier:
            nxt: list[str] = []
            for node in frontier:
                for child in child_map.get(node, ()):
                    if child not in found and child != root:
                        found.add(child)
                        nxt.append(child)
            frontier = nxt
        return found

    def unread_workspace_ids(self, conn: sqlite3.Connection) -> set[str]:
        blob = self._app_state(conn, UNREAD_KEY)
        if isinstance(blob, list):
            return {w for w in blob if isinstance(w, str)}
        # Tolerate a zustand-style wrapper if the shape ever changes.
        if isinstance(blob, dict):
            values = (blob.get("state") or {}).get("ids") or []
            return {w for w in values if isinstance(w, str)}
        return set()

    def _pinned_chat_session_ids(self, conn: sqlite3.Connection) -> set[str]:
        """Pinned ids that are chat sessions rather than workspaces.

        Computed against the *whole* pin list regardless of grouping or
        section, since a session pinned as a standalone chat is a candidate
        "parent" for creator-session child detection no matter which section
        (or none) it ends up rendered under.
        """
        order = self.pinned_workspace_ids(conn)
        if not order:
            return set()
        rows = self._fetch_workspaces(conn, order)
        return {pin for pin in order if pin not in rows}

    # -- the query the daemon actually uses ------------------------------

    def _resolve_ordered(
        self,
        conn: sqlite3.Connection,
        order: list[str],
        limit: int,
        *,
        unread: set[str],
        children: set[str],
        child_map: dict[str, list[str]],
        asking_sessions: dict[str, float],
    ) -> list[PinnedSession]:
        """Resolve an ordered id list (a pin list, or a group's members) to
        the first ``limit`` sessions, applying the shared archived/child/
        rollup rules. Shared by :meth:`pinned_sessions` and :meth:`sections`
        so a group's slots behave exactly like the Pinned section's.
        """
        if not order:
            return []
        rows = self._fetch_workspaces(conn, order)
        # Anything not matching a workspace may still be a pinned chat
        # session, which has no workspace of its own.
        unmatched = [pin for pin in order if pin not in rows]
        session_rows = self._fetch_sessions(conn, unmatched)

        # A parent whose children are working IS working, so roll their
        # activity up. Without this a session that has delegated all its
        # work looks idle while its subagents run.
        descendants = {
            pin: self.descendants_of(child_map, pin)
            for pin in order
            if pin not in children
        }
        every_descendant = set()
        for ids in descendants.values():
            every_descendant |= ids
        descendant_rows = self._fetch_workspaces(conn, every_descendant)

        def rolled_up(pin: str, own_running: bool, own_unread: bool):
            """Fold descendant state into a pinned slot.

            Only states that clear themselves are rolled up. Unread is
            deliberately **not**: the app does not mark a parent unread when a
            child has output, and overriding it produced a green light nothing
            could turn off -- one pin had 51 unread descendants, so clearing it
            by hand meant opening 51 child sessions. Work and questions are
            different: work stops on its own, and a question is answered from
            the parent.
            """
            running, has_unread = own_running, own_unread
            asking_at = 0.0
            interrupted = False
            for child in descendants.get(pin, ()):
                row = descendant_rows.get(child)
                if row is None or row["workspace_archived_at"] is not None:
                    continue
                if row["is_running"]:
                    running = True
                asking_at = max(asking_at, asking_sessions.get(row["session_id"], 0.0))
                if row["was_interrupted"]:
                    interrupted = True
            return running, has_unread, asking_at, interrupted

        resolved: list[PinnedSession] = []
        for pin in order:
            if len(resolved) >= limit:
                break
            if pin in children:
                continue

            row = rows.get(pin)
            if row is not None:
                if row["workspace_archived_at"] is not None:
                    continue
                running, has_unread, child_asking_at, child_interrupted = rolled_up(
                    pin, bool(row["is_running"]), pin in unread
                )
                asking_at = max(
                    asking_sessions.get(row["session_id"], 0.0), child_asking_at
                )
                resolved.append(
                    PinnedSession(
                        slot=len(resolved),
                        workspace_id=pin,
                        session_id=row["session_id"],
                        name=row["workspace_name"] or row["session_title"] or "(untitled)",
                        is_running=running,
                        unread=has_unread,
                        was_interrupted=bool(row["was_interrupted"]) or child_interrupted,
                        asking=asking_at > 0.0,
                        asking_at=asking_at,
                        activity=int(row["activity"] or 0),
                        auto_approve=bool(row["auto_approve"]),
                    )
                )
                continue

            session = session_rows.get(pin)
            if session is None or session["archived_at"] is not None:
                continue
            # A pinned chat session can be a parent too, via creator_session_id
            # naming its own session id -- a workspace_parent_links parent was
            # always a workspace, but this new linkage has no such limit.
            running, has_unread, child_asking_at, child_interrupted = rolled_up(
                pin, bool(session["is_running"]), pin in unread
            )
            asking_at = max(asking_sessions.get(pin, 0.0), child_asking_at)
            resolved.append(
                PinnedSession(
                    slot=len(resolved),
                    workspace_id=None,
                    session_id=pin,
                    name=session["title"] or "(untitled)",
                    is_running=running,
                    unread=has_unread,
                    was_interrupted=bool(session["was_interrupted"]) or child_interrupted,
                    asking=asking_at > 0.0,
                    asking_at=asking_at,
                    activity=int(session["activity"] or 0),
                    auto_approve=bool(session["auto_approve"]),
                )
            )
        return resolved

    def _section_inputs(self, conn: sqlite3.Connection):
        """The per-connection resources every section resolves against."""
        unread = self.unread_workspace_ids(conn)
        pinned_chat_ids = self._pinned_chat_session_ids(conn)
        children = self.child_workspace_ids(conn, pinned_chat_ids)
        child_map = self._child_map(conn, pinned_chat_ids)
        asking_sessions = self._asking_session_ids(conn)
        return unread, children, child_map, asking_sessions

    def pinned_sessions(self, limit: int) -> list[PinnedSession]:
        """Resolve the first ``limit`` pinned slots, in the order you dragged them.

        ``pinnedWorkspaceIds`` is a manually-ordered list, so it is used as-is
        rather than re-sorted -- key N must be the Nth pin you see.

        Three subtleties, all learned from real data:

        * **A pin is not always a workspace id.** Chat sessions have no
          workspace, so their *session* id appears in the same list. Dropping
          them shifts every later key up by one and silently mis-addresses
          sessions.
        * **Archived pins and child sessions are skipped.** An archived pin is
          gone from the sidebar, and a child workspace is a subagent's worktree
          rather than something you drive from the pad.
        * **A pin that belongs to an explicit group is skipped here too.** The
          sidebar renders it under the group instead of "Pinned", so this is
          just the "Pinned" section -- see :meth:`sections` for the rest.
        """
        with self._connect() as conn:
            order = self.pinned_workspace_ids(conn)
            if not order:
                return []
            grouped = self.grouped_member_ids(conn)
            order = [w for w in order if w not in grouped]
            unread, children, child_map, asking_sessions = self._section_inputs(conn)
            return self._resolve_ordered(
                conn,
                order,
                limit,
                unread=unread,
                children=children,
                child_map=child_map,
                asking_sessions=asking_sessions,
            )

    def sections(self, limit: int) -> list[Section]:
        """Every section the sidebar shows: "Pinned", then one per group.

        Mirrors :meth:`pinned_sessions` for section 0, then resolves each
        explicit group's members the same way. A group with no eligible
        members (all archived, or all children) is left out entirely, since
        there is nothing on a key for it to show.
        """
        with self._connect() as conn:
            pinned_order = self.pinned_workspace_ids(conn)
            grouped = self.grouped_member_ids(conn)
            unread, children, child_map, asking_sessions = self._section_inputs(conn)

            pinned_only = [w for w in pinned_order if w not in grouped]
            result = [
                Section(
                    name="Pinned",
                    sessions=tuple(
                        self._resolve_ordered(
                            conn,
                            pinned_only,
                            limit,
                            unread=unread,
                            children=children,
                            child_map=child_map,
                            asking_sessions=asking_sessions,
                        )
                    ),
                )
            ]
            for g in self.groups(conn):
                resolved = self._resolve_ordered(
                    conn,
                    g["members"],
                    limit,
                    unread=unread,
                    children=children,
                    child_map=child_map,
                    asking_sessions=asking_sessions,
                )
                if resolved:
                    result.append(Section(name=g["name"], sessions=tuple(resolved)))
            return result

    #: Activity types that mean "this session has stopped and wants you".
    ASKING_ACTIVITY = ("agent_asking", "agent_plan_ready")

    @classmethod
    def _asking_session_ids(cls, conn: sqlite3.Connection) -> dict[str, float]:
        """Sessions whose most recent activity is a question, and when.

        Only the *latest* item counts. ``is_read`` is not usable here -- the app
        leaves it 0 on essentially every row, so filtering on it would mark
        every question ever asked as still outstanding.

        The timestamp matters as much as the flag. The app writes no new
        activity item until a whole turn finishes, so answering a question
        leaves ``agent_asking`` as the newest item for as long as the answer
        takes to work through. Callers retire the question by comparing this
        against live hook activity.

        A missing table is tolerated: the feed is a newer addition to the app,
        and an older database must still light the pad rather than fail.
        """
        try:
            rows = conn.execute(
                """
                SELECT a.session_id AS session_id,
                       a.activity_type AS activity_type,
                       a.created_at AS created_at
                FROM activity_items AS a
                JOIN (
                    SELECT session_id, MAX(created_at) AS newest
                    FROM activity_items
                    GROUP BY session_id
                ) AS latest
                  ON latest.session_id = a.session_id
                 AND latest.newest = a.created_at
                """
            ).fetchall()
        except sqlite3.Error:
            return {}
        asking: dict[str, float] = {}
        for row in rows:
            if not row["session_id"] or row["activity_type"] not in cls.ASKING_ACTIVITY:
                continue
            asking[row["session_id"]] = cls._as_epoch(row["created_at"])
        return asking

    @staticmethod
    def _as_epoch(raw: str | None) -> float:
        """Parse the app's ISO-8601 UTC timestamps into epoch seconds."""
        if not raw:
            return 0.0
        try:
            text = str(raw).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            # The app writes UTC. Letting Python assume local time here would
            # shift the timestamp by the offset and either retire a live
            # question early or leave a stale one blinking for hours.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _fetch_sessions(
        conn: sqlite3.Connection, session_ids: Iterable[str]
    ) -> dict[str, sqlite3.Row]:
        ids = list(session_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        sql = f"""
            SELECT id, title, is_running, was_interrupted, archived_at, auto_approve,
                   {ACTIVITY_SUM_BARE} AS activity
            FROM sessions
            WHERE id IN ({placeholders})
        """
        return {row["id"]: row for row in conn.execute(sql, ids)}

    @staticmethod
    def _fetch_workspaces(
        conn: sqlite3.Connection, workspace_ids: Iterable[str]
    ) -> dict[str, sqlite3.Row]:
        ids = list(workspace_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        sql = f"""
            SELECT
                w.id                AS workspace_id,
                w.name              AS workspace_name,
                w.session_id        AS session_id,
                w.archived_at       AS workspace_archived_at,
                w.updated_at        AS updated_at,
                s.title             AS session_title,
                s.is_running        AS is_running,
                s.was_interrupted   AS was_interrupted,
                s.auto_approve      AS auto_approve,
                {ACTIVITY_SUM}      AS activity,
                s.archived_at       AS session_archived_at
            FROM workspaces w
            LEFT JOIN sessions s ON s.id = w.session_id
            WHERE w.id IN ({placeholders})
        """
        return {row["workspace_id"]: row for row in conn.execute(sql, ids)}
