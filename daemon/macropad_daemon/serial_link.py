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

#: How long to wait for the pad to say something after we open its data port,
#: before trying to restart its firmware over the console port.
SILENT_PAD_TIMEOUT = 4.0

#: How long a pad that HAS been talking may go quiet before we restart it.
#: The firmware heartbeats every 2s, so silence well past that means it died or
#: was interrupted into the REPL. Generous enough to absorb a slow RDP link.
STALLED_PAD_TIMEOUT = 15.0

#: CircuitPython's REPL: Ctrl-C interrupts, Ctrl-D reloads code.py.
REPL_RELOAD = b"\x04"


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


#: Outcomes of probing a candidate port. A busy port must be distinguished from
#: an absent one: they look identical to a bool-returning probe, but they need
#: opposite responses from the user ("free the port" vs "plug the pad in").
PROBE_PAD = "pad"
PROBE_SILENT = "silent"
PROBE_BUSY = "busy"


def _is_access_denied(exc: BaseException) -> bool:
    return "Access is denied" in str(exc) or getattr(exc, "errno", None) == 13


def probe_port(port: str, baud: int, timeout: float = PROBE_TIMEOUT) -> str:
    """Classify ``port`` as the pad, silent, or busy.

    Deliberately writes nothing. The firmware emits an unsolicited heartbeat
    every couple of seconds, so listening alone is sufficient -- and writing
    first is actively harmful: CircuitPython's REPL console echoes whatever it
    receives, so a probe that sent ``{"t":"hb"}`` and accepted any valid reply
    would match the *console* port on its own echo. Both ports are redirected
    over RDP, so that false positive is not hypothetical; it would leave the
    daemon talking to a Python prompt instead of the firmware.
    """
    try:
        with serial.Serial(port, baud, timeout=0.2) as handle:
            # Asserting DTR marks the port "connected" to CircuitPython, which
            # is what allows the firmware to start writing to it.
            try:
                handle.dtr = True
            except (OSError, serial.SerialException):
                pass

            # Poll in_waiting rather than calling readline(). readline() uses
            # the blocking overlapped-IO path, which fails outright on an
            # RDP-redirected port even while the pad is happily transmitting --
            # so a probe built on it reports "no response" for a working pad.
            buffer = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    waiting = handle.in_waiting
                except (OSError, serial.SerialException):
                    return PROBE_SILENT
                if not waiting:
                    time.sleep(0.05)
                    continue
                buffer += handle.read(waiting)
                while b"\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n")
                    try:
                        message = json.loads(raw.decode("utf-8").strip())
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(message, dict) and message.get("t") in ("hb", "hello"):
                        return PROBE_PAD
    except (serial.SerialException, OSError) as exc:
        return PROBE_BUSY if _is_access_denied(exc) else PROBE_SILENT
    return PROBE_SILENT


def probe(port: str, baud: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """True if ``port`` is the pad's *data* port."""
    return probe_port(port, baud, timeout) == PROBE_PAD


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
        self._busy_ports: list[str] = []

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
            # Trust an explicitly configured port rather than probing it.
            #
            # Probing opens and closes the port, and an RDP-redirected port does
            # not reliably survive that -- the next read fails with EOF until the
            # redirection re-establishes. Pinning the port is precisely how a
            # user says "stop guessing", so honour it and let the read loop
            # handle a port that turns out to be wrong.
            return self._configured_port
        self._busy_ports = []
        for port in candidate_ports():
            if self._stop.is_set():
                return None
            log.debug("probing %s", port)
            outcome = probe_port(port, self._baud)
            if outcome == PROBE_PAD:
                return port
            if outcome == PROBE_BUSY:
                # Record rather than ignore: a busy port is the single most
                # likely reason the pad is present but unreachable, and calling
                # it "not found" sends the user looking for the wrong problem.
                self._busy_ports.append(port)
        return None

    def _run(self) -> None:
        announced_missing = False
        while not self._stop.is_set():
            port = self._discover()
            if port is None:
                if self._busy_ports:
                    # Re-announce this one on every retry. Unlike an absent pad,
                    # a wedged port never clears itself, so staying quiet about
                    # it just leaves the pad looking broken.
                    log.warning(
                        "%s open but busy; the pad cannot be reached. Another "
                        "process may hold it, or the RDP COM redirection wedged "
                        "(a process died holding it). Replug the pad, or "
                        "reconnect the RDP session, to reset it.",
                        ", ".join(self._busy_ports),
                    )
                    announced_missing = False
                elif not announced_missing:
                    # Say this once, not every retry: an unplugged pad is a normal
                    # state to sit in for hours and it must not drown the log.
                    log.info("keybow not found; waiting for it to appear")
                    announced_missing = True
                else:
                    log.debug("keybow still not found")
                self._stop.wait(RECONNECT_DELAY)
                continue

            try:
                self._serial = serial.Serial(port, self._baud, timeout=0.2)
                # CircuitPython only writes to a data port it considers
                # connected, and that reflects DTR. Without this the pad stays
                # silent even though the link is otherwise fine.
                try:
                    self._serial.dtr = True
                except (OSError, serial.SerialException):
                    pass
            except (serial.SerialException, OSError) as exc:
                if "Access is denied" in str(exc) or getattr(exc, "errno", None) == 13:
                    # Either another process holds it, or -- common with an
                    # RDP-redirected port -- the redirection wedged because a
                    # process died while holding it open. Reconnecting the RDP
                    # session clears it.
                    log.warning(
                        "%s is busy: another process may hold it, or the RDP "
                        "redirection has wedged. Reconnect the RDP session to reset it.",
                        port,
                    )
                else:
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

    def _kick_console(self, data_port: str) -> bool:
        """Restart the pad's firmware via its REPL console port.

        Two situations need this, and both are normal rather than exceptional:

        * The pad is sitting at the REPL, because someone interrupted it or an
          earlier run finished. Without a nudge it stays there indefinitely.
        * Older firmware gated its writes on DTR, which an RDP-redirected port
          never delivers mid-run. Restarting the firmware while we already hold
          the data port open means DTR is high from its very first write.

        A CircuitPython board exposes two CDC interfaces; the one we are not
        using for data is the console. Sending Ctrl-D to anything else is
        harmless -- our own protocol simply ignores an unparseable byte.
        """
        for candidate in registry_ports() + [i.device for i in list_ports.comports()]:
            if candidate.upper() == data_port.upper():
                continue
            try:
                with serial.Serial(candidate, self._baud, timeout=0.3) as console:
                    try:
                        console.dtr = True
                    except (OSError, serial.SerialException):
                        pass
                    console.write(REPL_RELOAD)
                    console.flush()
                log.info("nudged %s to restart the pad firmware", candidate)
                return True
            except (serial.SerialException, OSError):
                continue
        return False

    def _read_loop(self) -> None:
        handle = self._serial
        buffer = b""
        started = time.monotonic()
        last_heard = started
        heard_anything = False
        kicked = False

        while not self._stop.is_set() and handle is not None and handle.is_open:
            try:
                # Poll in_waiting rather than issuing a blocking read.
                #
                # A blocking read on an RDP-redirected COM port fails with
                # "GetOverlappedResult failed / Reached the end of the file"
                # even when the link is healthy, which made the daemon drop and
                # reconnect roughly once a second. Reading only what is already
                # buffered avoids the overlapped-IO path entirely.
                waiting = handle.in_waiting
                if not waiting:
                    now = time.monotonic()
                    if not heard_anything and not kicked and (now - started) > SILENT_PAD_TIMEOUT:
                        # The port is open but the pad has said nothing. It is
                        # most likely parked at the REPL; restart its firmware
                        # now that we already hold the data port.
                        kicked = True
                        log.info("pad is silent; trying to restart its firmware")
                        self._kick_console(handle.port)
                    elif heard_anything and (now - last_heard) > STALLED_PAD_TIMEOUT:
                        # It was talking and stopped. The firmware has died or
                        # been interrupted into the REPL -- the link itself is
                        # fine, so reconnecting would not help and the pad would
                        # stay dark indefinitely. Restart it in place.
                        log.info(
                            "pad stopped sending for %.0fs; restarting its firmware",
                            now - last_heard,
                        )
                        last_heard = now
                        self._kick_console(handle.port)
                    time.sleep(0.02)
                    continue
                chunk = handle.read(waiting)
            except (serial.SerialException, OSError):
                return
            if not chunk:
                continue
            heard_anything = True
            last_heard = time.monotonic()
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
