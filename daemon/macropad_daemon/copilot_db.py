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
    ``id``, ``name``, ``session_id``, ``archived_at``.

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


@dataclass(frozen=True)
class PinnedSession:
    """One pinned workspace, resolved to everything a LED slot needs."""

    slot: int
    workspace_id: str
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
        """Resolve the first ``limit`` non-archived pins into LED slots.

        Archived workspaces are skipped rather than occupying a dead slot, so
        the pad always shows ``limit`` actionable sessions where they exist.
        """
        with self._connect() as conn:
            order = self.pinned_workspace_ids(conn)
            if not order:
                return []
            unread = self.unread_workspace_ids(conn)
            rows = self._fetch_workspaces(conn, order)

        resolved: list[PinnedSession] = []
        for workspace_id in order:
            if len(resolved) >= limit:
                break
            row = rows.get(workspace_id)
            if row is None or row["workspace_archived_at"] is not None:
                continue
            resolved.append(
                PinnedSession(
                    slot=len(resolved),
                    workspace_id=workspace_id,
                    session_id=row["session_id"],
                    name=row["workspace_name"] or row["session_title"] or "(untitled)",
                    is_running=bool(row["is_running"]),
                    unread=workspace_id in unread,
                    was_interrupted=bool(row["was_interrupted"]),
                )
            )
        return resolved

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
                s.title             AS session_title,
                s.is_running        AS is_running,
                s.was_interrupted   AS was_interrupted,
                s.archived_at       AS session_archived_at
            FROM workspaces w
            LEFT JOIN sessions s ON s.id = w.session_id
            WHERE w.id IN ({placeholders})
        """
        return {row["workspace_id"]: row for row in conn.execute(sql, ids)}
