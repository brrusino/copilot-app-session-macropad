# SPDX-License-Identifier: MIT
"""Tests for the read-only Copilot database reader, against a fixture database."""

import json
import sqlite3

import pytest

from macropad_daemon.copilot_db import PINS_KEY, UNREAD_KEY, CopilotDB

SCHEMA = """
CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    is_running INTEGER DEFAULT 0,
    was_interrupted INTEGER DEFAULT 0,
    archived_at TEXT
);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    name TEXT,
    session_id TEXT,
    archived_at TEXT
);
"""


def build_db(tmp_path, pins, workspaces, sessions, unread=()):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (PINS_KEY, json.dumps({"state": {"pinnedWorkspaceIds": pins}, "version": 1})),
    )
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (UNREAD_KEY, json.dumps(list(unread))),
    )
    conn.executemany(
        "INSERT INTO workspaces (id, name, session_id, archived_at) VALUES (?,?,?,?)",
        workspaces,
    )
    conn.executemany(
        "INSERT INTO sessions (id, title, is_running, was_interrupted, archived_at)"
        " VALUES (?,?,?,?,?)",
        sessions,
    )
    conn.commit()
    conn.close()
    return CopilotDB(path)


@pytest.fixture
def db(tmp_path):
    return build_db(
        tmp_path,
        pins=["ws-1", "ws-2", "ws-3"],
        workspaces=[
            ("ws-1", "First", "s-1", None),
            ("ws-2", "Second", "s-2", None),
            ("ws-3", "Third", "s-3", None),
        ],
        sessions=[
            ("s-1", "First session", 0, 0, None),
            ("s-2", "Second session", 1, 0, None),
            ("s-3", "Third session", 0, 0, None),
        ],
        unread=["ws-3"],
    )


def test_available(db):
    assert db.available() is True


def test_missing_database_is_unavailable(tmp_path):
    assert CopilotDB(tmp_path / "nope.db").available() is False


def test_slots_follow_pin_order(db):
    rows = db.pinned_sessions(8)
    assert [r.workspace_id for r in rows] == ["ws-1", "ws-2", "ws-3"]
    assert [r.slot for r in rows] == [0, 1, 2]


def test_running_and_unread_flags(db):
    rows = {r.workspace_id: r for r in db.pinned_sessions(8)}
    assert rows["ws-2"].is_running is True
    assert rows["ws-1"].is_running is False
    assert rows["ws-3"].unread is True
    assert rows["ws-1"].unread is False


def test_limit_truncates(db):
    assert len(db.pinned_sessions(2)) == 2


def test_archived_workspace_is_skipped_not_slotted(tmp_path):
    """An archived pin must not occupy a dead LED slot."""
    db = build_db(
        tmp_path,
        pins=["ws-1", "ws-2", "ws-3"],
        workspaces=[
            ("ws-1", "First", "s-1", None),
            ("ws-2", "Archived", "s-2", "2026-01-01T00:00:00Z"),
            ("ws-3", "Third", "s-3", None),
        ],
        sessions=[
            ("s-1", "a", 0, 0, None),
            ("s-2", "b", 0, 0, None),
            ("s-3", "c", 0, 0, None),
        ],
    )
    rows = db.pinned_sessions(8)
    assert [r.workspace_id for r in rows] == ["ws-1", "ws-3"]
    # Slots renumber so they stay contiguous.
    assert [r.slot for r in rows] == [0, 1]


def test_pin_referencing_missing_workspace_is_skipped(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ws-gone", "ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[("s-1", "a", 0, 0, None)],
    )
    rows = db.pinned_sessions(8)
    assert [r.workspace_id for r in rows] == ["ws-1"]


def test_workspace_without_session_is_not_focusable(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[("ws-1", "First", None, None)],
        sessions=[],
    )
    row = db.pinned_sessions(8)[0]
    assert row.session_id is None
    assert row.focusable is False


def test_falls_back_to_session_title_when_workspace_unnamed(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[("ws-1", None, "s-1", None)],
        sessions=[("s-1", "Session title", 0, 0, None)],
    )
    assert db.pinned_sessions(8)[0].name == "Session title"


def test_no_pins_returns_nothing(tmp_path):
    db = build_db(tmp_path, pins=[], workspaces=[], sessions=[])
    assert db.pinned_sessions(8) == []


def test_malformed_app_state_json_is_tolerated(tmp_path):
    db = build_db(tmp_path, pins=["ws-1"], workspaces=[("ws-1", "a", "s-1", None)],
                  sessions=[("s-1", "a", 0, 0, None)])
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE app_state SET value = ? WHERE key = ?", ("{not json", PINS_KEY))
    conn.commit()
    conn.close()
    assert db.pinned_sessions(8) == []


def test_connection_is_read_only(db):
    """The app's live database must never be writable through this class."""
    conn = db._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO app_state (key, value) VALUES ('x','y')")
    conn.close()
