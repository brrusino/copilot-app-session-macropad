# SPDX-License-Identifier: MIT
"""Tests for the cross-platform pad bridge used by macOS clients."""

import importlib.util
import json
import socket
import threading
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pad_bridge.py"
SPEC = importlib.util.spec_from_file_location("pad_bridge", SCRIPT)
pad_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pad_bridge)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeSerial:
    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.dtr = False
        self.writes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, count):
        chunk = bytes(self.data[:count])
        del self.data[:count]
        return chunk

    def write(self, data):
        self.writes.append(data)


def test_probe_recognizes_an_unsolicited_pad_heartbeat(monkeypatch):
    handle = FakeSerial(b'{"t":"hb","fw":3}\n')
    monkeypatch.setattr(pad_bridge.serial, "Serial", lambda *_a, **_k: handle)

    assert pad_bridge.probe("/dev/cu.usbmodem-pad", 115200) is True
    assert handle.dtr is True
    assert handle.writes == []


def test_probe_never_writes_a_frame_the_console_could_echo(monkeypatch):
    handle = FakeSerial()
    monkeypatch.setattr(pad_bridge.serial, "Serial", lambda *_a, **_k: handle)
    monkeypatch.setattr(pad_bridge, "PROBE_TIMEOUT", 0)

    assert pad_bridge.probe("/dev/cu.usbmodem-console", 115200) is False
    assert handle.writes == []


def test_connect_daemon_sends_the_token_first():
    port = free_port()
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    received = {}

    def serve():
        conn, _ = listener.accept()
        with conn:
            received.update(json.loads(conn.recv(512).split(b"\n")[0]))

    thread = threading.Thread(target=serve)
    thread.start()
    with pad_bridge.connect_daemon("127.0.0.1", port, "secret"):
        pass
    thread.join()
    listener.close()

    assert received == {"t": "auth", "token": "secret"}


def test_accept_daemon_authenticates_and_preserves_first_led_frame():
    port = free_port()
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    sent = (
        b'{"t":"auth","token":"secret"}\n'
        b'{"t":"states","v":["working"]}\n'
    )
    client = socket.create_connection(("127.0.0.1", port))
    client.sendall(sent)

    conn, remainder = pad_bridge.accept_daemon(listener, "secret")
    conn.close()
    client.close()
    listener.close()

    assert remainder == b'{"t":"states","v":["working"]}\n'


def test_accept_daemon_rejects_a_bad_token_before_accepting_the_next_client():
    port = free_port()
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(2)

    bad = socket.create_connection(("127.0.0.1", port))
    bad.sendall(b'{"t":"auth","token":"wrong"}\n')
    good = socket.create_connection(("127.0.0.1", port))
    good.sendall(b'{"t":"auth","token":"secret"}\n')

    conn, _ = pad_bridge.accept_daemon(listener, "secret")
    conn.close()
    bad.close()
    good.close()
    listener.close()


def test_connection_check_reports_rejected_on_clean_close():
    port = free_port()
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    def reject():
        conn, _ = listener.accept()
        conn.recv(512)
        conn.close()

    thread = threading.Thread(target=reject)
    thread.start()
    outcome = pad_bridge.test_daemon_connection("127.0.0.1", port, "wrong")
    thread.join()
    listener.close()

    assert outcome == "rejected"
