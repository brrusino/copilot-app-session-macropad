# SPDX-License-Identifier: MIT
"""Serial link to the Keybow 2040 over the USB CDC *data* port.

Runs a background thread that owns connect, read and reconnect. Key events
arrive as line-delimited JSON and are handed to a callback; LED state goes the
other way.

Port discovery is by probe rather than by hardcoded COM number. CircuitPython
presents two CDC interfaces (console and data) and which one gets which COM
number is not stable across replugs, so we rank candidates by USB vendor id and
then *verify* by listening for a frame the firmware actually sends. Set
``[serial] port`` in the config to skip discovery entirely.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Callable

import serial
from serial.tools import list_ports

from .config import CIRCUITPYTHON_VIDS

log = logging.getLogger(__name__)

#: ``callback(message)`` for every decoded frame from the pad.
EventCallback = Callable[[dict], None]

PROBE_TIMEOUT = 3.5
RECONNECT_DELAY = 2.0


def registry_ports() -> list[str]:
    """Serial ports listed in the registry's SERIALCOMM map (Windows only).

    This matters for RDP. A COM port redirected into a remote session is
    registered in ``HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM`` but does **not**
    appear in pyserial's ``comports()``, which enumerates SetupAPI device
    classes. Without this, a correctly-redirected pad is invisible to discovery.

    Returns an empty list on non-Windows or if the key cannot be read.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows always has winreg
        return []

    found: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM"
        ) as key:
            index = 0
            while True:
                try:
                    _name, value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                if isinstance(value, str) and value.upper().startswith("COM"):
                    found.append(value)
                index += 1
    except OSError:
        return []
    return found


def candidate_ports() -> list[str]:
    """Plausible pad ports, best guess first.

    CircuitPython's data interface sits at a higher USB interface number than
    the console, so among ports from the same physical board we try the later
    one first.

    Ports from unrelated vendors are only considered when *nothing* matches a
    known CircuitPython vendor id. Each probe costs a multi-second timeout, so
    blindly walking every COM port on the machine would make discovery crawl --
    and most of those belong to modems, Bluetooth stacks and virtual devices
    that will never answer.
    """
    matches = []
    others = []
    seen = set()
    for info in list_ports.comports():
        seen.add(info.device.upper())
        (matches if info.vid in CIRCUITPYTHON_VIDS else others).append(info)

    def rank(info) -> tuple:
        hwid = (info.hwid or "").upper()
        # Prefer the second CDC interface, which is the data port.
        interface_hint = 1 if ("MI_02" in hwid or "MI_04" in hwid or "X.2" in hwid) else 0
        return (-interface_hint, info.device)

    if matches:
        matches.sort(key=rank)
        return [i.device for i in matches]

    # Registry-only ports are almost always RDP-redirected, which is exactly the
    # case we care about, so try them ahead of ordinary local ports.
    redirected = [p for p in registry_ports() if p.upper() not in seen]
    return redirected + [i.device for i in others]


def probe(port: str, baud: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """True if ``port`` speaks our protocol.

    The firmware emits a heartbeat unprompted, so we only have to listen; we
    also nudge it with a heartbeat in case we connected mid-cycle.
    """
    try:
        with serial.Serial(port, baud, timeout=0.2) as handle:
            try:
                handle.write(b'{"t":"hb"}\n')
            except serial.SerialException:
                pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = handle.readline()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8").strip())
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict) and "t" in message:
                    return True
    except (serial.SerialException, OSError):
        return False
    return False


class SerialLink:
    """Owns the pad connection, including reconnect after a replug."""

    def __init__(
        self,
        on_event: EventCallback,
        port: str | None = None,
        baud: int = 115200,
    ) -> None:
        self._on_event = on_event
        self._configured_port = port
        self._baud = baud
        self._serial: serial.Serial | None = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_connect: Callable[[], None] | None = None

    # -- lifecycle -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def set_on_connect(self, callback: Callable[[], None]) -> None:
        """Called after each successful connect, to push initial state."""
        self._on_connect = callback

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="macropad-serial", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # -- io --------------------------------------------------------------

    def send(self, message: dict) -> bool:
        """Write one JSON frame. Returns False if the pad is not reachable."""
        handle = self._serial
        if handle is None or not handle.is_open:
            return False
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                handle.write(payload)
            return True
        except (serial.SerialException, OSError):
            log.warning("write failed; dropping link")
            self._close()
            return False

    def _close(self) -> None:
        handle, self._serial = self._serial, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _discover(self) -> str | None:
        if self._configured_port:
            return self._configured_port if probe(self._configured_port, self._baud) else None
        for port in candidate_ports():
            if self._stop.is_set():
                return None
            log.debug("probing %s", port)
            if probe(port, self._baud):
                return port
        return None

    def _run(self) -> None:
        announced_missing = False
        while not self._stop.is_set():
            port = self._discover()
            if port is None:
                # Say this once, not every retry: an unplugged pad is a normal
                # state to sit in for hours and it must not drown the log.
                if not announced_missing:
                    log.info("keybow not found; waiting for it to appear")
                    announced_missing = True
                else:
                    log.debug("keybow still not found")
                self._stop.wait(RECONNECT_DELAY)
                continue

            try:
                self._serial = serial.Serial(port, self._baud, timeout=0.2)
            except (serial.SerialException, OSError) as exc:
                log.warning("could not open %s: %s", port, exc)
                self._stop.wait(RECONNECT_DELAY)
                continue

            announced_missing = False
            log.info("keybow connected on %s", port)
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    log.exception("on_connect handler failed")

            self._read_loop()
            self._close()
            if not self._stop.is_set():
                log.info("keybow disconnected; will reconnect")
                self._stop.wait(RECONNECT_DELAY)

    def _read_loop(self) -> None:
        handle = self._serial
        buffer = b""
        while not self._stop.is_set() and handle is not None and handle.is_open:
            try:
                chunk = handle.read(256)
            except (serial.SerialException, OSError):
                return
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                try:
                    self._on_event(message)
                except Exception:
                    log.exception("event handler failed")
