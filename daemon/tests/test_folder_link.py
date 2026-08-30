# SPDX-License-Identifier: MIT
"""Tests for the Windows App redirected-folder pad transport."""

import json
import os
import threading
import time

from macropad_daemon.folder_link import FolderLink


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def touch_alive(root):
    marker = root / "bridge.alive"
    marker.touch()
    os.utime(marker, None)


def test_waits_until_the_redirected_folder_exists(tmp_path):
    root = tmp_path / "not-mounted-yet"
    link = FolderLink(lambda _message: None, root)
    link.start()
    try:
        assert link.connected is False
        root.mkdir()
        assert wait_until(lambda: (root / "to-pad").is_dir())
    finally:
        link.stop()


def test_bridge_heartbeat_connects_and_fires_callback(tmp_path):
    fired = threading.Event()
    link = FolderLink(lambda _message: None, tmp_path)
    link.set_on_connect(fired.set)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad").is_dir())
        touch_alive(tmp_path)
        assert fired.wait(3)
        assert link.connected is True
    finally:
        link.stop()


def test_bridge_liveness_does_not_depend_on_mac_and_windows_clock_agreement(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad").is_dir())
        marker = tmp_path / "bridge.alive"
        marker.touch()
        future = time.time() + 3600
        os.utime(marker, (future, future))

        assert wait_until(lambda: link.connected)
    finally:
        link.stop()


def test_state_flows_from_daemon_to_folder(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad").is_dir())
        touch_alive(tmp_path)
        assert wait_until(lambda: link.connected)

        assert link.send({"t": "states", "v": ["working"]}) is True
        files = list((tmp_path / "to-pad").glob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text()) == {
            "t": "states",
            "v": ["working"],
        }
    finally:
        link.stop()


def test_key_event_flows_from_folder_to_daemon(tmp_path):
    events = []
    link = FolderLink(events.append, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-daemon").is_dir())
        touch_alive(tmp_path)
        assert wait_until(lambda: link.connected)

        event = {"t": "down", "k": 3, "role": "session", "slot": 0}
        path = tmp_path / "to-daemon" / "event.json"
        path.write_text(json.dumps(event))

        assert wait_until(lambda: events == [event])
        assert not path.exists()
    finally:
        link.stop()


def test_stale_key_events_are_discarded_on_start(tmp_path):
    inbox = tmp_path / "to-daemon"
    inbox.mkdir()
    stale = inbox / "stale.json"
    stale.write_text('{"t":"down","k":3}')
    events = []

    link = FolderLink(events.append, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: not stale.exists())
        assert events == []
    finally:
        link.stop()


def test_send_fails_cleanly_without_a_live_bridge(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad").is_dir())
        assert link.send({"t": "hb"}) is False
    finally:
        link.stop()
