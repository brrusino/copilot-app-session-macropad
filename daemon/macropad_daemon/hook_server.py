# SPDX-License-Identifier: MIT
"""HTTP receiver for Copilot CLI hook events.

The hook commands installed into ``~/.copilot/hooks/macropad.json`` POST their
stdin payload here, one request per event. The payload carries ``sessionId``,
which is what lets us attribute an event to a LED slot.

Two properties matter more than anything else here:

**Never block the agent.** A hook runs inline with the agent's turn. This server
answers immediately with 204 and does the real work on the caller's thread after
responding, and the hook commands themselves use short curl timeouts and always
exit 0. If the daemon is down, the agent must not even notice.

**Observe only.** The ``permissionRequest`` hook is capable of returning an
allow/deny decision on stdout, and Agency already registers a handler on that
event with a long timeout. We deliberately return *nothing* -- two hooks both
emitting decisions for the same request is undefined behaviour, and stealing
approvals from Agency is not our business. We watch it purely to turn a LED
amber.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)

#: ``callback(event_type, session_id, payload)``
HookCallback = Callable[[str, str, dict], None]

MAX_BODY_BYTES = 1 << 20


class _Handler(BaseHTTPRequestHandler):
    server_version = "CopilotMacropad/1.0"

    # Silence the default stderr access log.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("hook http: " + fmt, *args)
    def _no_content(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        # Health probe so hook commands can cheaply skip work when we're down.
        if self.path.rstrip("/").endswith("/health"):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._no_content(404)

    def do_POST(self) -> None:  # noqa: N802
        parts = [p for p in self.path.split("/") if p]
        event_type = parts[-1] if parts else ""

        try:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""

        # Answer first: the agent is blocked until this returns.
        self._no_content()

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        session_id = ""
        if isinstance(payload, dict):
            session_id = str(
                payload.get("sessionId") or payload.get("session_id") or ""
            )

        callback: HookCallback | None = getattr(self.server, "hook_callback", None)
        if callback and event_type:
            try:
                callback(event_type, session_id, payload if isinstance(payload, dict) else {})
            except Exception:
                log.exception("hook callback failed for %s", event_type)


class _Server(ThreadingHTTPServer):
    """HTTP server that refuses to share its port.

    Python sets ``allow_reuse_address`` by default, and on Windows that flag
    lets a second process bind a port another process is *already listening on*.
    The newcomer looks perfectly healthy while the stale instance quietly keeps
    receiving every request -- so the LEDs stop responding and nothing anywhere
    reports an error.

    Turning it off makes a duplicate daemon fail loudly at startup instead.
    """

    allow_reuse_address = False
    daemon_threads = True


class PortInUseError(RuntimeError):
    """Raised when another macropad daemon already owns the hook port."""


class HookServer:
    """Threaded HTTP server carrying hook events into the daemon."""

    def __init__(self, host: str, port: int, callback: HookCallback) -> None:
        self.host = host
        self.port = port
        self._callback = callback
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            server = _Server((self.host, self.port), _Handler)
        except OSError as exc:
            raise PortInUseError(
                f"cannot bind {self.host}:{self.port} -- another macropad daemon "
                f"is probably already running ({exc})"
            ) from exc
        server.hook_callback = self._callback  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="macropad-hooks",
            daemon=True,
        )
        self._thread.start()
        log.info("hook receiver listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
