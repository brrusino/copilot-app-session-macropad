# SPDX-License-Identifier: MIT
"""Tests for the network pad transport used when the pad is on another machine."""

import json
import socket
import threading
import time

import pytest

from macropad_daemon.network_link import NetworkLink, load_or_create_token

TOKEN = "test-token-value"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Recorder:
    def __init__(self):
        self.events = []
        self.seen = threading.Event()

    def __call__(self, message):
        self.events.append(message)
        self.seen.set()

    def wait(self, timeout=3.0):
        assert self.seen.wait(timeout), "no event received"
        self.seen.clear()


@pytest.fixture
def link():
    recorder = Recorder()
    net = NetworkLink(recorder, "127.0.0.1", free_port(), TOKEN)
    net.start()
    try:
        yield net, recorder
    finally:
        net.stop()


def connect(net, token=TOKEN):
    sock = socket.create_connection(("127.0.0.1", net.port), timeout=3)
    sock.sendall(json.dumps({"t": "auth", "token": token}).encode() + b"\n")
    return sock


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_authenticated_bridge_connects(link):
    net, _ = link
    sock = connect(net)
    try:
        assert wait_until(lambda: net.connected)
    finally:
        sock.close()


def test_bad_token_is_rejected(link):
    net, _ = link
    sock = connect(net, token="wrong")
    try:
        assert not wait_until(lambda: net.connected, timeout=1.0)
    finally:
        sock.close()


def test_garbage_handshake_is_rejected(link):
    net, _ = link
    sock = socket.create_connection(("127.0.0.1", net.port), timeout=3)
    try:
        sock.sendall(b"not json at all\n")
        assert not wait_until(lambda: net.connected, timeout=1.0)
    finally:
        sock.close()


def test_key_events_flow_from_bridge_to_daemon(link):
    net, recorder = link
    sock = connect(net)
    try:
        assert wait_until(lambda: net.connected)
        sock.sendall(b'{"t":"down","k":3,"role":"session","slot":0}\n')
        recorder.wait()
        assert recorder.events[0] == {"t": "down", "k": 3, "role": "session", "slot": 0}
    finally:
        sock.close()


def test_multiple_frames_in_one_packet(link):
    net, recorder = link
    sock = connect(net)
    try:
        assert wait_until(lambda: net.connected)
        sock.sendall(b'{"t":"hb"}\n{"t":"down","k":1}\n')
        assert wait_until(lambda: len(recorder.events) >= 2)
        assert recorder.events[1]["k"] == 1
    finally:
        sock.close()


def test_malformed_frame_does_not_break_the_link(link):
    net, recorder = link
    sock = connect(net)
    try:
        assert wait_until(lambda: net.connected)
        sock.sendall(b'{{{bad\n{"t":"down","k":7}\n')
        assert wait_until(lambda: any(e.get("k") == 7 for e in recorder.events))
    finally:
        sock.close()


def test_states_flow_from_daemon_to_bridge(link):
    net, _ = link
    sock = connect(net)
    try:
        assert wait_until(lambda: net.connected)
        assert net.send({"t": "states", "v": ["idle"] * 8}) is True
        sock.settimeout(3)
        received = sock.recv(1024).decode()
        assert json.loads(received.strip())["t"] == "states"
    finally:
        sock.close()


def test_send_fails_cleanly_with_no_bridge(link):
    net, _ = link
    assert net.connected is False
    assert net.send({"t": "hb"}) is False


def test_on_connect_fires_for_bridge(link):
    net, _ = link
    fired = threading.Event()
    net.set_on_connect(fired.set)
    sock = connect(net)
    try:
        assert fired.wait(3)
    finally:
        sock.close()


def test_newer_bridge_replaces_older(link):
    """Reconnecting after a dropped link must not deadlock on the stale one."""
    net, _ = link
    first = connect(net)
    assert wait_until(lambda: net.connected)
    second = connect(net)
    try:
        assert wait_until(lambda: net.connected)
        assert net.send({"t": "hb"}) is True
    finally:
        first.close()
        second.close()


def test_token_generated_and_reused(tmp_path):
    first = load_or_create_token(tmp_path)
    assert len(first) > 20
    assert load_or_create_token(tmp_path) == first


def test_token_file_written_to_copilot_home(tmp_path):
    load_or_create_token(tmp_path)
    assert (tmp_path / "macropad.token").is_file()


# --- connect mode ---------------------------------------------------------
# Needed when the daemon's machine cannot accept inbound connections, e.g. a
# Cloud PC behind a gateway or a firewall you are not an admin on.


def test_rejects_unknown_mode():
    with pytest.raises(ValueError):
        NetworkLink(lambda m: None, "127.0.0.1", 1234, "t", mode="sideways")


def test_connect_mode_dials_out_and_authenticates():
    """Daemon dials the bridge and presents the token first."""
    port = free_port()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.settimeout(6)

    net = NetworkLink(lambda m: None, "127.0.0.1", port, TOKEN, mode="connect")
    net.start()
    try:
        conn, _ = server.accept()
        conn.settimeout(4)
        line = b""
        while b"\n" not in line:
            chunk = conn.recv(256)
            if not chunk:
                break
            line += chunk
        handshake = json.loads(line.split(b"\n")[0])
        assert handshake["token"] == TOKEN
        assert wait_until(lambda: net.connected)
        conn.close()
    finally:
        net.stop()
        server.close()


def test_connect_mode_events_flow_from_bridge():
    port = free_port()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.settimeout(6)

    recorder = Recorder()
    net = NetworkLink(recorder, "127.0.0.1", port, TOKEN, mode="connect")
    net.start()
    try:
        conn, _ = server.accept()
        conn.settimeout(4)
        conn.recv(512)  # handshake
        conn.sendall(b'{"t":"down","k":3,"role":"session","slot":0}\n')
        recorder.wait()
        assert recorder.events[0]["slot"] == 0
        conn.close()
    finally:
        net.stop()
        server.close()


def test_connect_mode_survives_an_absent_bridge():
    """No listener yet must mean 'keep retrying', not crash."""
    net = NetworkLink(lambda m: None, "127.0.0.1", free_port(), TOKEN, mode="connect")
    net.start()
    try:
        time.sleep(1.0)
        assert net.connected is False
        assert net.send({"t": "hb"}) is False
    finally:
        net.stop()
