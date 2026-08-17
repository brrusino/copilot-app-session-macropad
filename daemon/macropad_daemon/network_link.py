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
    """Accepts a single pad bridge connection and speaks the pad protocol."""

    def __init__(
        self,
        on_event: EventCallback,
        host: str,
        port: int,
        token: str,
    ) -> None:
        self._on_event = on_event
        self.host = host
        self.port = port
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
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        listener.bind((self.host, self.port))
        listener.listen(1)
        listener.settimeout(0.5)
        self._listener = listener
        self._stop.clear()
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
