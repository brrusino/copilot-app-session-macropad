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
        assert wait_until(lambda: (root / "to-pad.jsonl").is_file())
    finally:
        link.stop()


def test_bridge_heartbeat_connects_and_fires_callback(tmp_path):
    fired = threading.Event()
    link = FolderLink(lambda _message: None, tmp_path)
    link.set_on_connect(fired.set)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad.jsonl").is_file())
        touch_alive(tmp_path)
        assert fired.wait(3)
        assert link.connected is True
    finally:
        link.stop()


def test_bridge_liveness_does_not_depend_on_mac_and_windows_clock_agreement(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad.jsonl").is_file())
        marker = tmp_path / "bridge.alive"
        marker.touch()
        future = time.time() + 3600
        os.utime(marker, (future, future))

        assert wait_until(lambda: link.connected)
    finally:
        link.stop()


def test_state_uses_a_fixed_mailbox_not_directory_entries(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad.jsonl").is_file())
        touch_alive(tmp_path)
        assert wait_until(lambda: link.connected)

        assert link.send({"t": "states", "v": ["working"]}) is True
        envelope = json.loads((tmp_path / "mailbox-states.json").read_text())
        assert envelope["m"] == {
            "t": "states",
            "v": ["working"],
        }
        assert (tmp_path / "to-pad.jsonl").read_bytes() == b""
    finally:
        link.stop()


def test_ordered_type_messages_append_to_the_fixed_log(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad.jsonl").is_file())
        touch_alive(tmp_path)
        assert wait_until(lambda: link.connected)

        assert link.send({"t": "type", "v": "ctrl+1"}) is True
        line = (tmp_path / "to-pad.jsonl").read_text().strip()
        assert json.loads(line) == {
            "t": "type",
            "v": "ctrl+1",
        }
    finally:
        link.stop()


def test_key_event_flows_from_fixed_log_to_daemon(tmp_path):
    events = []
    link = FolderLink(events.append, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-daemon.jsonl").is_file())
        touch_alive(tmp_path)
        assert wait_until(lambda: link.connected)

        event = {"t": "down", "k": 3, "role": "session", "slot": 0}
        path = tmp_path / "to-daemon.jsonl"
        path.write_text(json.dumps(event) + "\n")

        assert wait_until(lambda: events == [event])
    finally:
        link.stop()


def test_partial_event_is_completed_before_it_is_dispatched(tmp_path):
    events = []
    link = FolderLink(events.append, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-daemon.jsonl").is_file())
        touch_alive(tmp_path)
        assert wait_until(lambda: link.connected)

        path = tmp_path / "to-daemon.jsonl"
        path.write_bytes(b'{"t":"down"')
        time.sleep(0.2)
        assert events == []
        with path.open("ab") as handle:
            handle.write(b',"k":3}\n')

        assert wait_until(lambda: events == [{"t": "down", "k": 3}])
    finally:
        link.stop()


def test_stale_key_events_are_discarded_on_start(tmp_path):
    stale = tmp_path / "to-daemon.jsonl"
    stale.write_text('{"t":"down","k":3}\n')
    events = []

    link = FolderLink(events.append, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: stale.read_bytes() == b"")
        assert events == []
    finally:
        link.stop()


def test_send_fails_cleanly_without_a_live_bridge(tmp_path):
    link = FolderLink(lambda _message: None, tmp_path)
    link.start()
    try:
        assert wait_until(lambda: (tmp_path / "to-pad.jsonl").is_file())
        assert link.send({"t": "hb"}) is False
    finally:
        link.stop()
