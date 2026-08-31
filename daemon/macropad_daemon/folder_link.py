# SPDX-License-Identifier: MIT
"""Pad transport over a folder redirected through Windows App.

Windows App on macOS forwards keyboard input but not serial/COM devices. It
does, however, expose a selected Mac folder as a bidirectional network drive in
the remote session. This transport uses only fixed file names in that folder.

Directory enumeration through the redirected filesystem is both slow and, on
the live Windows App path, can hang or return malformed names. Recurring state
therefore uses pre-existing fixed-size mailboxes, while ordered messages use
two append-only JSONL files.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

EventCallback = Callable[[dict], None]

# Kept below the existing 0.18-second action-key flash, so a host action starts
# while the physical confirmation is still visible.
POLL_INTERVAL = 0.1

# The pad firmware considers a two-second heartbeat stale after six seconds.
# Use the same contract for deciding whether the Mac bridge is present.
BRIDGE_ALIVE_TIMEOUT = 6.0

# The firmware's receive buffer is 4096 bytes. Fixed-size mailbox files use the
# same protocol boundary and are padded with JSON-safe whitespace.
MAILBOX_SIZE = 4096
MAILBOX_TYPES = ("states", "hb", "focus", "palette", "brightness", "levels")


class FolderLink:
    """Carries the pad protocol through a redirected directory."""

    def __init__(self, on_event: EventCallback, root: Path) -> None:
        self._on_event = on_event
        self.root = Path(root)
        self._to_pad = self.root / "to-pad.jsonl"
        self._to_daemon = self.root / "to-daemon.jsonl"
        self._alive = self.root / "bridge.alive"
        self._connected = False
        self._prepared = False
        self._alive_signature: int | None = None
        self._last_bridge_seen = 0.0
        self._inbound_offset = 0
        self._inbound_buffer = b""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_connect: Callable[[], None] | None = None
        self._write_lock = threading.Lock()
        self._sequence = 0
        self._run_id = secrets.token_hex(8)

    @property
    def connected(self) -> bool:
        return self._connected

    def set_on_connect(self, callback: Callable[[], None]) -> None:
        self._on_connect = callback

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="macropad-folder", daemon=True
        )
        self._thread.start()
        log.info("pad bridge folder: waiting for %s", self.root)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._connected = False

    def send(self, message: dict) -> bool:
        if not self._connected:
            return False
        try:
            if message.get("t") in MAILBOX_TYPES:
                self._write_mailbox(message)
            else:
                payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
                self._append_frame(self._to_pad, payload)
            return True
        except OSError:
            log.warning("redirected-folder write failed; waiting for it to return")
            self._connected = False
            self._prepared = False
            return False

    def _append_frame(self, path: Path, payload: bytes) -> None:
        with self._write_lock:
            with path.open("ab", buffering=0) as handle:
                handle.write(payload + b"\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _write_mailbox(self, message: dict) -> None:
        with self._write_lock:
            self._sequence += 1
            envelope = {
                "r": self._run_id,
                "q": self._sequence,
                "m": message,
            }
            payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            if len(payload) >= MAILBOX_SIZE:
                with self._to_pad.open("ab", buffering=0) as handle:
                    handle.write(
                        json.dumps(message, separators=(",", ":")).encode("utf-8")
                        + b"\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                return
            payload += b"\n" + (b" " * (MAILBOX_SIZE - len(payload) - 1))
            path = self.root / f"mailbox-{message['t']}.json"
            with path.open("r+b", buffering=0) as handle:
                handle.seek(0)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

    def _prepare(self) -> bool:
        if not self.root.is_dir():
            return False
        try:
            # Truncating fixed paths avoids directory enumeration and prevents
            # old key events or typed commands from replaying after reconnect.
            self._to_daemon.write_bytes(b"")
            self._to_pad.write_bytes(b"")
            for kind in MAILBOX_TYPES:
                (self.root / f"mailbox-{kind}.json").write_bytes(b" " * MAILBOX_SIZE)
            try:
                self._alive.unlink()
            except FileNotFoundError:
                pass
            self._alive_signature = None
            self._last_bridge_seen = 0.0
            self._inbound_offset = 0
            self._inbound_buffer = b""
            self._prepared = True
            return True
        except OSError:
            return False

    def _bridge_is_alive(self) -> bool:
        try:
            signature = self._alive.stat().st_mtime_ns
        except OSError:
            return False
        if signature != self._alive_signature:
            self._alive_signature = signature
            self._last_bridge_seen = time.monotonic()
        return (
            self._last_bridge_seen > 0.0
            and (time.monotonic() - self._last_bridge_seen) <= BRIDGE_ALIVE_TIMEOUT
        )

    def _consume_events(self) -> None:
        size = self._to_daemon.stat().st_size
        if size < self._inbound_offset:
            self._inbound_offset = 0
            self._inbound_buffer = b""
        if size == self._inbound_offset:
            return

        with self._to_daemon.open("rb") as handle:
            handle.seek(self._inbound_offset)
            chunk = handle.read()
        self._inbound_offset += len(chunk)
        self._inbound_buffer += chunk

        while b"\n" in self._inbound_buffer:
            raw, _, self._inbound_buffer = self._inbound_buffer.partition(b"\n")
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
                log.exception("folder bridge event handler failed")

    def _run(self) -> None:
        announced_missing = False
        while not self._stop.is_set():
            if not self._prepared and not self._prepare():
                if not announced_missing:
                    log.info("redirected pad folder not available: %s", self.root)
                    announced_missing = True
                self._stop.wait(POLL_INTERVAL)
                continue

            announced_missing = False
            alive = self._bridge_is_alive()
            if alive and not self._connected:
                self._connected = True
                log.info("pad bridge connected through redirected folder")
                if self._on_connect:
                    try:
                        self._on_connect()
                    except Exception:
                        log.exception("on_connect handler failed")
            elif not alive and self._connected:
                self._connected = False
                log.info("redirected-folder pad bridge disconnected")

            try:
                self._consume_events()
            except OSError:
                self._connected = False
                self._prepared = False

            self._stop.wait(POLL_INTERVAL)
