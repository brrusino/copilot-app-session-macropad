# SPDX-License-Identifier: MIT
"""Read-only companion navigation feed, independent of daemon state.

``CompanionFeed(callback).start()`` discovers the running app and delivers full
snapshots on a worker thread. Callbacks should return promptly; ``stop()`` waits
for that thread. ``latest`` remains available while disconnected. Revisions are
ordered within an app instance, not across app restarts.
"""

from __future__ import annotations

import asyncio
import copy
import ctypes
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.client import connect

log = logging.getLogger(__name__)
# WebSocket debug logs include handshake headers. Never pass them to root logs.
_wire_log = logging.Logger(__name__ + ".wire")
_wire_log.addHandler(logging.NullHandler())
_wire_log.propagate = False


@dataclass(frozen=True)
class Discovery:
    websocket_url: str
    bearer_token: str = field(repr=False)
    instance_id: str
    pid: int
    schema_version: int = 1


@dataclass(frozen=True)
class NavigationItem:
    kind: str
    id: str
    label: str
    status: str | None
    status_label: str | None
    indicator_kind: str | None
    deep_link: str | None
    icon_kind: str | None
    unread: bool | None
    accent_token: str | None
    pending_question: dict[str, Any] | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class Collection:
    items: list[NavigationItem]
    total_count: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class ActiveSession:
    id: str
    kind: str | None
    workspace_id: str | None
    project_id: str | None
    model_label: str | None
    model_provider_kind: str | None
    reasoning_effort: str | None
    reasoning_effort_label: str | None
    context_tier: str | None
    context_tier_label: str | None
    context_current_tokens: int | None
    context_token_limit: int | None
    context_usage_label: str | None
    ai_credit_label: str | None
    return_collection_key: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    protocol_version: str
    instance_id: str
    revision: int
    generated_at: str | None
    root: Collection
    collections: dict[str, Collection]
    active_session: ActiveSession | None
    mcp_servers: Any
    voice_dictation: Any
    raw: dict[str, Any]


def _collection(value: dict[str, Any]) -> Collection:
    items = []
    for item in value.get("items", []):
        question = item.get("pendingQuestion")
        items.append(NavigationItem(
            kind=item.get("kind", ""), id=item.get("id", ""),
            label=item.get("label", ""), status=item.get("status"),
            status_label=item.get("statusLabel"),
            indicator_kind=item.get("indicatorKind"),
            deep_link=item.get("deepLink"), icon_kind=item.get("iconKind"),
            unread=item.get("unread"), accent_token=item.get("accentToken"),
            pending_question=question if isinstance(question, dict) else None,
            raw=item,
        ))
    return Collection(items, value.get("totalCount", len(items)), value)


def parse_snapshot(msg: dict[str, Any]) -> Snapshot:
    """Parse a decoded snapshot, retaining unknown fields and status strings.

    Non-snapshots and invalid envelope fields raise ``ValueError``. Optional
    question metadata is opaque; missing or differently shaped metadata is safe.
    """
    if (
        not isinstance(msg, dict) or msg.get("type") != "snapshot"
        or type(msg.get("revision")) is not int
        or not isinstance(msg.get("instanceId"), str)
    ):
        raise ValueError("invalid companion snapshot envelope")
    raw = copy.deepcopy(msg)
    active = raw.get("activeSession")
    session = None
    if isinstance(active, dict):
        session = ActiveSession(
            id=active.get("id", ""), kind=active.get("kind"),
            workspace_id=active.get("workspaceId"),
            project_id=active.get("projectId"),
            model_label=active.get("modelLabel"),
            model_provider_kind=active.get("modelProviderKind"),
            reasoning_effort=active.get("reasoningEffort"),
            reasoning_effort_label=active.get("reasoningEffortLabel"),
            context_tier=active.get("contextTier"),
            context_tier_label=active.get("contextTierLabel"),
            context_current_tokens=active.get("contextCurrentTokens"),
            context_token_limit=active.get("contextTokenLimit"),
            context_usage_label=active.get("contextUsageLabel"),
            ai_credit_label=active.get("aiCreditLabel"),
            return_collection_key=active.get("returnCollectionKey"),
            raw=active,
        )
    return Snapshot(
        protocol_version=raw.get("protocolVersion", ""),
        instance_id=raw["instanceId"], revision=raw["revision"],
        generated_at=raw.get("generatedAt"),
        root=_collection(raw.get("root", {})),
        collections={
            key: _collection(value)
            for key, value in raw.get("collections", {}).items()
        },
        active_session=session, mcp_servers=raw.get("mcpServers"),
        voice_dictation=raw.get("voiceDictation"), raw=raw,
    )


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover(copilot_home: str | Path) -> Discovery | None:
    """Find a live app under ``copilot_home/run``, preferring companion v1."""
    for name in ("companion-navigation-v1.json", "logitech-navigation-v1.json"):
        try:
            data = json.loads((Path(copilot_home) / "run" / name).read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict) or data.get("schemaVersion") != 1
                or type(data.get("pid")) is not int or data["pid"] <= 0
                or not all(isinstance(data.get(key), str) and data[key]
                           for key in ("websocketUrl", "bearerToken", "instanceId"))
                or not _pid_alive(data["pid"])
            ):
                continue
            return Discovery(
                data["websocketUrl"], data["bearerToken"],
                data["instanceId"], data["pid"],
            )
        except (OSError, ValueError, OverflowError):
            continue
    return None


class CompanionFeed:
    """Discover and reconnect automatically; no commands are sent to the app."""

    def __init__(
        self, on_snapshot: Callable[[Snapshot], None],
        copilot_home: str | Path | None = None,
        *, reconnect_delay: float = 0.5, max_reconnect_delay: float = 10.0,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        if reconnect_delay <= 0 or max_reconnect_delay < reconnect_delay:
            raise ValueError("invalid reconnect delays")
        self._callback = on_snapshot
        self._on_disconnect = on_disconnect
        self._home = Path(copilot_home) if copilot_home is not None else Path.home() / ".copilot"
        self._delay = reconnect_delay
        self._max_delay = max_reconnect_delay
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._connected = threading.Event()
        self._latest: Snapshot | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    @property
    def latest(self) -> Snapshot | None:
        with self._lock:
            return self._latest

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._worker, name="companion-feed", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Cancel connection/backoff and wait for the worker (unless called by it)."""
        self._stopped.set()
        with self._lock:
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _worker(self) -> None:
        asyncio.run(self._run())

    def _accept_snapshot(self, snapshot: Snapshot, *, replay: bool = False) -> bool:
        with self._lock:
            previous = self._latest
            if (
                previous is not None
                and snapshot.instance_id == previous.instance_id
                and (snapshot.revision < previous.revision
                     or (snapshot.revision == previous.revision and not replay))
            ):
                return False
            self._latest = snapshot
        try:
            self._callback(snapshot)
        except Exception:
            # Don't include exception text or payloads: they may contain secrets.
            log.warning("companion snapshot callback failed")
        return True

    def _disconnect(self) -> None:
        was_connected = self._connected.is_set()
        self._connected.clear()
        if was_connected and self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception:
                log.warning("companion disconnect callback failed")

    async def _run(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        delay = self._delay
        try:
            while not self._stopped.is_set():
                discovery = discover(self._home)
                if discovery is not None:
                    try:
                        async with connect(
                            discovery.websocket_url,
                            additional_headers={"Authorization": f"Bearer {discovery.bearer_token}"},
                            max_size=None, open_timeout=5, close_timeout=1,
                            logger=_wire_log,
                        ) as websocket:
                            self._connected.set()
                            first_snapshot = True
                            async for frame in websocket:
                                if self._stopped.is_set():
                                    break
                                try:
                                    message = json.loads(frame)
                                    if not isinstance(message, dict) or message.get("type") != "snapshot":
                                        continue
                                    snapshot = parse_snapshot(message)
                                except (ValueError, TypeError, AttributeError):
                                    continue
                                if snapshot.instance_id != discovery.instance_id:
                                    continue
                                self._accept_snapshot(snapshot, replay=first_snapshot)
                                first_snapshot = False
                                delay = self._delay
                    except Exception:
                        log.debug("companion connection unavailable; rediscovering")
                    finally:
                        self._disconnect()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_delay)
        except asyncio.CancelledError:
            pass
        finally:
            with self._lock:
                self._loop = None
                self._task = None
            self._disconnect()
