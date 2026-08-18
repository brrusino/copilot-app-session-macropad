# SPDX-License-Identifier: MIT
"""Read-only access to the Copilot app's local state.

Hard rule: this module **never writes** to ``data.db``. The database is live and
in WAL mode while the app is running, so every connection is opened with
``mode=ro`` and a busy timeout. All mutations in this project go through
surfaces the app actually owns (``ghapp://`` deep links and OS keystrokes),
never through its storage.

Verified layout of the bits we depend on:

``app_state['sidebar-project-groups']``
    JSON ``{"state": {"pinnedWorkspaceIds": [...], ...}, "version": N}``.
    ``pinnedWorkspaceIds`` is an ordered array of workspace ids -- slot *i* of
    the macropad mirrors entry *i*.

``app_state['workspace-unread']``
    Bare JSON array of workspace ids with unread agent output.

``workspaces``
    ``id``, ``name``, ``session_id``, ``archived_at``, ``updated_at``.

``workspace_parent_links``
    ``child_workspace_id`` -> ``parent_workspace_id``. A workspace appearing as
    a child here was spawned by another session; those are excluded so the pad
    shows only top-level work.

``sessions``
    ``id``, ``title``, ``is_running``, ``was_interrupted``, ``archived_at``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PINS_KEY = "sidebar-project-groups"
UNREAD_KEY = "workspace-unread"

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

    @property
    def focusable(self) -> bool:
        return self.session_id is not None


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

    def sort_mode(self, conn: sqlite3.Connection) -> str:
        """The app's own workspace sort mode, e.g. ``activity``.

        Not applied to pinned slots: pins are drag-ordered, so their stored
        order is the order you see. Exposed for diagnostics only.
        """
        blob = self._app_state(conn, PINS_KEY) or {}
        return str((blob.get("state") or {}).get("workspaceSortMode") or "")

    @staticmethod
    def child_workspace_ids(conn: sqlite3.Connection) -> set[str]:
        """Workspaces spawned by another session.

        These are the app's child sessions; they are excluded so a key always
        addresses a top-level piece of work rather than a subagent's worktree.
        Their *activity* is still rolled up into the parent -- see
        :meth:`descendants_of`.
        """
        try:
            rows = conn.execute(
                "SELECT child_workspace_id FROM workspace_parent_links"
            )
        except sqlite3.Error:
            # Older schema without parent links: nothing is a child.
            return set()
        return {row[0] for row in rows if row[0]}

    @staticmethod
    def _child_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
        """parent workspace id -> direct children."""
        mapping: dict[str, list[str]] = {}
        try:
            rows = conn.execute(
                "SELECT parent_workspace_id, child_workspace_id"
                " FROM workspace_parent_links"
            )
        except sqlite3.Error:
            return mapping
        for parent, child in rows:
            if parent and child:
                mapping.setdefault(parent, []).append(child)
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

    # -- the query the daemon actually uses ------------------------------

    def pinned_sessions(self, limit: int) -> list[PinnedSession]:
        """Resolve the first ``limit`` pinned slots, in the order you dragged them.

        ``pinnedWorkspaceIds`` is a manually-ordered list, so it is used as-is
        rather than re-sorted -- key N must be the Nth pin you see.

        Two subtleties, both learned from real data:

        * **A pin is not always a workspace id.** Chat sessions have no
          workspace, so their *session* id appears in the same list. Dropping
          them shifts every later key up by one and silently mis-addresses
          sessions.
        * **Archived pins and child sessions are skipped.** An archived pin is
          gone from the sidebar, and a child workspace is a subagent's worktree
          rather than something you drive from the pad.
        """
        with self._connect() as conn:
            order = self.pinned_workspace_ids(conn)
            if not order:
                return []
            unread = self.unread_workspace_ids(conn)
            children = self.child_workspace_ids(conn)
            child_map = self._child_map(conn)
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
            running, has_unread = own_running, own_unread
            for child in descendants.get(pin, ()):
                row = descendant_rows.get(child)
                if row is None or row["workspace_archived_at"] is not None:
                    continue
                if row["is_running"]:
                    running = True
                if child in unread:
                    has_unread = True
            return running, has_unread

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
                running, has_unread = rolled_up(
                    pin, bool(row["is_running"]), pin in unread
                )
                resolved.append(
                    PinnedSession(
                        slot=len(resolved),
                        workspace_id=pin,
                        session_id=row["session_id"],
                        name=row["workspace_name"] or row["session_title"] or "(untitled)",
                        is_running=running,
                        unread=has_unread,
                        was_interrupted=bool(row["was_interrupted"]),
                    )
                )
                continue

            session = session_rows.get(pin)
            if session is None or session["archived_at"] is not None:
                continue
            resolved.append(
                PinnedSession(
                    slot=len(resolved),
                    workspace_id=None,
                    session_id=pin,
                    name=session["title"] or "(untitled)",
                    is_running=bool(session["is_running"]),
                    unread=pin in unread,
                    was_interrupted=bool(session["was_interrupted"]),
                )
            )
        return resolved

    @staticmethod
    def _fetch_sessions(
        conn: sqlite3.Connection, session_ids: Iterable[str]
    ) -> dict[str, sqlite3.Row]:
        ids = list(session_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        sql = f"""
            SELECT id, title, is_running, was_interrupted, archived_at
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
                s.archived_at       AS session_archived_at
            FROM workspaces w
            LEFT JOIN sessions s ON s.id = w.session_id
            WHERE w.id IN ({placeholders})
        """
        return {row["workspace_id"]: row for row in conn.execute(sql, ids)}
