# SPDX-License-Identifier: MIT
"""Tests for pad discovery ordering."""

from types import SimpleNamespace

import pytest

from macropad_daemon import serial_link
from macropad_daemon.config import CIRCUITPYTHON_VIDS

RP2040_VID = 0x2E8A


def port(device, vid=None, hwid=""):
    return SimpleNamespace(device=device, vid=vid, hwid=hwid)


@pytest.fixture
def fake_ports(monkeypatch):
    def install(ports):
        monkeypatch.setattr(serial_link.list_ports, "comports", lambda: ports)

    return install


def test_unrelated_ports_skipped_when_a_board_matches(fake_ports):
    """Each probe costs seconds, so don't walk the whole machine's COM ports."""
    fake_ports([port("COM2"), port("COM7", vid=RP2040_VID)])
    assert serial_link.candidate_ports() == ["COM7"]


def test_falls_back_to_everything_when_no_vendor_matches(fake_ports):
    """An unexpected vendor id must not make the pad undiscoverable."""
    fake_ports([port("COM2"), port("COM3")])
    assert serial_link.candidate_ports() == ["COM2", "COM3"]


def test_data_interface_preferred_over_console(fake_ports):
    """CircuitPython's second CDC interface is the data port we want."""
    fake_ports(
        [
            port("COM7", vid=RP2040_VID, hwid="USB VID:PID=2E8A:100A MI_00"),
            port("COM8", vid=RP2040_VID, hwid="USB VID:PID=2E8A:100A MI_02"),
        ]
    )
    assert serial_link.candidate_ports()[0] == "COM8"


def test_no_ports_at_all(fake_ports):
    fake_ports([])
    assert serial_link.candidate_ports() == []


def test_known_vids_cover_rp2040_and_adafruit():
    assert 0x2E8A in CIRCUITPYTHON_VIDS
    assert 0x239A in CIRCUITPYTHON_VIDS
