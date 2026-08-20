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

# Row 3, left to right.
#
# Only the first needs the host: "which session wants me" is derived from
# state the pad cannot see. The other three are fixed chords it types itself.
ACTION_KEYS = {
    ROWS[2][0]: "next_attention",
}

# Dictation: two adjacent bottom-row keys driving ONE push-to-talk chord.
# The middle pair, so it falls under a thumb from either hand.
DICTATION_KEYS = (ROWS[3][1], ROWS[3][2])

# The modifier chord the dictation keys hold down, as adafruit_hid Keycode
# NAMES (resolved at runtime so this file stays plain data).
#
# Default is Wispr Flow's push-to-talk on Windows: Ctrl+Win held down.
# On macOS the same physical chord reports as Ctrl+Cmd, so LEFT_GUI is still
# correct there -- but if your dictation tool uses a different shortcut, change
# it here rather than in code.py.
DICTATION_CHORD = ("LEFT_CONTROL", "LEFT_GUI")

# Bring the Copilot app to the front before acting on it.
#
# Windows focuses the nth taskbar app with Win+<n>, and that is the only
# mechanism available: the daemon cannot raise the window itself. Windows
# refuses SetForegroundWindow from a process that did not receive the last
# input event, which was confirmed here -- the call returns False once any
# other app has focus. The pad, being a keyboard, can.
#
# Set this to your Copilot app's taskbar position, counting left to right and
# ignoring Start / Search / Task View. Set it to None to never steal focus.
#
# Win+<n> TOGGLES: pressed while the app is already focused it minimises it.
# So the pad only sends this when the daemon has told it the app is not
# focused, and assumes focused when it has no daemon to ask.
FOCUS_APP_CHORD = "win+7"

# Keys that should bring the app forward first. Everything that acts on the
# app, which is everything except dictation -- dictation types into whatever
# you are already using, and stealing focus would defeat the point.
FOCUS_KEYS = tuple(k for row in ROWS for k in row if k not in DICTATION_KEYS)

# Keys that type a fixed sequence of chords straight into whatever has focus.
#
# These live on the pad rather than going through the daemon for the same
# reason dictation does: they are plain keystrokes with no session logic, so
# routing them through the host would only add latency and a dependency on the
# daemon being up. Each value is a tuple of chords, sent in order.
#
# How long to wait after typing a slash command before pressing Enter.
#
# Typing "/" opens the app's command menu and each letter filters it, which is
# async work in a webview. The pad sends its keystrokes back-to-back over USB,
# so without a pause the Enter arrives before the menu has settled and the
# command is left sitting in the composer unsent -- which is exactly what
# happened. The letters themselves land fine at full speed, so this is the only
# gap needed.
#
# Tune it here if a slower moment leaves the command unsent.
MENU_SETTLE = 0.4

# The app's own command for the rubber-duck subagent.
#
# This is a first-class slash command, not a sentence: the `rubber_duck`
# experiment adds /rubber-duck to the composer. Typing the command beats typing
# a request that asks for the same thing, because the app dispatches it
# directly instead of an agent having to read the wording and decide what was
# meant. Appended to whatever is already in the composer, so you can write the
# context first and then hit the key.
RUBBER_DUCK_COMMAND = "/rubber-duck"

# Keys that type a fixed sequence straight into whatever has focus.
#
# These live on the pad rather than going through the daemon for the same
# reason dictation does: they are plain keystrokes with no session logic, so
# routing them through the host would only add latency and a dependency on the
# daemon being up.
#
# Each value is a tuple, sent in order. An entry is either a chord name, a
# number meaning "pause this many seconds", or "text:..." for literal text.
#
#   row 3 [1]   - rubber duck: pressure-test what we just did
#   row 3 [2]   - cycle mode: plan -> interactive -> autopilot
#   row 3 [3]   - compact this session
#   row 4 left  - clear the composer: select everything, then delete it
#   row 4 right - submit, which is also how you approve a prompt
TYPING_KEYS = {
    ROWS[2][1]: ("text:" + RUBBER_DUCK_COMMAND, MENU_SETTLE, "enter"),
    ROWS[2][2]: ("shift+tab",),
    ROWS[2][3]: ("text:/compact", MENU_SETTLE, "enter"),
    ROWS[3][0]: ("ctrl+a", "delete"),
    ROWS[3][3]: ("enter",),
}

# Every key has a job now.
FREE_KEYS = ()

# Type the app's own Ctrl+<n> shortcut when a session key is pressed.
#
# This is how session switching actually happens, and it belongs on the pad
# rather than in the daemon. The daemon can only synthesise keystrokes with
# SendInput, which silently reaches nothing unless the daemon happens to be
# running on the interactive desktop -- and over RDP the keyboard belongs to
# the client machine, not the one the daemon runs on. The pad is a real USB
# keyboard, so RDP forwards what it types like any other key. The dictation
# chord already proved that path works.
#
# The serial link is still what carries LED state back, and the daemon still
# sees the press; it just no longer tries to perform the switch itself.
#
#   Ctrl+1 .. Ctrl+8 -> session slots 0-7, in pinned order
SEND_SESSION_SHORTCUTS = True

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
    # The bottom two rows are BACKLIT: their keycaps have a glyph cut through
    # them, and the LED is what makes that legend readable. So they rest near
    # maximum rather than dim -- the resting colour *is* the label, and a small
    # aperture throws away most of the light before it reaches your eye.
    #
    # Hue still separates the three groups, but every one is pushed to the top
    # of its range. Pressed states stay brighter still by going toward white,
    # which is the only headroom left above a saturated colour.
    "action":         ((190, 130, 255), "solid"),
    "action_active":  ((235, 215, 255), "solid"),
    # Dictation is the exception: its keycaps carry no glyph to light, so it
    # keeps the original dim resting colour and goes hot only while held.
    "dictation":      ((70, 30, 60),    "solid"),
    "dictation_live": ((255, 60, 140),  "solid"),
    "typing":         ((60, 255, 200),  "solid"),
    "typing_active":  ((215, 255, 245), "solid"),
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
