# SPDX-License-Identifier: MIT
"""Tests for the supervised Microsoft Dev Tunnel host."""

import threading

import pytest

from macropad_daemon import dev_tunnel


class FakeProcess:
    def __init__(self):
        self.terminated = threading.Event()

    def poll(self):
        return 0 if self.terminated.is_set() else None

    def terminate(self):
        self.terminated.set()

    def kill(self):
        self.terminated.set()

    def wait(self, timeout=None):
        self.terminated.wait(timeout)
        return 0


def test_missing_cli_fails_before_starting_a_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_tunnel.shutil, "which", lambda _command: None)
    host = dev_tunnel.DevTunnelHost(
        "tunnel", 7831, tmp_path / "tunnel.log", "missing"
    )

    with pytest.raises(FileNotFoundError, match="command not found"):
        host.start()


def test_host_process_is_started_and_stopped_with_the_daemon(tmp_path, monkeypatch):
    started = threading.Event()
    calls = []
    process = FakeProcess()

    monkeypatch.setattr(dev_tunnel.shutil, "which", lambda _command: "devtunnel.exe")
    monkeypatch.setattr(
        dev_tunnel.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        started.set()
        return process

    monkeypatch.setattr(dev_tunnel.subprocess, "Popen", popen)
    host = dev_tunnel.DevTunnelHost(
        "pad-tunnel", 7831, tmp_path / "tunnel.log"
    )

    host.start()
    assert started.wait(3)
    host.stop()

    assert calls[0][0] == ["devtunnel.exe", "host", "pad-tunnel"]
    assert process.terminated.is_set()


def test_missing_tunnel_is_recreated_with_anonymous_access_and_port(tmp_path, monkeypatch):
    calls = []
    results = iter((1, 0, 1, 0))
    monkeypatch.setattr(dev_tunnel.shutil, "which", lambda _command: "devtunnel.exe")

    def run(args, **_kwargs):
        calls.append(args)
        return type("Result", (), {"returncode": next(results)})()

    monkeypatch.setattr(dev_tunnel.subprocess, "run", run)
    monkeypatch.setattr(dev_tunnel.threading.Thread, "start", lambda _self: None)
    host = dev_tunnel.DevTunnelHost(
        "pad-tunnel.usw3", 7831, tmp_path / "tunnel.log"
    )

    host.start()

    assert calls[0][-3:] == ["pad-tunnel.usw3", "--expiration", "30d"]
    assert calls[1][1:3] == ["create", "pad-tunnel"]
    assert "--allow-anonymous" in calls[1]
    assert calls[2][1:4] == ["port", "show", "pad-tunnel.usw3"]
    assert calls[3][1:4] == ["port", "create", "pad-tunnel.usw3"]
