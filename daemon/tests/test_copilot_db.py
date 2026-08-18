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
    archived_at TEXT,
    auto_approve INTEGER DEFAULT 0
);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    name TEXT,
    session_id TEXT,
    archived_at TEXT,
    updated_at TEXT
);
CREATE TABLE workspace_parent_links (
    child_workspace_id TEXT,
    parent_workspace_id TEXT,
    creator_session_id TEXT,
    created_at TEXT
);
CREATE TABLE activity_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    session_id TEXT,
    activity_type TEXT,
    preview TEXT,
    is_read INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    metadata_json TEXT
);
"""


def build_db(
    tmp_path,
    pins,
    workspaces,
    sessions,
    unread=(),
    children=(),
    sort_mode=None,
    activity=(),
):
    """Build a fixture database.

    ``workspaces`` rows are ``(id, name, session_id, archived_at)`` with an
    optional 5th ``updated_at`` element. ``activity`` rows are
    ``(session_id, activity_type, created_at)``.
    """
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    state = {"pinnedWorkspaceIds": pins}
    if sort_mode is not None:
        state["workspaceSortMode"] = sort_mode
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (PINS_KEY, json.dumps({"state": state, "version": 1})),
    )
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (UNREAD_KEY, json.dumps(list(unread))),
    )
    padded = [row if len(row) == 5 else (*row, None) for row in workspaces]
    conn.executemany(
        "INSERT INTO workspaces (id, name, session_id, archived_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        padded,
    )
    conn.executemany(
        "INSERT INTO sessions (id, title, is_running, was_interrupted, archived_at)"
        " VALUES (?,?,?,?,?)",
        sessions,
    )
    conn.executemany(
        "INSERT INTO workspace_parent_links"
        " (child_workspace_id, parent_workspace_id, creator_session_id, created_at)"
        " VALUES (?,?,?,?)",
        [(c, "parent-ws", "parent-sess", "2026-01-01") for c in children],
    )
    conn.executemany(
        "INSERT INTO activity_items (id, session_id, activity_type, created_at)"
        " VALUES (?,?,?,?)",
        [(f"a-{i}", s, t, ts) for i, (s, t, ts) in enumerate(activity)],
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


# --- child sessions -------------------------------------------------------
# A workspace spawned by another session is a subagent's worktree, not
# something to drive from the pad.


def test_child_workspaces_are_skipped(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ws-1", "ws-child", "ws-3"],
        workspaces=[
            ("ws-1", "First", "s-1", None),
            ("ws-child", "Spawned by an agent", "s-2", None),
            ("ws-3", "Third", "s-3", None),
        ],
        sessions=[
            ("s-1", "a", 0, 0, None),
            ("s-2", "b", 1, 0, None),
            ("s-3", "c", 0, 0, None),
        ],
        children=["ws-child"],
    )
    rows = db.pinned_sessions(8)
    assert [r.workspace_id for r in rows] == ["ws-1", "ws-3"]
    # Slots stay contiguous after the removal.
    assert [r.slot for r in rows] == [0, 1]


def test_missing_parent_links_table_is_tolerated(tmp_path):
    """An older app schema must not break slot resolution."""
    db = build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[("s-1", "a", 0, 0, None)],
    )
    conn = sqlite3.connect(db.db_path)
    conn.execute("DROP TABLE workspace_parent_links")
    conn.commit()
    conn.close()
    assert [r.workspace_id for r in db.pinned_sessions(8)] == ["ws-1"]


# --- child activity roll-up ----------------------------------------------
# A parent that has delegated its work to subagents is still working. Without
# rolling their activity up, such a session looks idle while its children run.


def build_family(tmp_path, child_running=0, child_unread=(), depth=1, parent_unread=False):
    """A pinned parent with a child (optionally a grandchild)."""
    links = [("ws-child", "ws-parent")]
    workspaces = [
        ("ws-parent", "Parent", "s-parent", None),
        ("ws-child", "Child", "s-child", None),
    ]
    sessions = [
        ("s-parent", "parent", 0, 0, None),
        ("s-child", "child", child_running if depth == 1 else 0, 0, None),
    ]
    if depth > 1:
        links.append(("ws-grand", "ws-child"))
        workspaces.append(("ws-grand", "Grandchild", "s-grand", None))
        sessions.append(("s-grand", "grand", child_running, 0, None))

    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (PINS_KEY, json.dumps({"state": {"pinnedWorkspaceIds": ["ws-parent"]}})),
    )
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (
            UNREAD_KEY,
            json.dumps(list(child_unread) + (["ws-parent"] if parent_unread else [])),
        ),
    )
    conn.executemany(
        "INSERT INTO workspaces (id,name,session_id,archived_at,updated_at)"
        " VALUES (?,?,?,?,NULL)",
        workspaces,
    )
    conn.executemany(
        "INSERT INTO sessions (id,title,is_running,was_interrupted,archived_at)"
        " VALUES (?,?,?,?,?)",
        sessions,
    )
    conn.executemany(
        "INSERT INTO workspace_parent_links"
        " (child_workspace_id,parent_workspace_id,creator_session_id,created_at)"
        " VALUES (?,?,'s','2026-01-01')",
        links,
    )
    conn.commit()
    conn.close()
    return CopilotDB(path)


def test_running_child_makes_the_parent_working(tmp_path):
    db = build_family(tmp_path, child_running=1)
    rows = db.pinned_sessions(8)
    assert len(rows) == 1
    assert rows[0].name == "Parent"
    assert rows[0].is_running is True


def test_idle_children_leave_the_parent_idle(tmp_path):
    db = build_family(tmp_path, child_running=0)
    assert db.pinned_sessions(8)[0].is_running is False


def test_unread_child_does_not_make_the_parent_unread(tmp_path):
    """Green has to be clearable by visiting the pinned session.

    The app does not mark a parent unread when a child has output, and
    overriding it produced a light nothing could turn off: one pin had 51
    unread descendants, so clearing it by hand meant opening 51 child sessions.
    Unlike work, which stops on its own, unread only clears by being read.
    """
    db = build_family(tmp_path, child_unread=["ws-child"])
    assert db.pinned_sessions(8)[0].unread is False


def test_the_parents_own_unread_still_counts(tmp_path):
    """Dropping the roll-up must not lose the signal that actually matters."""
    db = build_family(tmp_path, parent_unread=True)
    assert db.pinned_sessions(8)[0].unread is True


def test_rollup_reaches_grandchildren(tmp_path):
    """Work delegated two levels down still counts."""
    db = build_family(tmp_path, child_running=1, depth=2)
    assert db.pinned_sessions(8)[0].is_running is True


def test_archived_child_does_not_count(tmp_path):
    db = build_family(tmp_path, child_running=1)
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE workspaces SET archived_at='2026-01-01' WHERE id='ws-child'")
    conn.commit()
    conn.close()
    assert db.pinned_sessions(8)[0].is_running is False


def test_descendant_walk_survives_a_cycle():
    """A corrupted link table must not cause an infinite walk."""
    cyclic = {"a": ["b"], "b": ["c"], "c": ["a"]}
    assert CopilotDB.descendants_of(cyclic, "a") == {"b", "c"}


# --- ordering -------------------------------------------------------------
# Pins are drag-ordered, so their stored order IS the order on screen. Key N
# must address the Nth pin the user sees.


def test_stored_drag_order_is_preserved(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ws-old", "ws-new"],
        workspaces=[
            ("ws-old", "Oldest", "s-1", None, "2026-01-01T00:00:00"),
            ("ws-new", "Newest", "s-2", None, "2026-03-01T00:00:00"),
        ],
        sessions=[("s-1", "a", 0, 0, None), ("s-2", "b", 0, 0, None)],
        sort_mode="activity",
    )
    # Even with the app sorting other lists by activity, pins keep their order.
    assert [r.workspace_id for r in db.pinned_sessions(8)] == ["ws-old", "ws-new"]


# --- pinned chat sessions -------------------------------------------------
# pinnedWorkspaceIds holds a MIX of workspace ids and session ids: a chat
# session has no workspace. Dropping those shifts every later key up by one,
# silently mis-addressing sessions.


def test_pinned_chat_session_occupies_its_slot(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ws-1", "s-chat", "ws-2"],
        workspaces=[
            ("ws-1", "First", "s-1", None),
            ("ws-2", "Third", "s-2", None),
        ],
        sessions=[
            ("s-1", "a", 0, 0, None),
            ("s-2", "b", 0, 0, None),
            ("s-chat", "pr-stack skill ownership", 1, 0, None),
        ],
    )
    rows = db.pinned_sessions(8)
    assert [r.name for r in rows] == ["First", "pr-stack skill ownership", "Third"]
    # The chat session keeps the middle slot rather than being skipped.
    assert rows[1].slot == 1
    assert rows[1].workspace_id is None
    assert rows[1].session_id == "s-chat"
    assert rows[1].focusable is True
    assert rows[1].is_running is True


def test_archived_chat_session_is_skipped(tmp_path):
    db = build_db(
        tmp_path,
        pins=["s-chat", "ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[
            ("s-1", "a", 0, 0, None),
            ("s-chat", "gone", 0, 0, "2026-01-01T00:00:00"),
        ],
    )
    assert [r.name for r in db.pinned_sessions(8)] == ["First"]


def test_pin_matching_nothing_is_skipped(tmp_path):
    db = build_db(
        tmp_path,
        pins=["ghost", "ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[("s-1", "a", 0, 0, None)],
    )
    assert [r.name for r in db.pinned_sessions(8)] == ["First"]

def test_focused_ids_reports_active_workspace_and_route(tmp_path):
    """Used to tell when a deep-link navigation actually landed."""
    db_path = tmp_path / "data.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        [
            ("activeWorkspaceId", "ws-123"),
            ("lastRoutePath", "/workspaces/ws-123"),
        ],
    )
    conn.commit()
    conn.close()

    ids = CopilotDB(db_path).focused_ids()
    assert "ws-123" in ids
    assert "/workspaces/ws-123" in ids


def test_focused_ids_matches_a_session_id_route(tmp_path):
    """Chat sessions have no workspace, so the route is the only signal."""
    db_path = tmp_path / "data.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        ("lastRoutePath", "/sessions/sess-abc"),
    )
    conn.commit()
    conn.close()

    assert "sess-abc" in CopilotDB(db_path).focused_ids()


def test_focused_ids_is_empty_when_db_is_unreadable(tmp_path):
    assert CopilotDB(tmp_path / "missing.db").focused_ids() == set()

def test_latest_activity_marks_a_session_as_asking(tmp_path):
    """The app records agent_asking only when the agent really stops to ask."""
    db = build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[("s-1", "First session", 1, 0, None)],
        activity=[("s-1", "agent_idle", "2026-01-01T00:00:00Z"),
                  ("s-1", "agent_asking", "2026-01-01T01:00:00Z")],
    )
    assert db.pinned_sessions(4)[0].asking is True


def test_an_answered_question_stops_asking(tmp_path):
    """Only the newest item counts, so answering clears the slot by itself.

    is_read cannot be used for this: the app leaves it 0 on essentially every
    row, so every question ever asked would look outstanding forever.
    """
    db = build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[("s-1", "First session", 1, 0, None)],
        activity=[("s-1", "agent_asking", "2026-01-01T00:00:00Z"),
                  ("s-1", "agent_idle", "2026-01-01T02:00:00Z")],
    )
    assert db.pinned_sessions(4)[0].asking is False


def test_a_child_asking_surfaces_on_the_parent(tmp_path):
    """A subagent blocked on you is still you being blocked."""
    db = build_db(
        tmp_path,
        pins=["parent-ws"],
        workspaces=[("parent-ws", "Parent", "s-p", None),
                    ("kid-ws", "Kid", "s-k", None)],
        sessions=[("s-p", "Parent session", 1, 0, None),
                  ("s-k", "Kid session", 1, 0, None)],
        children=["kid-ws"],
        activity=[("s-k", "agent_asking", "2026-01-01T01:00:00Z")],
    )
    assert db.pinned_sessions(4)[0].asking is True


def test_plan_ready_counts_as_asking(tmp_path):
    """A plan awaiting approval is just as blocked as a question."""
    db = build_db(
        tmp_path,
        pins=["ws-1"],
        workspaces=[("ws-1", "First", "s-1", None)],
        sessions=[("s-1", "First session", 1, 0, None)],
        activity=[("s-1", "agent_plan_ready", "2026-01-01T01:00:00Z")],
    )
    assert db.pinned_sessions(4)[0].asking is True
