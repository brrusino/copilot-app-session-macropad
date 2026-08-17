# SPDX-License-Identifier: MIT
"""Actions the macropad can trigger on the host.

Two mechanisms, both of which go through surfaces the Copilot app actually owns.
Nothing here writes to the app's database.

**Focus a session** -- the ``ghapp://sessions/<id>`` deep link. ``ghapp:`` is
registered in ``HKCU\\Software\\Classes`` to the app executable, so handing the
URL to the shell focuses the session in the running instance.

**Keystrokes** -- Win32 ``SendInput``, via ctypes so there is no extra
dependency. Used for the row 3 actions that have no deep link, after focusing
the relevant session first.
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
INPUT_KEYBOARD = 1


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
    event = _INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=vk,
                wScan=0,
                dwFlags=KEYEVENTF_KEYUP if keyup else 0,
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
