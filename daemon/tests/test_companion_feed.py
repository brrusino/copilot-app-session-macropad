# SPDX-License-Identifier: MIT
"""Companion parsing, discovery, and an isolated authenticated WebSocket feed."""

import json
import os
import threading

import pytest
from websockets.sync.server import serve

from macropad_daemon import companion_feed as companion
from macropad_daemon.companion_feed import CompanionFeed, discover, parse_snapshot


def test_reconnect_replays_the_current_revision_and_notifies_disconnect():
    received, disconnected = [], []
    feed = CompanionFeed(received.append, on_disconnect=lambda: disconnected.append(True))
    snapshot = parse_snapshot({
        "type": "snapshot", "instanceId": "app", "revision": 1,
    })
    feed._connected.set()
    feed._accept_snapshot(snapshot)
    feed._disconnect()
    feed._disconnect()
    assert disconnected == [True]
    assert not feed.connected
    assert feed.latest is snapshot
    feed._connected.set()
    assert feed._accept_snapshot(snapshot, replay=True)
    assert len(received) == 2


def snapshot_message(revision=1, instance="instance-a"):
    return {
        "type": "snapshot", "protocolVersion": "1", "instanceId": instance,
        "revision": revision, "generatedAt": "2026-10-22T16:00:00Z",
        "root": {
            "items": [{"kind": "project", "id": "project-a", "label": "Project",
                       "iconKind": "folder"}],
            "totalCount": 1,
        },
        "collections": {
            "project:project-a": {
                "items": [{
                    "kind": "workspace", "id": "session-a", "label": "Session",
                    "status": "future-status", "statusLabel": "Future status",
                    "indicatorKind": "attention", "unread": True,
                    "deepLink": "ghapp://sessions/session-a", "accentToken": "blue",
                    "pendingQuestion": {
                        "choiceCount": 2, "customInputAvailable": True,
                        "choiceLabelPrefixes": ["One", "Two"], "future": [1, 2],
                    },
                    "futureItemField": {"nested": True},
                }],
                "totalCount": 7, "futureCollectionField": "retained",
            },
        },
        "activeSession": {
            "id": "session-a", "kind": "workspace", "workspaceId": "workspace-a",
            "projectId": "project-a", "modelLabel": "Model",
            "modelProviderKind": "provider", "reasoningEffort": "high",
            "reasoningEffortLabel": "High", "contextTier": "long_context",
            "contextTierLabel": "Long", "contextCurrentTokens": 1234,
            "contextTokenLimit": 100000, "contextUsageLabel": "1%",
            "aiCreditLabel": "1 credit", "returnCollectionKey": "project:project-a",
            "futureActiveField": True,
        },
        "mcpServers": [{"name": "server", "future": True}],
        "voiceDictation": {"enabled": False},
        "futureSnapshotField": "retained",
    }


def write_discovery(home, *, name="logitech", pid=None, instance="instance-a",
                    token="test-token", url="ws://127.0.0.1:1234/v1/logitech/navigation"):
    run = home / "run"
    run.mkdir(exist_ok=True)
    path = run / f"{name}-navigation-v1.json"
    path.write_text(json.dumps({
        "schemaVersion": 1, "websocketUrl": url, "bearerToken": token,
        "instanceId": instance, "pid": os.getpid() if pid is None else pid,
    }), encoding="utf-8")
    return path


def test_parse_preserves_unknown_fields_and_status():
    message = snapshot_message(9)
    snapshot = parse_snapshot(message)
    assert snapshot.revision == 9
    assert snapshot.protocol_version == "1"
    assert snapshot.instance_id == "instance-a"
    assert snapshot.generated_at == message["generatedAt"]
    assert snapshot.root.total_count == 1
    assert snapshot.root.items[0].icon_kind == "folder"
    collection = snapshot.collections["project:project-a"]
    assert collection.total_count == 7
    assert collection.raw["futureCollectionField"] == "retained"
    item = collection.items[0]
    assert (item.kind, item.id, item.label) == ("workspace", "session-a", "Session")
    assert item.status == "future-status"
    assert item.status_label == "Future status"
    assert item.indicator_kind == "attention"
    assert item.deep_link == "ghapp://sessions/session-a"
    assert item.unread is True
    assert item.accent_token == "blue"
    assert item.pending_question["future"] == [1, 2]
    assert item.raw["futureItemField"] == {"nested": True}
    active = snapshot.active_session
    assert active.workspace_id == "workspace-a"
    assert active.project_id == "project-a"
    assert active.model_label == "Model"
    assert active.model_provider_kind == "provider"
    assert active.reasoning_effort == "high"
    assert active.reasoning_effort_label == "High"
    assert active.context_tier == "long_context"
    assert active.context_tier_label == "Long"
    assert active.context_current_tokens == 1234
    assert active.context_token_limit == 100000
    assert active.context_usage_label == "1%"
    assert active.ai_credit_label == "1 credit"
    assert active.return_collection_key == "project:project-a"
    assert active.raw["futureActiveField"] is True
    assert snapshot.mcp_servers == message["mcpServers"]
    assert snapshot.voice_dictation == message["voiceDictation"]
    assert snapshot.raw == message
    item.raw["futureItemField"]["nested"] = False
    assert message["collections"]["project:project-a"]["items"][0]["futureItemField"]["nested"]


@pytest.mark.parametrize("question", [None, "unknown-shape", [], {"future": True}])
def test_parse_optional_fields(question):
    message = snapshot_message()
    message["activeSession"] = None
    item = message["collections"]["project:project-a"]["items"][0]
    item.clear()
    item.update(kind="chat", id="chat-a", label="Chat", pendingQuestion=question)
    snapshot = parse_snapshot(message)
    parsed = snapshot.collections["project:project-a"].items[0]
    assert snapshot.active_session is None
    assert parsed.status is None
    assert parsed.pending_question == (question if isinstance(question, dict) else None)
    assert parsed.raw["pendingQuestion"] == question


@pytest.mark.parametrize("message", [
    {}, {"type": "hello"}, {"type": "snapshot", "revision": True, "instanceId": "a"},
    {"type": "snapshot", "revision": "1", "instanceId": "a"},
])
def test_parse_rejects_invalid_envelope(message):
    with pytest.raises(ValueError):
        parse_snapshot(message)


def test_discover_missing_file(tmp_path):
    assert discover(tmp_path) is None


def test_discover_prefers_live_companion(tmp_path):
    write_discovery(tmp_path)
    write_discovery(tmp_path, name="companion", instance="preferred")
    discovery = discover(tmp_path)
    assert discovery.instance_id == "preferred"
    assert discovery.pid == os.getpid()
    assert "test-token" not in repr(discovery)


def test_discover_stale_pid_falls_back(tmp_path, monkeypatch):
    write_discovery(tmp_path, pid=123)
    write_discovery(tmp_path, name="companion", pid=456)
    monkeypatch.setattr(companion, "_pid_alive", lambda pid: pid == 123)
    assert discover(tmp_path).pid == 123
    monkeypatch.setattr(companion, "_pid_alive", lambda pid: False)
    assert discover(tmp_path) is None


@pytest.mark.parametrize("content", ["{", "null", "[]", '{"pid": 1}'])
def test_discover_malformed_file_falls_back(tmp_path, content):
    write_discovery(tmp_path)
    preferred = write_discovery(tmp_path, name="companion")
    preferred.write_text(content, encoding="utf-8")
    assert discover(tmp_path).instance_id == "instance-a"


@pytest.mark.skipif(os.name != "nt", reason="Windows process API")
def test_windows_liveness_never_uses_kill(monkeypatch):
    def forbidden(*args):
        pytest.fail("os.kill must never be used on Windows")

    monkeypatch.setattr(os, "kill", forbidden)
    assert companion._pid_alive(os.getpid())
    assert not companion._pid_alive(0)


def test_revision_ordering_and_instance_restart(tmp_path):
    received = []
    feed = CompanionFeed(received.append, tmp_path)
    for revision in (10, 9, 10, 11):
        feed._accept_snapshot(parse_snapshot(snapshot_message(revision)))
    assert [snapshot.revision for snapshot in received] == [10, 11]
    feed._accept_snapshot(parse_snapshot(snapshot_message(1, "instance-b")))
    assert feed.latest.instance_id == "instance-b"
    assert feed.latest.revision == 1
    assert len(received) == 3


def test_stop_interrupts_discovery_backoff(tmp_path):
    feed = CompanionFeed(lambda snapshot: None, tmp_path, reconnect_delay=10)
    feed.start()
    feed.start()
    feed.stop()
    feed.stop()
    assert not feed._thread.is_alive()
    assert not feed.connected
    assert feed.latest is None


def test_authenticated_feed_and_restart(tmp_path):
    received = []
    errors = []
    done = threading.Event()
    release = threading.Event()
    handshakes = []

    def callback(snapshot):
        received.append(snapshot)
        if len(received) == 3:
            done.set()

    def handler(socket):
        try:
            header = socket.request.headers["Authorization"]
            handshakes.append(header)
            if len(handshakes) == 1:
                assert header == "Bearer first-token"
                socket.send(json.dumps({"type": "hello", "protocolVersion": "1",
                                        "instanceId": "instance-a"}))
                first = snapshot_message(4)
                first["largeField"] = "x" * 100000
                socket.send(json.dumps(first))
                socket.send("not JSON")
                socket.send(json.dumps({"type": "snapshot", "revision": "bad"}))
                socket.send(json.dumps(snapshot_message(5)))
                socket.send(json.dumps(snapshot_message(4)))
                socket.send(json.dumps(snapshot_message(5)))
                write_discovery(tmp_path, name="companion", token="second-token",
                                instance="instance-b", url=url)
            else:
                assert header == "Bearer second-token"
                socket.send(json.dumps({"type": "hello", "protocolVersion": "1",
                                        "instanceId": "instance-b"}))
                socket.send(json.dumps(snapshot_message(99, "wrong-instance")))
                socket.send(json.dumps(snapshot_message(1, "instance-b")))
                assert release.wait(5)
        except Exception as exc:
            errors.append(exc)
            done.set()

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        url = f"ws://127.0.0.1:{port}/v1/logitech/navigation"
        write_discovery(tmp_path, token="first-token", url=url)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        feed = CompanionFeed(callback, tmp_path, reconnect_delay=0.01)
        try:
            feed.start()
            assert done.wait(5), "snapshots didn't arrive"
            assert not errors
            assert handshakes == ["Bearer first-token", "Bearer second-token"]
            assert [(item.instance_id, item.revision) for item in received] == [
                ("instance-a", 4), ("instance-a", 5), ("instance-b", 1),
            ]
            assert len(received[0].raw["largeField"]) == 100000
            assert feed.latest is received[-1]
            assert feed.connected
        finally:
            feed.stop()
            release.set()
            server.shutdown()
            server_thread.join(timeout=5)
        assert not feed.connected
        assert not feed._thread.is_alive()
        assert not server_thread.is_alive()
