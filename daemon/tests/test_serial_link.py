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


def test_falls_back_to_everything_when_no_vendor_matches(fake_ports, monkeypatch):
    """An unexpected vendor id must not make the pad undiscoverable."""
    monkeypatch.setattr(serial_link, "registry_ports", lambda: [])
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


def test_no_ports_at_all(fake_ports, monkeypatch):
    monkeypatch.setattr(serial_link, "registry_ports", lambda: [])
    fake_ports([])
    assert serial_link.candidate_ports() == []


def test_known_vids_cover_rp2040_and_adafruit():
    assert 0x2E8A in CIRCUITPYTHON_VIDS
    assert 0x239A in CIRCUITPYTHON_VIDS


# --- RDP-redirected ports -------------------------------------------------
# A COM port redirected into a remote session is registered in
# HKLM\HARDWARE\DEVICEMAP\SERIALCOMM but does NOT appear in pyserial's
# comports(), so discovery has to consult the registry too.


def test_registry_only_ports_are_probed(fake_ports, monkeypatch):
    fake_ports([port("COM2")])
    monkeypatch.setattr(serial_link, "registry_ports", lambda: ["COM3"])
    assert serial_link.candidate_ports() == ["COM3", "COM2"]


def test_redirected_ports_are_tried_first(fake_ports, monkeypatch):
    """Redirected ports are the interesting case, so probe them before locals."""
    fake_ports([port("COM1"), port("COM2")])
    monkeypatch.setattr(serial_link, "registry_ports", lambda: ["COM9"])
    assert serial_link.candidate_ports()[0] == "COM9"


def test_registry_port_not_duplicated_when_also_enumerated(fake_ports, monkeypatch):
    fake_ports([port("COM3")])
    monkeypatch.setattr(serial_link, "registry_ports", lambda: ["COM3"])
    assert serial_link.candidate_ports() == ["COM3"]


def test_registry_ignored_when_a_real_board_is_present(fake_ports, monkeypatch):
    """A directly-attached CircuitPython board still wins."""
    fake_ports([port("COM7", vid=RP2040_VID)])
    monkeypatch.setattr(serial_link, "registry_ports", lambda: ["COM3"])
    assert serial_link.candidate_ports() == ["COM7"]


def test_registry_ports_is_safe_off_windows(monkeypatch):
    monkeypatch.setattr(serial_link.sys, "platform", "linux")
    assert serial_link.registry_ports() == []


# --- probe ----------------------------------------------------------------


class FakePort:
    """Minimal stand-in for serial.Serial, recording anything written."""

    def __init__(self, lines, echo=False):
        self._lines = list(lines)
        self.written = []
        self.dtr = False
        self.echo = echo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        self.written.append(data)
        if self.echo:
            self._lines.append(data)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


def test_probe_never_writes(monkeypatch):
    """Writing to the pad's REPL console would execute Python on the device."""
    fake = FakePort([b'{"t":"hb"}\n'])
    monkeypatch.setattr(serial_link.serial, "Serial", lambda *a, **k: fake)
    assert serial_link.probe("COM9", 115200) is True
    assert fake.written == []


def test_probe_asserts_dtr(monkeypatch):
    """CircuitPython only writes to a data port it considers connected."""
    fake = FakePort([b'{"t":"hb"}\n'])
    monkeypatch.setattr(serial_link.serial, "Serial", lambda *a, **k: fake)
    serial_link.probe("COM9", 115200)
    assert fake.dtr is True


def test_probe_accepts_hello(monkeypatch):
    fake = FakePort([b'{"t":"hello","fw":1,"slots":8}\n'])
    monkeypatch.setattr(serial_link.serial, "Serial", lambda *a, **k: fake)
    assert serial_link.probe("COM9", 115200) is True


def test_probe_rejects_an_echoing_repl(monkeypatch):
    """The console port echoes input; that must not look like the pad.

    Regression test for a real false positive: both the pad's console and data
    ports are redirected over RDP, and a probe that wrote a heartbeat then
    accepted any valid JSON would match the console's echo of its own message.
    """
    fake = FakePort([b"Adafruit CircuitPython 9.1.4\n", b">>> \n"], echo=True)
    monkeypatch.setattr(serial_link.serial, "Serial", lambda *a, **k: fake)
    assert serial_link.probe("COM9", 115200, timeout=0.3) is False


def test_probe_ignores_other_json(monkeypatch):
    fake = FakePort([b'{"t":"down","k":3}\n'])
    monkeypatch.setattr(serial_link.serial, "Serial", lambda *a, **k: fake)
    assert serial_link.probe("COM9", 115200, timeout=0.3) is False


def test_probe_survives_garbage(monkeypatch):
    fake = FakePort([b"\xff\xfe not json\n", b'{"t":"hb"}\n'])
    monkeypatch.setattr(serial_link.serial, "Serial", lambda *a, **k: fake)
    assert serial_link.probe("COM9", 115200) is True


def test_probe_returns_false_when_port_cannot_open(monkeypatch):
    def boom(*a, **k):
        raise serial_link.serial.SerialException("nope")

    monkeypatch.setattr(serial_link.serial, "Serial", boom)
    assert serial_link.probe("COM9", 115200) is False
