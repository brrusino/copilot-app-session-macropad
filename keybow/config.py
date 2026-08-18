# SPDX-License-Identifier: MIT
"""User-editable configuration for the Keybow 2040 macropad firmware.

Everything here is safe to tweak directly on the CIRCUITPY drive without touching
``code.py``. Save the file and CircuitPython reloads automatically.

Physical key numbering
----------------------
The Keybow 2040 numbers its 16 switches 0-15. On the stock board, switch 0 is the
**bottom-left** key and the numbering runs *upwards* through each column before
moving one column to the right::

     visual layout            key numbers
    +----+----+----+----+    +----+----+----+----+
    | r1 | r1 | r1 | r1 |    |  3 |  7 | 11 | 15 |   <- top row
    +----+----+----+----+    +----+----+----+----+
    | r2 | r2 | r2 | r2 |    |  2 |  6 | 10 | 14 |
    +----+----+----+----+    +----+----+----+----+
    | r3 | r3 | r3 | r3 |    |  1 |  5 |  9 | 13 |
    +----+----+----+----+    +----+----+----+----+
    | r4 | r4 | r4 | r4 |    |  0 |  4 |  8 | 12 |   <- bottom row
    +----+----+----+----+    +----+----+----+----+

If your unit disagrees, run ``calibrate.py`` (copy it over ``code.py``
temporarily) to print the number of whichever key you press, then fix ``ROWS``
below. Nothing else in the firmware hardcodes a key number.
"""

# Rows top-to-bottom, each left-to-right. This is the ONLY place physical
# key numbers are declared.
ROWS = (
    (3, 7, 11, 15),  # row 1  - session slots 0-3
    (2, 6, 10, 14),  # row 2  - session slots 4-7
    (1, 5, 9, 13),   # row 3  - global agent actions
    (0, 4, 8, 12),   # row 4  - dictation chord + free keys
)

# The 8 session slots, in order. Slot i mirrors the i-th pinned session.
SESSION_KEYS = ROWS[0] + ROWS[1]

# Row 3 global actions, left to right. Names are sent verbatim to the host.
ACTION_KEYS = {
    ROWS[2][0]: "approve",
    ROWS[2][1]: "interrupt",
    ROWS[2][2]: "next_attention",
    ROWS[2][3]: "new_session",
}

# Dictation: two adjacent bottom-row keys driving ONE push-to-talk chord.
# Defaults to the two leftmost bottom keys so it falls under a thumb.
DICTATION_KEYS = (ROWS[3][0], ROWS[3][1])

# The modifier chord the dictation keys hold down, as adafruit_hid Keycode
# NAMES (resolved at runtime so this file stays plain data).
#
# Default is Wispr Flow's push-to-talk on Windows: Ctrl+Win held down.
# On macOS the same physical chord reports as Ctrl+Cmd, so LEFT_GUI is still
# correct there -- but if your dictation tool uses a different shortcut, change
# it here rather than in code.py.
DICTATION_CHORD = ("LEFT_CONTROL", "LEFT_GUI")

# Remaining bottom-row keys are unassigned; presses are still reported to the
# host so they can be bound later without a reflash.
FREE_KEYS = (ROWS[3][2], ROWS[3][3])

# Also type F13-F24 when a session or action key is pressed.
#
# Leave this False for a normal setup: the pad talks to the daemon over USB
# serial, which carries key presses AND LED state.
#
# Set it True only as a fallback for when the daemon cannot see the pad's serial
# port at all -- typically a locked-down machine where RDP COM port redirection
# is unavailable. Because the pad is also a keyboard, RDP forwards these
# keystrokes to the remote session and the daemon can pick them up as global
# hotkeys with nothing installed on the local machine.
#
# It is input-only: a keyboard has no return path, so the LEDs stay on their
# "disconnected" colour. Pair with `transport = "hid"` on the daemon.
#
#   F13-F20 -> session slots 0-7
#   F21-F24 -> approve / interrupt / next attention / new session
SEND_FUNCTION_KEYS = False

# Global brightness scale applied to every colour, 0.0-1.0.
BRIGHTNESS = 0.6

# Semantic state -> (colour, effect). The host pushes state names; the pad owns
# the colours and the animation. Effects: "solid", "breathe", "pulse", "off".
#
# Colours are plain (r, g, b) 0-255 tuples and can be overridden at runtime by a
# {"t":"palette"} message from the host, so tuning does not require a reflash.
PALETTE = {
    "empty":          ((0, 0, 0),       "off"),
    "idle":           ((110, 110, 110), "solid"),
    "working":        ((0, 80, 255),    "breathe"),
    "unread":         ((0, 230, 60),    "solid"),
    "needs_approval": ((255, 110, 0),   "pulse"),
    # Stopped part-way and waiting for a nudge. Blinking rather than solid so
    # it cannot be confused with a plain error, and red rather than amber so it
    # cannot be confused with a session asking you a question.
    "interrupted":    ((255, 20, 20),   "pulse"),
    "error":          ((255, 20, 20),   "solid"),
    # Shown on every session key when the host daemon is not connected.
    # Deliberately a different HUE to idle, not just dimmer: "the daemon is
    # down" and "this session is idle" are completely different situations and
    # two shades of dim white are impossible to tell apart on these LEDs.
    "disconnected":   ((0, 0, 45),      "solid"),
    # Action keys sit dim until pressed.
    "action":         ((60, 40, 90),    "solid"),
    "action_active":  ((200, 150, 255), "solid"),
    # Dictation keys: dim normally, hot while the chord is held.
    "dictation":      ((70, 30, 60),    "solid"),
    "dictation_live": ((255, 60, 140),  "solid"),
    # Momentary flash confirming a session key press was registered. The app
    # takes several seconds to navigate, so without this the pad appears to
    # have ignored you.
    "pressed":        ((255, 255, 255), "solid"),
}

# Animation tuning, in seconds per full cycle.
BREATHE_PERIOD = 2.4
PULSE_PERIOD = 0.7

# How long without a host heartbeat before the pad decides it is on its own.
HOST_TIMEOUT = 6.0

# How often the pad emits its own heartbeat.
HEARTBEAT_INTERVAL = 2.0
