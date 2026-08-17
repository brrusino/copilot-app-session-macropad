# SPDX-License-Identifier: MIT
"""Global hotkey input: the pad drives the daemon by *typing*.

Why this exists
---------------
The pad may be plugged into a machine that can run nothing at all -- a
locked-down corporate device with no installs and no PowerShell -- while the
daemon runs on a remote machine reached over RDP. In that situation no bridge
and no serial redirection is available.

But one channel always works: **the pad is a USB keyboard**, and RDP forwards
keystrokes to the remote session like any other typing. So instead of sending
key events over a data link, the firmware types a key that nothing else uses and
the daemon listens for it globally.

F13-F24 are used because they are real, unambiguous HID keycodes that no
physical keyboard emits and essentially no software binds. That means no
collision with anything you actually type, and no modifier chord to get wrong.

    F13-F20  ->  session slots 0-7
    F21-F24  ->  approve / interrupt / next attention / new session

What this does *not* give you is LED state: it is an input-only channel, because
a keyboard has no return path that survives RDP. Slot LEDs stay on their
"disconnected" colour, which is honest rather than stale.

Implementation note
-------------------
``RegisterHotKey`` is thread-affine and its ``WM_HOTKEY`` messages are delivered
to the registering thread's message queue, so registration and the message pump
both live on one dedicated thread.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

#: ``callback(kind, index)`` where kind is "session" or "action".
HotkeyCallback = Callable[[str, int], None]

# Virtual-key codes. F13 = 0x7C ... F24 = 0x87.
VK_F13 = 0x7C
SESSION_KEY_COUNT = 8
ACTION_KEY_COUNT = 4

MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001

#: Order must match the firmware's row 3 layout.
ACTIONS = ("approve", "interrupt", "next_attention", "new_session")


def vk_for_slot(slot: int) -> int:
    return VK_F13 + slot


def vk_for_action(index: int) -> int:
    return VK_F13 + SESSION_KEY_COUNT + index


class HotkeyListener:
    """Listens for the pad's F13-F24 keystrokes anywhere in the session."""

    def __init__(self, on_hotkey: HotkeyCallback) -> None:
        self._on_hotkey = on_hotkey
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._registered: list[int] = []
        self.failed: list[str] = []

    @property
    def connected(self) -> bool:
        """True once at least one hotkey is live.

        Named to match the other transports so the daemon can treat them alike.
        """
        return bool(self._registered)

    def set_on_connect(self, callback: Callable[[], None]) -> None:
        # Nothing to push to a keyboard; accepted for interface compatibility.
        self._on_connect = callback

    def send(self, message: dict) -> bool:
        """No return path exists over a keyboard, so this always fails.

        The daemon calls it to push LED state; reporting False keeps the pad on
        its "disconnected" colour rather than showing stale state.
        """
        return False

    def start(self) -> None:
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="macropad-hotkeys", daemon=True)
        self._thread.start()
        # Surface registration failures to the caller rather than silently
        # listening for nothing.
        self._ready.wait(timeout=5)
        if self.failed:
            log.warning(
                "%d hotkey(s) could not be registered (already taken by another app): %s",
                len(self.failed),
                ", ".join(self.failed),
            )
        if self._registered:
            log.info("listening for pad keystrokes on %d hotkeys", len(self._registered))
        else:
            log.error("no hotkeys could be registered; pad keys will not work")

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # -- worker ----------------------------------------------------------

    def _register_all(self, user32) -> None:
        for slot in range(SESSION_KEY_COUNT):
            hotkey_id = slot
            if user32.RegisterHotKey(None, hotkey_id, MOD_NOREPEAT, vk_for_slot(slot)):
                self._registered.append(hotkey_id)
            else:
                self.failed.append(f"F{13 + slot}")

        for index in range(ACTION_KEY_COUNT):
            hotkey_id = SESSION_KEY_COUNT + index
            if user32.RegisterHotKey(None, hotkey_id, MOD_NOREPEAT, vk_for_action(index)):
                self._registered.append(hotkey_id)
            else:
                self.failed.append(f"F{13 + SESSION_KEY_COUNT + index}")

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        self._register_all(user32)
        self._ready.set()

        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                # GetMessage blocks, which is what we want: no polling.
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result in (0, -1):  # WM_QUIT or error
                    break
                if message.message == WM_HOTKEY:
                    self._dispatch(int(message.wParam))
        finally:
            for hotkey_id in self._registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._registered.clear()

    def _dispatch(self, hotkey_id: int) -> None:
        if hotkey_id < SESSION_KEY_COUNT:
            kind, index = "session", hotkey_id
        else:
            kind, index = "action", hotkey_id - SESSION_KEY_COUNT
        try:
            self._on_hotkey(kind, index)
        except Exception:
            log.exception("hotkey handler failed")
