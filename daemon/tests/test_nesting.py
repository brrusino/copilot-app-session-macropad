# SPDX-License-Identifier: MIT
"""Workstream status, exact navigation, and children pages without hardware."""

from dataclasses import replace
import sqlite3

import pytest

from macropad_daemon.companion_feed import parse_snapshot
from macropad_daemon.copilot_db import Section
from macropad_daemon.state import StateStore
from test_copilot_db import build_db, build_family
from test_daemon_actions import daemon, session


def node(slot, **kwargs):
    extra = {key: kwargs.pop(key) for key in ("children", "watched") if key in kwargs}
    return replace(session(slot, **kwargs), **extra)


def snapshot(*items, revision=1):
    """Items are (session id, status[, indicator kind[, item kind]])."""
    return parse_snapshot({
        "type": "snapshot", "instanceId": "app", "revision": revision,
        "generatedAt": "2026-10-22T16:30:00Z",
        "collections": {"pinned": {"items": [
            {"id": i[0], "status": i[1],
             "indicatorKind": i[2] if len(i) > 2 else None,
             "kind": i[3] if len(i) > 3 else "workspace"}
            for i in items
        ]}},
    })


@pytest.mark.parametrize("status,indicator,expected", [
    ("needs_input", None, "needs_approval"),
    ("attention", "needs_input", "needs_approval"),
    ("error", None, "error"),
    ("interrupted", None, "interrupted"),
    ("running", None, "working"),
    ("unread", None, "idle"),
    ("merge_ready", None, "idle"),
    ("paused", None, "idle"),
])
@pytest.mark.parametrize("watched", [True, False])
def test_descendant_rollup_table(status, indicator, expected, watched):
    grandchild = node(2, watched=watched)
    root = node(0, children=(node(1, children=(grandchild,)),))
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_feed(snapshot(("s-2", status, indicator)))
    assert store.slot_states()[0] == expected
    assert store.rolled_up([root]) == (None if expected == "idle" else expected)


def test_priority_and_self_only_states():
    root = node(0, unread=True, children=(node(1), node(2), node(3), node(4)))
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_feed(snapshot(("s-1", "running"), ("s-2", "interrupted"),
                              ("s-3", "error"), ("s-4", "needs_input")))
    assert store.resolve(root) == "needs_approval"
    for status in ("merge_ready", "paused", "unread"):
        store.apply_feed(snapshot(("s-0", status)))
        assert store.resolve(root) == status


def test_feed_beats_stale_db_and_hooks_and_falls_back_per_item():
    root = node(0, asking=True, asking_at=1, is_running=True,
                children=(node(1),))
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_hook("errorOccurred", "s-0")
    store.apply_feed(snapshot(("s-0", "idle")))
    assert store.resolve_self(root) == "idle"
    store.apply_hook("errorOccurred", "s-1")
    assert store.resolve(root) == "error"  # child is missing from the feed
    store.apply_feed(snapshot(("s-0", "idle"), ("s-1", "idle")))
    assert store.resolve(root) == "idle"
    store.apply_feed(None)
    assert store.resolve(root) == "error"


def test_unverified_attention_is_not_assumed_to_be_a_question():
    root = node(0)
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_feed(snapshot(("s-0", "attention", "merge_ready")))
    assert store.resolve(root) == "idle"


@pytest.mark.parametrize("evidence", ["hook", "activity", "db_retirement"])
def test_stale_feed_question_uses_fallback_everywhere(evidence, monkeypatch):
    child = node(1, asking=True, asking_at=1, activity=10)
    root = node(0, children=(child,))
    store = StateStore()
    store.apply_snapshot([root])
    if evidence == "db_retirement":
        child = replace(child, activity=11)
        root = replace(root, children=(child,))
        store.apply_snapshot([root])
    store.apply_feed(snapshot(("s-0", "idle"), ("s-1", "attention", "needs_input")))
    if evidence == "hook":
        monkeypatch.setattr("macropad_daemon.state.time.time",
                            lambda: store._feed_question_at["s-1"] + 1)
        store.apply_hook("postToolUse", "s-1")
    elif evidence == "activity":
        child = replace(child, activity=12)
        root = replace(root, children=(child,))
        store.apply_snapshot([root])
    assert store.resolve_self(child) == "working"
    assert store.resolve(root) == "working"
    assert store.tap_target(root) == root
    assert store.attention_sessions() == []
    assert store.next_attention() is None
    assert store.rolled_up([root]) == "working"
    # Clearing the DB question must not resurrect the stuck feed question.
    child = replace(child, asking=False, asking_at=0)
    store.apply_snapshot([replace(root, children=(child,))])
    assert store.resolve_self(child) == "working"


@pytest.mark.parametrize("asking", [False, True])
def test_fresh_feed_question_wins_before_db_catches_up(asking):
    child = node(1, asking=asking, asking_at=1 if asking else 0,
                 is_running=True, activity=10)
    store = StateStore()
    store.apply_snapshot([child])
    store.apply_feed(snapshot(("s-1", "needs_input")))
    # Running alone also accompanies real questions; it isn't an answer.
    store.apply_snapshot([child])
    assert store.resolve_self(child) == "needs_approval"


def test_new_feed_question_does_not_inherit_previous_retirement():
    child = node(1, activity=10)
    store = StateStore()
    store.apply_snapshot([child])
    store.apply_feed(snapshot(("s-1", "needs_input")))
    child = replace(child, activity=11)
    store.apply_snapshot([child])
    assert store.resolve_self(child) == "working"
    store.apply_feed(snapshot(("s-1", "running"), revision=2))
    store.apply_feed(snapshot(("s-1", "needs_input"), revision=3))
    assert store.resolve_self(child) == "needs_approval"


def test_child_question_is_retired_using_the_childs_work_not_the_parents():
    child = node(1, asking=True, asking_at=1)
    root = node(0, children=(child,))
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_hook("userPromptSubmitted", "s-0")
    assert store.resolve(root) == "needs_approval"
    store.apply_hook("userPromptSubmitted", "s-1")
    assert store.resolve(root) == "working"
    store.apply_snapshot([], offscreen=[root])
    assert store.resolve(root) == "working"


def test_tap_jumps_to_oldest_question_then_error_then_interrupted():
    interrupted = node(1, was_interrupted=True)
    error = node(2)
    newer = node(3, asking=True, asking_at=30)
    older = node(4, asking=True, asking_at=10)
    root = node(0, children=(interrupted, error, node(5, children=(newer, older))))
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_hook("errorOccurred", "s-2")
    assert store.tap_target(root).session_id == "s-4"
    root = replace(root, children=(interrupted, error))
    assert store.tap_target(root).session_id == "s-2"
    root = replace(root, children=(interrupted,))
    assert store.tap_target(root).session_id == "s-1"
    assert store.tap_target(replace(root, children=(node(6, unread=True),))).session_id == "s-0"


def test_attention_walks_tree_in_priority_order_excluding_watched_unread():
    root = node(0, unread=True, children=(
        node(1, unread=True, watched=True),
        node(2, unread=True),
        node(3, was_interrupted=True),
        node(4, asking=True, asking_at=20),
    ))
    other = node(5, children=(node(6), node(7, asking=True, asking_at=10)))
    store = StateStore()
    store.apply_snapshot([root], offscreen=[other])
    store.apply_hook("errorOccurred", "s-6")
    expected = ["s-7", "s-4", "s-6", "s-3", "s-0", "s-2"]
    assert [s.session_id for s in store.attention_sessions()] == expected
    after = None
    actual = []
    for _ in range(len(expected) + 1):
        target = store.next_attention(after)
        actual.append(target.session_id)
        after = target.session_id
    assert actual == [*expected, expected[0]]


def test_archived_errors_and_interruptions_stop_rolling_up(tmp_path):
    db = build_family(tmp_path)
    root = db.sections()[0].sessions[0]
    store = StateStore()
    store.apply_snapshot([root])
    store.apply_hook("errorOccurred", "s-child")
    assert store.resolve(root) == "error"
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE sessions SET archived_at='2000-01-01' WHERE id='s-child'")
    refreshed = db.refresh_status([Section("Pinned", (root,))])
    root = refreshed[0].sessions[0]
    store.apply_snapshot([root])
    assert root.children == ()
    assert store.resolve(root) == "idle"


def test_coordinator_wins_over_creator_and_explicit_link(tmp_path):
    db = build_db(
        tmp_path, pins=["ws-a", "ws-b", "ws-c"],
        workspaces=[
            ("ws-a", "A", "s-a", None),
            ("ws-b", "B", "s-b", None),
            ("ws-c", "C", "s-c", None, None, "s-a", "s-b"),
        ],
        sessions=[(sid, sid, 0, 0, None) for sid in ("s-a", "s-b", "s-c")],
    )
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("INSERT INTO workspace_parent_links VALUES ('ws-c','ws-a','s-a','2000-01-01')")
    roots = db.sections()[0].sessions
    assert [s.session_id for s in roots] == ["s-a", "s-b"]
    assert roots[0].children == ()
    assert roots[1].children[0].session_id == "s-c"
    assert roots[1].children[0].watched


@pytest.mark.parametrize("notify,recipient,coordinator,watched", [
    (0, None, None, False),
    (1, None, None, True),
    (0, "s-parent", None, True),
    (0, None, "s-parent", True),
    (0, "outside", None, False),
])
def test_watched_columns_require_a_live_coordinator_in_the_tree(
    tmp_path, notify, recipient, coordinator, watched,
):
    db = build_family(tmp_path)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE workspaces SET notify_creator_on_idle=?,"
            " notification_recipient_session_id=?, coordinating_creator_session_id=?"
            " WHERE id='ws-child'", (notify, recipient, coordinator),
        )
    child = db.sections()[0].sessions[0].children[0]
    assert child.watched is watched


def test_descendants_are_depth_first_in_sidebar_activity_order(tmp_path):
    db = build_db(
        tmp_path, pins=["ws-root"],
        workspaces=[
            ("ws-root", "Root", "s-root", None),
            ("ws-a", "A", "s-a", None, "2026-10-20", "s-root"),
            ("ws-b", "B", "s-b", None, "2026-10-22", "s-root"),
            ("ws-grand", "Grandchild", "s-grand", None, "2026-10-21", "s-b"),
        ],
        sessions=[(sid, sid, 0, 0, None) for sid in ("s-root", "s-a", "s-b", "s-grand")],
    )
    root = db.sections()[0].sessions[0]
    assert [s.session_id for s in root.descendants()] == ["s-b", "s-grand", "s-a"]
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE workspaces SET updated_at='2026-10-23' WHERE id='ws-a'")
    refreshed = db.refresh_status([Section("Pinned", (root,))])[0].sessions[0]
    assert [s.session_id for s in refreshed.descendants()] == ["s-a", "s-b", "s-grand"]


def install_sections(daemon, monkeypatch, sections):
    monkeypatch.setattr(daemon.db, "sections", lambda _limit: sections)
    daemon._read_structure()
    daemon._apply_view()


def test_feed_revisions_push_without_polling_and_only_new_structure_is_read(daemon, monkeypatch):
    sections = [Section("Pinned", (node(0),))]
    reads = []
    monkeypatch.setattr(daemon.db, "sections", lambda limit: reads.append(limit) or sections)
    daemon._on_feed_snapshot(snapshot(("s-0", "running")))
    assert daemon.link.sent[-2] == {"t": "states", "v": ["working"] + ["empty"] * 7}
    daemon._on_feed_snapshot(snapshot(("s-0", "idle"), revision=2))
    assert daemon.store.slot_states()[0] == "idle"
    assert reads == [None]
    daemon._on_feed_snapshot(snapshot(("s-0", "idle"), ("new", "running"), revision=3))
    assert reads == [None, None]


def test_missing_feed_item_polls_fallback_without_rebuilding_structure(daemon, monkeypatch):
    root = node(0, children=(node(1),))
    sections = [Section("Pinned", (root,))]
    install_sections(daemon, monkeypatch, sections)
    daemon._on_feed_snapshot(snapshot(("s-0", "idle")))
    refreshed = [Section("Pinned", (replace(root, children=(node(1, was_interrupted=True),)),))]
    monkeypatch.setattr(daemon.db, "refresh_status", lambda secs: refreshed)
    monkeypatch.setattr(daemon.db, "sections", lambda *_a: pytest.fail("rebuilt structure"))
    daemon.reconcile()
    assert daemon.store.slot_states()[0] == "interrupted"
    assert daemon.store.tap_target(daemon.store.session_for_slot(0)).session_id == "s-1"


def test_disconnect_immediately_uses_fresh_db_and_hooks(daemon, monkeypatch):
    sections = [Section("Pinned", (node(0),))]
    install_sections(daemon, monkeypatch, sections)
    daemon._on_feed_snapshot(snapshot(("s-0", "idle")))
    daemon._on_hook("errorOccurred", "s-0", {})
    assert daemon.store.slot_states()[0] == "idle"
    daemon._on_feed_disconnect()
    assert daemon.store.slot_states()[0] == "error"
    assert daemon.link.sent[-1]["v"][0] == "error"


def test_full_feed_coverage_needs_no_db_poll(daemon, monkeypatch):
    install_sections(daemon, monkeypatch, [Section("Pinned", (node(0),))])
    daemon._on_feed_snapshot(snapshot(("s-0", "idle")))
    monkeypatch.setattr(daemon.db, "sections", lambda *_a: pytest.fail("structure poll"))
    monkeypatch.setattr(daemon.db, "refresh_status", lambda *_a: pytest.fail("status poll"))
    daemon.reconcile()
    daemon.reconcile()


def test_feed_question_polls_work_evidence_and_keeps_fallback_current(daemon, monkeypatch):
    child = node(0, asking=True, asking_at=1, activity=10)
    install_sections(daemon, monkeypatch, [Section("Pinned", (child,))])
    daemon._on_feed_snapshot(snapshot(("s-0", "needs_input")))
    assert daemon.store.slot_states()[0] == "needs_approval"
    calls = []
    refreshed = replace(child, activity=11, is_running=True)
    monkeypatch.setattr(daemon.db, "refresh_status",
                        lambda secs: calls.append(True) or [Section("Pinned", (refreshed,))])
    monkeypatch.setattr(daemon.db, "sections", lambda *_a: pytest.fail("structure poll"))
    daemon.reconcile()
    assert calls == [True]
    assert daemon.store.slot_states()[0] == "working"
    refreshed = replace(refreshed, asking=False, asking_at=0,
                        is_running=False, unread=True)
    daemon.reconcile()
    assert calls == [True, True]
    assert not daemon.store.session_for_slot(0).is_running
    assert not daemon.store.session_for_slot(0).asking
    assert daemon.store.slot_states()[0] != "needs_approval"


def test_hook_unread_hint_is_not_erased_by_switching_the_cached_view(daemon, monkeypatch):
    install_sections(daemon, monkeypatch, [Section("Pinned", (node(0),))])
    daemon._on_hook("agentStop", "s-0", {})
    assert daemon.store.slot_states()[0] == "unread"
    daemon._open_children(0)
    assert daemon.store.slot_states()[0] == "unread"


def test_new_feed_question_refreshes_its_db_timestamp_not_structure(daemon, monkeypatch):
    root = node(0, children=(node(1, asking=True, asking_at=10),))
    install_sections(daemon, monkeypatch, [Section("Pinned", (root,))])
    daemon._on_feed_snapshot(snapshot(("s-0", "idle"), ("s-1", "running")))
    fresh = replace(root, children=(node(1, asking=True, asking_at=30),))
    calls = []
    monkeypatch.setattr(daemon.db, "refresh_status", lambda sections: calls.append(True) or [Section("Pinned", (fresh,))])
    monkeypatch.setattr(daemon.db, "sections", lambda *_a: pytest.fail("structure read"))
    daemon._on_feed_snapshot(snapshot(("s-0", "idle"), ("s-1", "needs_input"), revision=2))
    assert calls == [True]
    assert daemon.store.tap_target(daemon.store.session_for_slot(0)).asking_at == 30


def test_chat_feed_matches_session_id_and_workspace_feed_not_workspace_id():
    store = StateStore()
    chat = replace(node(1), workspace_id=None)
    store.apply_snapshot([node(0), chat])
    store.apply_feed(snapshot(("s-0", "running"), ("s-1", "unread", None, "chat")))
    assert store.slot_states()[:2] == ["working", "unread"]


def test_tap_and_attention_navigate_by_child_identity_across_sections(daemon, monkeypatch):
    root = node(0, children=(node(1, asking=True, asking_at=20),))
    other = node(2, children=(node(3, asking=True, asking_at=10),))
    install_sections(daemon, monkeypatch, [Section("Pinned", (root,)), Section("Other", (other,))])
    daemon._on_pad_event({"t": "down", "role": "session", "slot": 0})
    daemon._run_action("next_attention")
    daemon._run_action("next_attention")
    assert daemon.selected == ["s-1", "s-3", "s-1"]
    assert not any(m["t"] == "type" for m in daemon.link.sent)
    assert daemon._elsewhere_attention_state() == "needs_approval"


def test_children_page_filters_and_pages_with_coordinator_in_slot_one(daemon, monkeypatch):
    children = (
        node(1, unread=True, watched=True), node(2),
        *[node(i, is_running=True) for i in range(3, 13)],
        node(13, unread=True),
    )
    root = node(0, unread=True, children=children)
    install_sections(daemon, monkeypatch, [Section("Pinned", (root,))])
    daemon._on_pad_event({"t": "hold", "role": "session", "slot": 0})
    assert daemon.selected == []
    assert [s.session_id for s in daemon.store._sessions] == ["s-0", *[f"s-{i}" for i in range(3, 10)]]
    assert daemon.store.slot_states()[0] == "unread"  # self, not rolled-up blue
    daemon._run_action("section_down")
    assert [s.session_id for s in daemon.store._sessions] == ["s-0", "s-10", "s-11", "s-12", "s-13"]
    daemon._run_action("section_down")
    assert daemon._children_page == 1  # no wrap
    daemon._on_pad_event({"t": "down", "role": "session", "slot": 4})
    assert daemon.selected == ["s-13"]
    daemon._on_pad_event({"t": "down", "role": "session", "slot": 0})
    assert daemon.selected[-1] == "s-0"
    daemon._run_action("section_up")
    assert daemon._children_root_id is None
    assert daemon.store.slot_states()[0] == "working"


def test_children_page_shows_self_only_states_and_clamps_after_update(daemon, monkeypatch):
    root = node(0, children=tuple(node(i) for i in range(1, 10)))
    install_sections(daemon, monkeypatch, [Section("Pinned", (root,))])
    items = [(f"s-{i}", "paused" if i % 2 else "merge_ready") for i in range(1, 10)]
    daemon._on_feed_snapshot(snapshot(*items))
    assert daemon.store.slot_states()[0] == "idle"
    daemon._open_children(0)
    assert daemon.store.slot_states()[1:3] == ["paused", "merge_ready"]
    daemon._run_action("section_down")
    assert daemon._children_page == 1
    daemon._on_feed_snapshot(snapshot(*[(sid, "idle") for sid, _ in items], revision=2))
    assert daemon._children_page == 0
    assert daemon.store.slot_states() == ["idle"] + ["empty"] * 7


def test_coordinator_tap_on_children_page_still_jumps_to_a_blocked_descendant(daemon, monkeypatch):
    root = node(0, children=(node(1, asking=True, asking_at=10),))
    install_sections(daemon, monkeypatch, [Section("Pinned", (root,))])
    daemon._open_children(0)
    daemon._activate_session(0)
    assert daemon.selected == ["s-1"]


def test_attention_includes_roots_beyond_the_physical_keys(daemon, monkeypatch):
    roots = tuple(node(i, unread=i == 9) for i in range(10))
    install_sections(daemon, monkeypatch, [Section("Pinned", roots)])
    daemon._run_action("next_attention")
    assert daemon.selected == ["s-9"]
