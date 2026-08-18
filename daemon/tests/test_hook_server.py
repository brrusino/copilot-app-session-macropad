# SPDX-License-Identifier: MIT
"""Integration tests for the hook receiver over real HTTP.

These exercise the actual socket path a Copilot hook command would take, so a
regression in request handling shows up here rather than at 2am when a LED
stops changing.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from macropad_daemon.hook_server import HookServer, PortInUseError
from macropad_daemon.state import StateStore
from macropad_daemon.copilot_db import PinnedSession


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Recorder:
    def __init__(self):
        self.events = []
        self.seen = threading.Event()

    def __call__(self, event_type, session_id, payload):
        self.events.append((event_type, session_id, payload))
        self.seen.set()

    def wait(self, timeout=2.0):
        assert self.seen.wait(timeout), "callback was never invoked"
        self.seen.clear()


@pytest.fixture
def server():
    recorder = Recorder()
    srv = HookServer("127.0.0.1", free_port(), recorder)
    srv.start()
    try:
        yield srv, recorder
    finally:
        srv.stop()


def post(srv, event, payload, timeout=3):
    url = f"http://127.0.0.1:{srv.port}/hook/{event}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def test_hook_post_reaches_callback(server):
    srv, recorder = server
    status, _ = post(srv, "agentStop", {"sessionId": "sess-xyz", "result": "completed"})
    assert status == 204
    recorder.wait()
    event_type, session_id, payload = recorder.events[0]
    assert event_type == "agentStop"
    assert session_id == "sess-xyz"
    assert payload["result"] == "completed"


def test_hook_returns_no_body(server):
    """A hook that emits nothing cannot accidentally vote on a permission."""
    srv, _ = server
    status, body = post(srv, "permissionRequest", {"sessionId": "s"})
    assert status == 204
    assert body == b""


def test_snake_case_session_id_accepted(server):
    srv, recorder = server
    post(srv, "agentStop", {"session_id": "snake"})
    recorder.wait()
    assert recorder.events[0][1] == "snake"


def test_missing_session_id_is_not_fatal(server):
    srv, recorder = server
    status, _ = post(srv, "agentStop", {})
    assert status == 204
    recorder.wait()
    assert recorder.events[0][1] == ""


def test_malformed_body_is_not_fatal(server):
    """A hook must never fail an agent turn, whatever it sends."""
    srv, recorder = server
    url = f"http://127.0.0.1:{srv.port}/hook/agentStop"
    request = urllib.request.Request(
        url, data=b"{{{not json", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        assert response.status == 204
    recorder.wait()
    assert recorder.events[0][0] == "agentStop"


def test_empty_body_is_not_fatal(server):
    srv, recorder = server
    url = f"http://127.0.0.1:{srv.port}/hook/sessionStart"
    with urllib.request.urlopen(urllib.request.Request(url, data=b""), timeout=3) as response:
        assert response.status == 204
    recorder.wait()


def test_callback_exception_still_returns_success(server):
    """A daemon-side bug must not propagate into the agent's turn."""
    srv = HookServer("127.0.0.1", free_port(), lambda *a: 1 / 0)
    srv.start()
    try:
        status, _ = post(srv, "agentStop", {"sessionId": "s"})
        assert status == 204
    finally:
        srv.stop()


def test_health_endpoint(server):
    srv, _ = server
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/health", timeout=3) as response:
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True


def test_second_instance_refuses_to_share_the_port(server):
    """A duplicate daemon must fail loudly, not silently steal hook traffic.

    Python enables SO_REUSEADDR by default, and on Windows that lets a second
    process bind a port someone else is already listening on. The newcomer then
    looks healthy while the stale instance keeps receiving every request, so the
    LEDs stop updating with no error anywhere. Regression test for that.
    """
    srv, _ = server
    duplicate = HookServer("127.0.0.1", srv.port, lambda *a: None)
    with pytest.raises(PortInUseError):
        duplicate.start()


def test_port_in_use_message_is_actionable(server):
    srv, _ = server
    duplicate = HookServer("127.0.0.1", srv.port, lambda *a: None)
    with pytest.raises(PortInUseError) as info:
        duplicate.start()
    assert "already running" in str(info.value)


def test_port_released_on_stop():
    """Stopping must free the port so a restart works."""
    port = free_port()
    first = HookServer("127.0.0.1", port, lambda *a: None)
    first.start()
    first.stop()
    second = HookServer("127.0.0.1", port, lambda *a: None)
    second.start()
    second.stop()


def test_end_to_end_hook_changes_slot_colour(server):
    """The whole observe path: HTTP hook in, LED state out."""
    srv, recorder = server
    store = StateStore(slot_count=2)
    store.apply_snapshot(
        [
            PinnedSession(
                slot=0,
                workspace_id="ws-1",
                session_id="sess-1",
                name="demo",
                is_running=False,
                unread=False,
                was_interrupted=False,
                # A session that actually asks, so an approval prompt is a real
                # block rather than an auto-approved tool call.
                auto_approve=False,
            )
        ],
        now=100.0,
    )
    srv._callback = lambda event, sid, payload: store.apply_hook(event, sid, now=200.0)
    # Rebind the live server's callback too.
    srv._server.hook_callback = srv._callback

    assert store.slot_states()[0] == "idle"
    post(srv, "permissionRequest", {"sessionId": "sess-1"})

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if store.slot_states()[0] == "needs_approval":
            break
        time.sleep(0.02)
    assert store.slot_states()[0] == "needs_approval"
