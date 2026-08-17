# Copilot macropad

A hardware control surface for the GitHub Copilot app, built on a
[Pimoroni Keybow 2040](https://shop.pimoroni.com/products/keybow-2040). The top
eight keys mirror your pinned sessions and light up to show what each one is
doing; the bottom eight are global actions, including a two-key push-to-talk
dictation chord.

It's modelled on OpenAI's Codex Micro, but built against the surfaces the
Copilot app already exposes on your machine.

```
+---------+---------+---------+---------+
| session | session | session | session |   pinned sessions 1-4
+---------+---------+---------+---------+
| session | session | session | session |   pinned sessions 5-8
+---------+---------+---------+---------+
| approve |interrupt|  next   |   new   |   global agent actions
+---------+---------+---------+---------+
|      dictation    |  free   |  free   |   push-to-talk + spare
+---------+---------+---------+---------+
```

### LED states

| Colour | Meaning |
|---|---|
| dim white | idle |
| blue, breathing | agent is working |
| green | finished, output unread |
| amber, pulsing | waiting on your approval |
| red | the turn errored |
| off | no session pinned in that slot |

## How it works

Three pieces, each with one job.

**Firmware** on the pad handles key scanning, LED animation and the HID
keyboard. It receives *semantic* state from the host ("slot 3 is working")
rather than pixel values, so animation runs locally, serial stays quiet, and
the lights keep breathing even if the host stalls.

**A host daemon** works out what each slot should show. It merges two sources,
because neither is sufficient alone:

- **Copilot CLI hooks** push events the instant they happen — a turn started, a
  turn stopped, the agent needs approval, the turn errored. Fast and precise,
  but hooks can't see *you*: reading a session clears its unread badge inside
  the app and no hook fires.
- **The app's database** is authoritative for unread state and pin order, but we
  only observe it on a poll, so it lags a fast agent.

When the two disagree about whether a session is working, the more recent
observation wins. That's what stops a finished agent from staying blue until the
next poll, and stops a stale hook from pinning a slot to "working" forever.

**A hook config** at `~/.copilot/hooks/macropad.json` wires the two together.

### Things it will not do

The daemon **never writes to the Copilot app's database**. That database is live
and in WAL mode while the app runs, so every connection is opened read-only.
Everything that changes state goes through a surface the app owns — `ghapp://`
deep links and OS keystrokes.

The hook config is **purely additive**. The Copilot CLI loads every `*.json` in
its hooks directory, so installation writes only `macropad.json` and never
touches `agency.json` or anything else already there.

Our `permissionRequest` hook is **observe-only**. That event can return an
allow/deny decision on stdout, and other tools may already register a handler on
it — two hooks both answering is undefined behaviour. We watch it purely to turn
a LED amber, and every generated hook command discards stdout and exits 0, so a
dead daemon can never fail an agent turn.

## Setup

### 1. Firmware

Put CircuitPython on the pad if it isn't already: hold **BOOT** while plugging
it in, then drop the
[Keybow 2040 CircuitPython `.uf2`](https://circuitpython.org/board/pimoroni_keybow2040/)
onto the `RPI-RP2` drive that appears.

Install the two libraries the firmware needs into `CIRCUITPY\lib\`, from the
[PMK library](https://github.com/pimoroni/pmk-circuitpython) and the
[Adafruit CircuitPython bundle](https://circuitpython.org/libraries):

```
CIRCUITPY\lib\pmk\
CIRCUITPY\lib\adafruit_hid\
```

Then copy the firmware across:

```powershell
.\scripts\flash-firmware.ps1
```

**Unplug and replug the pad afterwards.** `boot.py` enables the USB serial data
port, and that only takes effect on a hard reset — a soft reload won't do it.

### 2. Daemon

```powershell
cd daemon
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Check it can read your Copilot state before going further:

```powershell
python -m macropad_daemon --status
```

That prints the eight sessions it resolved and the state it would show for each.
It needs no hardware, so it's the fastest way to confirm the plumbing works.

### 3. Hooks

```powershell
.\scripts\install-hooks.ps1
```

Hooks are picked up by **new** sessions, so restart any session you want
reflected on the pad. To preview without writing anything, run
`python -m macropad_daemon --print-hooks`.

### 4. Run it

```powershell
cd daemon
.\.venv\Scripts\python.exe -m macropad_daemon
```

## Running over RDP (pad and app on different machines)

The daemon has to run where the Copilot app's state lives. If you work over RDP,
that's the remote host — but the pad enumerates on the local client you're
sitting at. RDP can redirect COM ports, but redirection of USB-CDC composite
devices is unreliable, so there's a bridge for this.

The dictation key needs none of it. The chord is pure firmware HID, so the pad
types Ctrl+Win into the local machine and Wispr Flow picks it up there,
regardless of what the daemon is doing.

For the LEDs and session keys, on the **daemon machine** set:

```toml
[pad]
transport = "network"
bridge_host = "0.0.0.0"
bridge_port = 7831
```

Start the daemon once; it writes a shared token to `~/.copilot/macropad.token`.
Then on the **machine with the pad**, copy `scripts/pad_bridge.py` across and
run it:

```powershell
pip install pyserial
python pad_bridge.py --host <daemon-host> --token <token-from-that-file>
```

The bridge depends on nothing but `pyserial`, so the client machine doesn't need
the rest of this project. It connects outbound to the daemon, which is the same
direction RDP already travels, so it needs no inbound firewall change on the
client. Both sides reconnect on their own if the pad is replugged or the link
drops.

## Which session is on which key

Keys 1-8 follow your **pinned sessions**, in sidebar order — the daemon reads
the app's own pinned list, so re-ordering your pins re-orders the keys. Archived
pins are skipped rather than occupying a dead slot.

## Dictation

The two bottom-left keys drive one **Ctrl+Win** push-to-talk chord for Wispr
Flow. It's handled entirely in firmware as a real USB HID chord, so there's no
host round trip and it keeps working when the daemon isn't running.

The two keys share a hold refcount: the chord engages when the first goes down
and releases only when the last comes up, so rolling off one key mid-sentence
won't cut you off.

## Customising

Layout, colours and animation timing live in `keybow/config.py` on the pad
itself — edit it on the CIRCUITPY drive and CircuitPython reloads on save.

Daemon settings live in `config/macropad.toml` (or `~/.copilot/macropad.toml`,
or wherever `$MACROPAD_CONFIG` points). Colours can also be overridden from
there and are pushed to the pad on connect, so you can retune without
reflashing.

## Calibration

Two things are worth confirming against your own hardware and app rather than
trusting the defaults.

**Physical key numbering.** `keybow/config.py` assumes the stock Keybow 2040
layout: switch 0 bottom-left, numbering upwards through each column. If your
unit disagrees, install the calibration firmware, press keys, and read the
numbers off the REPL:

```powershell
.\scripts\flash-firmware.ps1 -Calibrate
```

Put the result into `ROWS` and reflash without `-Calibrate`. That table is the
only place a physical key number appears.

**Row 3 keystrokes.** `approve`, `interrupt` and `new_session` send keystrokes
to the app after focusing the target session. The defaults in
`config/macropad.toml` are starting points — confirm them against the running
app and fix them there.

**Watch for LED flicker at turn boundaries.** Hook-derived state is overridden
by any *newer* database snapshot, which is what stops a stale hook from pinning
a slot forever. The side effect is that if the app takes longer than one
`reconcile_interval` to record `is_running`, a starting turn can briefly show
blue, drop to white, then go blue again. This hasn't been observed with real
sessions — the app appears to write promptly — but if you do see a flicker,
raising `reconcile_interval` makes the window rarer and is the first thing to
try.

## Troubleshooting

**LEDs never change.** Check the daemon log for `hook <event> session=...` lines
with `--verbose`. No lines means hooks aren't reaching it: confirm
`~/.copilot/hooks/macropad.json` exists, and remember hooks are only picked up
by sessions started *after* installation.

**A second daemon appears to start fine but nothing works.** It won't — the
daemon now refuses to bind a port another instance already holds, and exits with
a clear message. If you see that, find the older process and stop it. (Python
enables `SO_REUSEADDR` by default, which on Windows would otherwise let the
duplicate bind successfully while the stale instance silently kept receiving
every request.)

**Pad not detected.** In `serial` mode the daemon only probes ports whose USB
vendor id looks like a CircuitPython board, falling back to every port when none
match. Confirm the pad enumerates as a serial device, and that you replugged it
after the first firmware install so `boot.py` could enable the data port. Pin
the port with `[serial] port` if discovery keeps picking wrong. If the pad is on
a different machine from the daemon, you want `transport = "network"` and the
bridge — see above.

**Bridge won't connect.** Check the token matches `~/.copilot/macropad.token` on
the daemon machine, that `bridge_host` isn't `127.0.0.1` (that only accepts
connections from the daemon's own machine), and that the daemon host is
reachable from the client on `bridge_port`.

**Dictation still works but nothing else does.** That's expected and by design —
the chord is pure firmware HID, so it's unaffected by the daemon being down.

## Development

```powershell
cd daemon
.\.venv\Scripts\python.exe -m pytest
```

The suite covers slot resolution and the hook/database conflict rule, the
read-only database reader against a fixture database, hook generation
(including the safety properties above), pad discovery ordering, and the hook
receiver over real HTTP.

## Layout

```
keybow/     CircuitPython firmware -- copy to CIRCUITPY
daemon/     host daemon and tests
config/     daemon config template
scripts/    install and flash helpers, plus pad_bridge.py for remote pads
```

## What's verified vs. what still needs your hardware

Confirmed working against the live app on this machine:

- Pinned sessions resolve to slots in sidebar order, with real names and unread
  flags, read-only.
- The generated hook commands run, exit 0, emit nothing, and drive the full
  state machine: `userPromptSubmitted` → working, `permissionRequest` →
  needs approval, `agentStop` → unread, `errorOccurred` → error.
- Installing hooks leaves every other file in `~/.copilot/hooks/` byte-identical.
- `ghapp://sessions/<id>` focuses the session — a simulated key press through
  the bridge navigated the app to the expected workspace.

Still needs the pad in hand — see [Calibration](#calibration):

- Physical key numbering.
- Whether the two dictation keys land where you want them.
- The row 3 `approve` / `interrupt` / `new_session` keystrokes.
