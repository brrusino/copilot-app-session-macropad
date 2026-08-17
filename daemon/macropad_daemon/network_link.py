# SPDX-License-Identifier: MIT
"""Network transport for a pad attached to a different machine.

Why this exists
---------------
The daemon has to run where the Copilot app's state lives, but the pad plugs in
wherever you physically are. Over RDP those are different machines: the app and
``data.db`` sit on the remote host while the Keybow enumerates on the local
client. RDP *can* redirect COM ports, but redirection of USB-CDC composite
devices is unreliable, so this provides a path that doesn't depend on it.

:mod:`scripts.pad_bridge` runs on the machine holding the pad, reads its serial
port, and relays the same line-delimited JSON to this listener. Because the
client already reaches the host for RDP itself, an outbound connection from
bridge to daemon works without any inbound firewall change on the client.

This class is interface-compatible with :class:`~.serial_link.SerialLink`, so
:mod:`.main` treats the two identically.

Access control
--------------
Unlike the serial transport this opens a real network socket, so the bridge must
present a shared token as its first frame. Connections that fail to authenticate
are dropped without a reply.

Connection direction
--------------------
Two modes, because which end can accept a connection depends on the network:

``listen`` (default)
    The daemon listens and the bridge dials in. Simplest when the daemon's
    machine accepts inbound connections.

``connect``
    The daemon dials out to a listener on the bridge machine. Needed when the
    daemon runs somewhere that cannot accept inbound connections -- a Cloud PC
    or VM behind a gateway, or any host whose firewall you cannot edit because
    you are not an administrator on it. Outbound is almost always permitted
    where inbound is not.

The authentication handshake is identical in both directions: whichever side
dials out sends the token first.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

EventCallback = Callable[[dict], None]

TOKEN_FILENAME = "macropad.token"
AUTH_TIMEOUT = 5.0
DIAL_RETRY_DELAY = 3.0


def load_or_create_token(copilot_home: Path) -> str:
    """Return the shared bridge token, generating one on first use."""
    path = Path(copilot_home) / TOKEN_FILENAME
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    log.info("generated bridge token at %s", path)
    return token


class NetworkLink:
    """Carries the pad protocol over TCP, in either connection direction."""

    def __init__(
        self,
        on_event: EventCallback,
        host: str,
        port: int,
        token: str,
        mode: str = "listen",
    ) -> None:
        if mode not in ("listen", "connect"):
            raise ValueError(f"mode must be 'listen' or 'connect', got {mode!r}")
        self._on_event = on_event
        self.host = host
        self.port = port
        self.mode = mode
        self._token = token
        self._listener: socket.socket | None = None
        self._client: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_connect: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def set_on_connect(self, callback: Callable[[], None]) -> None:
        self._on_connect = callback

    def start(self) -> None:
        self._stop.clear()
        if self.mode == "connect":
            self._thread = threading.Thread(
                target=self._dial_loop, name="macropad-net", daemon=True
            )
            self._thread.start()
            log.info("pad bridge: dialling %s:%d", self.host, self.port)
            return

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        listener.bind((self.host, self.port))
        listener.listen(1)
        listener.settimeout(0.5)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="macropad-net", daemon=True)
        self._thread.start()
        log.info("pad bridge listener on %s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        self._drop_client()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # -- io --------------------------------------------------------------

    def send(self, message: dict) -> bool:
        client = self._client
        if client is None:
            return False
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                client.sendall(payload)
            return True
        except OSError:
            log.warning("bridge write failed; dropping connection")
            self._drop_client()
            return False

    def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def _dial_loop(self) -> None:
        """Outbound mode: keep trying to reach the bridge's listener.

        The daemon is the one that authenticates here, since it is the side
        dialling out.
        """
        announced_down = False
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=10)
            except OSError as exc:
                if not announced_down:
                    log.info("pad bridge not reachable at %s:%d (%s); retrying",
                             self.host, self.port, exc)
                    announced_down = True
                else:
                    log.debug("pad bridge still unreachable")
                self._stop.wait(DIAL_RETRY_DELAY)
                continue

            try:
                sock.sendall(
                    json.dumps({"t": "auth", "token": self._token}).encode() + b"\n"
                )
            except OSError:
                sock.close()
                self._stop.wait(DIAL_RETRY_DELAY)
                continue

            announced_down = False
            sock.settimeout(0.5)
            self._client = sock
            log.info("pad bridge connected to %s:%d", self.host, self.port)
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    log.exception("on_connect handler failed")

            self._read_loop(sock)
            self._drop_client()
            if not self._stop.is_set():
                log.info("pad bridge disconnected; will redial")
                self._stop.wait(DIAL_RETRY_DELAY)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            if not self._authenticate(client, address):
                continue

            # A newer bridge replaces an older one rather than fighting it.
            self._drop_client()
            client.settimeout(0.5)
            self._client = client
            log.info("pad bridge connected from %s", address[0])
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    log.exception("on_connect handler failed")

            self._read_loop(client)
            if self._client is client:
                self._drop_client()
                log.info("pad bridge disconnected")

    def _authenticate(self, client: socket.socket, address) -> bool:
        client.settimeout(AUTH_TIMEOUT)
        try:
            buffer = b""
            while b"\n" not in buffer and len(buffer) < 4096:
                chunk = client.recv(256)
                if not chunk:
                    break
                buffer += chunk
            line, _, _ = buffer.partition(b"\n")
            message = json.loads(line.decode("utf-8"))
            presented = str(message.get("token", ""))
        except (OSError, ValueError, UnicodeDecodeError):
            log.warning("bridge from %s failed to authenticate", address[0])
            client.close()
            return False

        # Constant-time compare so the token can't be probed byte by byte.
        if not secrets.compare_digest(presented, self._token):
            log.warning("bridge from %s presented a bad token", address[0])
            client.close()
            return False
        return True

    def _read_loop(self, client: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = client.recv(512)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
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
