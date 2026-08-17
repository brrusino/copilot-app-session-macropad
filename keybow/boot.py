# SPDX-License-Identifier: MIT
"""Keybow 2040 boot configuration.

Enables the secondary USB CDC *data* port so the host daemon has a serial channel
that is independent of the CircuitPython REPL console. HID stays enabled (it is on
by default) because the dictation key must present as a real USB keyboard.

This file only takes effect on a hard reset / replug, not a soft reload.
"""

import usb_cdc

# console=True keeps the REPL available for debugging.
# data=True is the channel the host daemon talks to.
usb_cdc.enable(console=True, data=True)
