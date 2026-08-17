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

## Which machine does what

This can span up to three machines, so it's worth being explicit:

| Machine | Role |
|---|---|
| **Where the Copilot app runs** | The daemon and the hooks. Must be here — it's where `data.db` and the sessions live. |
| **Where the pad is plugged in** | Determines how the daemon reaches it, and where dictation types. |
| **A machine you can install on** | Bootstrap only — flashing CircuitPython and copying libraries onto the pad. Used once, then irrelevant. |

If the first two are the same machine, ignore this section — the default
`transport = "serial"` just works.

### Quickstart: pad on your local machine, RDP into the devbox

The common setup, and the one to try first. The pad stays plugged into the
machine in front of you; RDP carries its COM port into the session.

**In your RDP client**, before connecting:

> Show Options → Local Resources → More… → tick **Ports**

(or `redirectcomports:i:1` in the `.rdp` file). No admin, nothing installed.
Reconnect the session.

**On the devbox:**

```powershell
cd daemon
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m macropad_daemon --ports   # confirm the pad arrived
.\scripts\install-hooks.ps1
.\.venv\Scripts\python.exe -m macropad_daemon
```

That's the whole thing — the default `transport = "serial"` treats a redirected
port like any other, so LEDs and session keys both work. Dictation goes wherever
the pad is plugged in, and RDP forwards the chord into the session, so it lands
right either way.

If `--ports` doesn't find the pad, it prints what to check. Fall back to
[Option 2](#using-the-network-bridge) or the
[keystroke fallback](#keystroke-fallback).

### Getting the daemon and the pad connected

**Dictation works regardless, and needs nothing installed.** The pad is a USB
HID keyboard, so it types Ctrl+Win into whatever it's plugged into, and RDP
forwards keystrokes to the remote session like any other typing.

**For LED state and focus-on-press**, the daemon needs a two-way channel to the
pad. In order of preference:

**Option 1: RDP COM port redirection.** ⭐ *Start here if you RDP into the
machine running the Copilot app.*

CircuitPython's USB serial port enumerates under Windows' "Ports (COM & LPT)"
class exactly like a physical serial port, and RDP redirects that class
[bidirectionally](https://learn.microsoft.com/en-us/azure/virtual-desktop/redirection-remote-desktop-protocol).
So the pad stays plugged into the machine in front of you, its COM port appears
inside the session, and the daemon talks to it normally — **full functionality,
LEDs included**.

Enable it in the RDP client and reconnect:

> Remote Desktop Connection → Show Options → Local Resources → More… → tick
> **Ports**

or add `redirectcomports:i:1` to your `.rdp` file. **Neither needs
administrator rights or any software installed**, and on Windows 365 Cloud PCs
COM ports are
[redirected by default](https://learn.microsoft.com/en-us/azure/virtual-desktop/redirection-configure-serial-com-ports)
on the session side.

Then confirm it arrived:

```powershell
python -m macropad_daemon --ports
```

Keep the default `transport = "serial"` — a redirected port is just a COM port,
so nothing else changes.

**Option 2: run a bridge on the pad's machine.** A small relay forwards the pad
to the daemon over TCP. `scripts/pad-bridge.ps1` needs only Windows PowerShell
(which ships with Windows); `scripts/pad_bridge.py` needs Python and `pyserial`.
Use this when the pad's machine can run something. See
[Using the network bridge](#using-the-network-bridge).

**Option 3: keystroke fallback (input only).** If neither of the above is
available, the pad can drive the daemon by *typing* — see
[Keystroke fallback](#keystroke-fallback). This gets you session switching and
actions with nothing installed anywhere, but **no LED state**, because a
keyboard has no return path.

### When the pad's machine can run nothing at all

A locked-down machine that permits no installs and no PowerShell rules out both
bridges. What's left:

- **COM port redirection (Option 1) still works** — it's a client-side checkbox,
  not software. This is the path to try, and it gives you everything.
- **The keystroke fallback (Option 3) still works** — the pad is a keyboard, and
  keyboards always work. Input only.

Ruled out, so you don't waste time on them:

- **Keyboard LED state as a back-channel.** It's the one host→device path a HID
  keyboard has, but
  [RDP does not sync lock-key state back to the client](https://learn.microsoft.com/en-us/troubleshoot/windows-server/remote/caps-lock-key-status-not-synced-to-client)
  — the session uses an abstracted "Remote Desktop Keyboard Device" decoupled
  from your physical keyboard's LEDs. No setting changes it.
- **Writing state files to the CIRCUITPY drive.** Drive redirection can reach the
  volume, but CircuitPython's running code
  [cannot reliably see host writes without a remount or reset](https://learn.adafruit.com/customizing-usb-devices-in-circuitpython/circuitpy-midi-serial)
  — the host and the device deliberately don't share a live filesystem view.
  Windows 365 also disables drive redirection by default.
- **RemoteFX USB redirection.** Requires a client-side Group Policy that needs
  local admin. Worse, composite devices containing a mass-storage interface are
  excluded by default — and the pad is exactly that.
- **MIDI, smart card, WebAuthn, printer redirection.** Either not an RDP
  redirection class at all (MIDI), or classes CircuitPython doesn't implement.

## Keystroke fallback

Last resort, for when no data channel to the pad exists at all. The pad types
**F13–F24** — real HID keycodes that no physical keyboard emits and essentially
nothing binds — and the daemon picks them up as global hotkeys. RDP forwards
them like any other typing.

```
F13-F20  ->  session slots 1-8
F21-F24  ->  approve / interrupt / next attention / new session
```

On the pad, in `keybow/config.py`:

```python
SEND_FUNCTION_KEYS = True
```

On the daemon:

```toml
[pad]
transport = "hid"
```

**This is input-only.** A keyboard has no return path, so slot LEDs stay on
their dim "disconnected" colour — honest, rather than showing state that might
be stale. Dictation is unaffected. If you want the LEDs, you need Option 1 or 2.

## Setup

### 1. Bootstrap the pad

Do this on any machine you can install software on — a Mac is fine. It doesn't
have to be the machine that ends up hosting the pad; once flashed, the pad is
self-contained.

**Get the code:**

```bash
git clone https://github.com/brrusino/copilot-app-session-macropad
cd copilot-app-session-macropad
```

**Put CircuitPython on the pad.** A fresh Keybow 2040 has no Python on it, so
this is a one-time step.

The pad's chip has a permanent bootloader built in. Hold the small **BOOT**
button while plugging the USB-C cable in, and the pad pretends to be a USB stick
called **`RPI-RP2`**. Copying a `.uf2` file onto that stick makes it flash itself
and reboot — you don't "run" a `.uf2`, you just copy it.

```bash
# 1. Unplug the pad.
# 2. Hold BOOT, plug the cable back in, then let go.
#    A drive named RPI-RP2 appears.
./scripts/flash-firmware.sh --install-circuitpython
```

That downloads the right CircuitPython build and copies it across. macOS often
warns *"disk was not ejected properly"* — that's the pad rebooting mid-copy and
is completely normal. Wait a few seconds and a new drive named **`CIRCUITPY`**
appears.

**Copy the firmware and its libraries:**

```bash
./scripts/flash-firmware.sh --fetch-libs
```

`--fetch-libs` downloads the three libraries the firmware needs directly onto the
pad, so nothing is installed on the machine you're flashing from:

| Library | Why |
|---|---|
| `pmk` | Pimoroni's driver for the keys and LEDs |
| `adafruit_hid` | the dictation keystroke chord |
| `adafruit_is31fl3731` | the LED matrix driver PMK sits on top of |

**Then unplug and replug the pad.** `boot.py` enables the USB serial data port,
and that only takes effect on a full power cycle — a soft reload won't do it.

On Windows use `.\scripts\flash-firmware.ps1`, and install those three libraries
into `CIRCUITPY\lib\` yourself from the
[PMK library](https://github.com/pimoroni/pmk-circuitpython) and the
[Adafruit bundle](https://circuitpython.org/libraries).

### 2. Daemon

On the machine running the Copilot app:

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

## Using the network bridge

Use this when the pad is plugged into a different machine from the daemon and
that machine can run something. Typical case: you work on a Windows PC with the
pad attached, and RDP into a devbox where the Copilot app runs.

### Which side dials?

This matters more than it sounds. The daemon's machine often **cannot accept
inbound connections** — a Cloud PC or VM behind a gateway, or a firewall you're
not an administrator on. Outbound almost always works where inbound doesn't, so
there are two modes:

| `bridge_mode` | Who dials | Use when |
|---|---|---|
| `listen` | bridge → daemon | The daemon's machine accepts inbound connections. |
| `connect` | daemon → bridge | It doesn't. **Start here if the daemon runs on a Cloud PC or VM.** |

Either way the token is presented by whichever side dials out, so security is
identical.

### Recommended: daemon dials out (`connect`)

On the **daemon machine** (`~/.copilot/macropad.toml`):

```toml
[pad]
transport   = "network"
bridge_mode = "connect"
bridge_host = "<your-PC-hostname-or-IP>"
bridge_port = 7831
```

Run the daemon once to generate `~/.copilot/macropad.token`, and copy that value.

On the **PC with the pad**:

```powershell
.\pad-bridge.ps1 -Listen -Token <token>
```

That's it. The daemon keeps retrying until the bridge appears, and redials if
the link drops, so you can start them in either order.

`pad-bridge.ps1` needs **nothing installed** — Windows PowerShell ships with
Windows and the script uses only built-in .NET types. If you prefer Python,
`pad_bridge.py` does the same job with `pyserial`.

### If the daemon's machine does accept inbound

```toml
[pad]
transport   = "network"
bridge_mode = "listen"
bridge_host = "0.0.0.0"
bridge_port = 7831
```

```powershell
.\pad-bridge.ps1 -DaemonHost <devbox> -Token <token> -TestConnection   # verify first
.\pad-bridge.ps1 -DaemonHost <devbox> -Token <token>
```

`-TestConnection` checks reachability and the token before any hardware is
involved. If it reports UNREACHABLE, switch to `connect` mode above.

## If the daemon can't reach the pad

The pad degrades to a known state rather than failing:

- **Dictation still works.** Pure firmware HID, no daemon, no serial port.
- **Session keys show a dim "disconnected" colour**, so you can see at a glance
  that state isn't live rather than trusting stale colours.
- **Lost:** LED session state, and pressing a key to focus a session.

Still worth doing on the daemon side: install the hooks and run
`python -m macropad_daemon --status`, which prints the same information the LEDs
would show. It also means everything is in place the moment a transport appears.

See [When the pad's machine can run nothing at all](#when-the-pads-machine-can-run-nothing-at-all)
for why some setups have no transport available, and what to do about it.

## Which session is on which key

Keys 1-8 follow your **pinned sessions**, in sidebar order — the daemon reads
the app's own pinned list, so re-ordering your pins re-orders the keys. Archived
pins are skipped rather than occupying a dead slot.

## Dictation

The two bottom-left keys drive one **Ctrl+Win** push-to-talk chord for Wispr
Flow. It's handled entirely in firmware as a real USB HID chord, so there's no
host round trip, it needs no software on the machine it types into, and it keeps
working when the daemon isn't running at all.

The two keys share a hold refcount: the chord engages when the first goes down
and releases only when the last comes up, so rolling off one key mid-sentence
won't cut you off.

The chord itself is configurable — see `DICTATION_CHORD` in `keybow/config.py`.

## Customising

Layout, colours and animation timing live in `keybow/config.py` on the pad
itself — edit it on the CIRCUITPY drive and CircuitPython reloads on save.

Daemon settings live in `config/macropad.toml` (or `~/.copilot/macropad.toml`,
or wherever `$MACROPAD_CONFIG` points). Colours can also be overridden from
there and are pushed to the pad on connect, so you can retune without
reflashing.

## Calibration

**Physical key numbering is already correct** for a stock Keybow 2040, and this
is verified rather than assumed: Pimoroni's PMK documentation states keys are
numbered *"starting from the bottom left corner (when the USB connector is at
the top), which is key 0, going upwards in columns"*, which is exactly what
`ROWS` in `keybow/config.py` encodes. **Orient the pad with the USB-C connector
at the top** and the layout will match.

If your unit disagrees, or you want a different orientation, install the
calibration firmware, press keys, and read the numbers off the REPL:

```bash
./scripts/flash-firmware.sh --calibrate    # macOS / Linux
screen /dev/tty.usbmodem*                  # ctrl-a k to quit
```
```powershell
.\scripts\flash-firmware.ps1 -Calibrate    # Windows
```

Put the result into `ROWS` and reflash without the calibrate flag. That table is
the only place a physical key number appears.

**The dictation chord** defaults to Ctrl+Win for Wispr Flow. If your dictation
tool uses something else, change `DICTATION_CHORD` in `keybow/config.py` — it
takes Adafruit HID keycode names, so no code change is needed.

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

**Pad not detected.** Run `python -m macropad_daemon --ports` — it lists every
visible serial port and probes each for the pad, which is the quickest way to
tell a redirection problem from a firmware one. In `serial` mode the daemon only
probes ports whose USB vendor id looks like a CircuitPython board, falling back
to every port when none match. Confirm you replugged the pad after the first
firmware install so `boot.py` could enable the data port. Pin the port with
`[serial] port` if discovery keeps picking wrong.

Over RDP, "no ports visible" usually means COM redirection isn't forwarding the
port — see [Getting the daemon and the pad connected](#getting-the-daemon-and-the-pad-connected).

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

- Whether the two dictation keys land where you want them under your thumb.
- The row 3 `approve` / `interrupt` / `new_session` keystrokes.

Physical key numbering is **no longer** an unknown: it's confirmed against
Pimoroni's PMK documentation and already correct in `keybow/config.py`.

Unresolved until you try it: whether RDP COM port redirection forwards the pad
into the session on your specific client. `--ports` answers that in one command.
