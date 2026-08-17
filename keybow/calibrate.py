# SPDX-License-Identifier: MIT
"""Key-numbering calibration helper for the Keybow 2040.

The default ``ROWS`` table in ``config.py`` assumes the stock Keybow 2040
numbering (switch 0 bottom-left, counting upwards through each column). If your
unit disagrees, use this to find the truth rather than guessing.

Usage
-----
1. Back up ``code.py`` on the CIRCUITPY drive, then copy this file over it.
2. Open the CircuitPython REPL (any serial terminal on the *console* port).
3. Watch the sweep: keys light one at a time, low to high, and the REPL prints
   the number of the key that is currently lit. Note which physical key lights
   up for each number.
4. Press any key at any time -- it turns bright green and its number is printed.
5. Write the result into ``ROWS`` in ``config.py`` (rows top-to-bottom, each
   left-to-right), then restore the real ``code.py``.
"""

import time

from pmk import PMK
from pmk.platform.keybow2040 import Keybow2040 as Hardware

keybow = PMK(Hardware())
keys = keybow.keys

SWEEP_INTERVAL = 0.6

print()
print("Keybow 2040 calibration")
print("-" * 40)
print("Sweeping keys 0..{}. Press any key to identify it.".format(len(keys) - 1))
print()

previous = [False] * len(keys)
cursor = 0
last_step = time.monotonic()

while True:
    keybow.update()
    now = time.monotonic()

    if now - last_step >= SWEEP_INTERVAL:
        cursor = (cursor + 1) % len(keys)
        last_step = now
        print("lit: key {}".format(cursor))

    for number, key in enumerate(keys):
        pressed = key.pressed
        if pressed and not previous[number]:
            print(">>> PRESSED: key {}".format(number))
        previous[number] = pressed

        if pressed:
            key.set_led(0, 255, 0)      # pressed -> bright green
        elif number == cursor:
            key.set_led(120, 120, 120)  # sweep cursor -> white
        else:
            key.set_led(0, 0, 0)

    time.sleep(0.005)
