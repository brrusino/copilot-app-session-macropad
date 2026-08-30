# SPDX-License-Identifier: MIT
"""Tests for using redirected serial and the network bridge together."""

import pytest

from macropad_daemon.multi_link import MultiLink


class FakeLink:
    def __init__(self, connected=False, fails_to_start=False):
        self.connected = connected
        self.fails_to_start = fails_to_start
        self.started = False
        self.stopped = False
        self.sent = []
        self.on_connect = None

    def set_on_connect(self, callback):
        self.on_connect = callback

    def start(self):
        if self.fails_to_start:
            raise RuntimeError("start failed")
        self.started = True

    def stop(self):
        self.stopped = True

    def send(self, message):
        self.sent.append(message)
        return self.connected


def test_connected_when_either_transport_is_connected():
    serial = FakeLink()
    network = FakeLink(connected=True)
    assert MultiLink((serial, network)).connected is True


def test_disconnected_when_neither_transport_is_connected():
    assert MultiLink((FakeLink(), FakeLink())).connected is False


def test_output_is_broadcast_to_both_transports():
    serial = FakeLink(connected=True)
    network = FakeLink()
    link = MultiLink((serial, network))

    assert link.send({"t": "states", "v": ["idle"]}) is True
    assert serial.sent == [{"t": "states", "v": ["idle"]}]
    assert network.sent == [{"t": "states", "v": ["idle"]}]


def test_send_reports_failure_when_nothing_received_it():
    assert MultiLink((FakeLink(), FakeLink())).send({"t": "hb"}) is False


def test_each_transport_gets_the_connect_callback():
    serial = FakeLink()
    network = FakeLink()
    callback = lambda: None

    MultiLink((serial, network)).set_on_connect(callback)

    assert serial.on_connect is callback
    assert network.on_connect is callback


def test_start_and_stop_cover_both_transports():
    serial = FakeLink()
    network = FakeLink()
    link = MultiLink((serial, network))

    link.start()
    link.stop()

    assert serial.started and serial.stopped
    assert network.started and network.stopped


def test_partial_start_is_rolled_back_and_reported():
    serial = FakeLink()
    network = FakeLink(fails_to_start=True)
    link = MultiLink((serial, network))

    with pytest.raises(RuntimeError, match="start failed"):
        link.start()

    assert serial.stopped is True
