# SPDX-License-Identifier: MIT
"""Actions the macropad can trigger on the host.

Three mechanisms, all through surfaces the Copilot app actually owns. Nothing
here writes to the app's database.

**Switch session** -- the app's own ``Ctrl+<n>`` shortcut, which selects the
nth session directly. This is the fast path and by far the most important one:
the deep link below hands a URL to the shell, which spawns ``github.exe`` to
route it, and that was measured at roughly 4.5 seconds before the session
appeared. The keystroke is immediate because nothing new is launched.

**Focus a session** -- the ``ghapp://sessions/<id>`` deep link. ``ghapp:`` is
registered in ``HKCU\\Software\\Classes`` to the app executable, so handing the
URL to the shell focuses the session in the running instance. Kept as the
fallback for when the app is not on screen to receive a keystroke.

**Keystrokes** -- Win32 ``SendInput``, via ctypes so there is no extra
dependency.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

DEEP_LINK_SCHEME = "ghapp"

#: Executable that owns the Copilot app window.
APP_EXECUTABLE = "github.exe"

#: Highest slot reachable by the app's Ctrl+<n> shortcut. The shortcut is
#: defined over the single-digit keys, so a pad with more session keys than
#: this must fall back to the deep link for the rest.
MAX_SHORTCUT_SLOT = 9


def session_deep_link(session_id: str) -> str:
    return f"{DEEP_LINK_SCHEME}://sessions/{session_id}"


def focus_session(session_id: str) -> bool:
    """Bring a session to the front in the Copilot app."""
    if not session_id:
        return False
    url = session_deep_link(session_id)
    try:
        if IS_WINDOWS:
            os.startfile(url)  # noqa: S606 - shell handler is the intended path
        else:
            subprocess.Popen(["open", url])
        log.info("focus %s", url)
        return True
    except OSError as exc:
        log.warning("deep link failed (%s): %s", url, exc)
        return False


# --- keystroke synthesis ---------------------------------------------------

VK = {
    "enter": 0x0D,
    "return": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}

MODIFIER_VK = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1

#: Keys that must carry the extended-key flag or the scan code is ambiguous
#: with the numeric keypad.
_EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C})


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def parse_chord(chord: str) -> tuple[list[int], int] | None:
    """``"ctrl+enter"`` -> ``([0x11], 0x0D)``. Returns None if unparseable."""
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        return None
    *modifier_names, key_name = parts

    modifiers = []
    for name in modifier_names:
        vk = MODIFIER_VK.get(name)
        if vk is None:
            log.warning("unknown modifier %r in chord %r", name, chord)
            return None
        modifiers.append(vk)

    if key_name in VK:
        key = VK[key_name]
    elif len(key_name) == 1:
        key = ord(key_name.upper())
    else:
        log.warning("unknown key %r in chord %r", key_name, chord)
        return None

    return modifiers, key


def _send(vk: int, keyup: bool) -> None:
    # Populate the scan code as well as the virtual key. The app is a webview,
    # and Chromium reads keyboard input from scan codes -- a synthetic event
    # with wScan left at 0 is accepted by Win32 and then silently ignored by
    # the page, which looks exactly like the shortcut not existing.
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_KEYUP if keyup else 0
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = _INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=vk,
                wScan=scan,
                dwFlags=flags,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))


def send_chord(chord: str) -> bool:
    """Synthesise one modifier+key chord to whatever currently has focus."""
    if not IS_WINDOWS:
        log.warning("keystroke synthesis is Windows-only; ignoring %r", chord)
        return False
    parsed = parse_chord(chord)
    if parsed is None:
        return False
    modifiers, key = parsed
    try:
        for vk in modifiers:
            _send(vk, keyup=False)
        _send(key, keyup=False)
        _send(key, keyup=True)
        # Release in reverse order, mirroring how a human lets go.
        for vk in reversed(modifiers):
            _send(vk, keyup=True)
        return True
    except Exception:
        log.exception("failed to send chord %r", chord)
        return False


def focus_then_chord(session_id: str, chord: str, settle: float = 0.25) -> bool:
    """Focus a session, then send it a keystroke.

    ``settle`` gives the app a moment to raise and take focus before the
    keystroke lands; too short and it goes to the previously focused window.
    """
    if not focus_session(session_id):
        return False
    time.sleep(settle)
    return send_chord(chord)


# --- finding the app window ------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_image_name(pid: int) -> str:
    """Basename of the executable behind ``pid``, or "" if unavailable."""
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return os.path.basename(buffer.value).lower()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def app_window() -> int | None:
    """Handle of the Copilot app's main window, or None if it is not running.

    Matched on the owning executable rather than the window title: the title
    changes with whatever session is open, so matching on it would break the
    moment you switched session -- which is precisely when this is used.
    """
    if not IS_WINDOWS:
        return None

    found: list[int] = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and _process_image_name(pid.value) == APP_EXECUTABLE:
            # Only top-level windows with a title bar are the real one; Tauri
            # keeps hidden helper windows in the same process.
            if user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
                return False
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception:
        log.exception("failed to enumerate windows")
        return None
    return found[0] if found else None


def app_is_foreground() -> bool:
    """Whether the Copilot app is the frontmost window.

    The daemon can *observe* this but cannot change it: Windows refuses
    ``SetForegroundWindow`` from a process that did not receive the last input
    event, which is every background daemon. Raising the app is therefore the
    pad's job, and this is what tells it whether that is needed.
    """
    if not IS_WINDOWS:
        return True
    hwnd = app_window()
    if hwnd is None:
        return False
    try:
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _activate(hwnd: int) -> bool:
    """Bring ``hwnd`` to the foreground so a keystroke will land in it."""
    user32 = ctypes.windll.user32
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        log.exception("failed to activate the app window")
        return False


def switch_to_slot(slot: int, session_id: str) -> bool:
    """Select the ``slot``-th session using the app's own Ctrl+<n> shortcut.

    Falls back to the deep link when the shortcut cannot be used -- the app is
    not running, the slot is past the single-digit shortcuts, or the window
    refuses to come forward. The fallback is correct but slow, so it is the
    exception rather than the path taken.
    """
    if not IS_WINDOWS or slot >= MAX_SHORTCUT_SLOT:
        return focus_session(session_id)

    hwnd = app_window()
    if hwnd is None:
        log.debug("app window not found; using the deep link")
        return focus_session(session_id)

    foreground = ctypes.windll.user32.GetForegroundWindow()
    if foreground != hwnd and not _activate(hwnd):
        # Windows refuses foreground changes from a background process in some
        # states. The deep link goes through the shell, which is allowed to.
        log.debug("could not foreground the app; using the deep link")
        return focus_session(session_id)

    return send_chord(f"ctrl+{slot + 1}")
