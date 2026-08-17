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

### Quickstart: Windows PC with the pad, RDP into a devbox

The common setup. Pad on the PC in front of you, Copilot app on the devbox.

**On the devbox** (`~/.copilot/macropad.toml`):

```toml
[pad]
transport   = "network"
bridge_mode = "connect"
bridge_host = "<your-PC-hostname-or-IP>"
```

```powershell
cd daemon
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\scripts\install-hooks.ps1
.\.venv\Scripts\python.exe -m macropad_daemon        # writes ~/.copilot/macropad.token
```

**On the PC**, after flashing the pad:

```powershell
.\scripts\pad-bridge.ps1 -Listen -Token <token-from-that-file>
```

Nothing to install on the PC — `pad-bridge.ps1` uses only what ships with
Windows. `bridge_mode = "connect"` has the devbox dial *out*, which works even
when it can't accept inbound connections (Cloud PCs and gateway-fronted VMs
usually can't).

Dictation goes to the PC, since that's where the pad is plugged in. If your
dictation tool runs on the devbox instead, RDP forwards the keystrokes there
anyway — either way it works, with no extra setup.

### Getting the daemon and the pad connected

**Dictation works regardless, and needs nothing installed.** The pad is a USB
HID keyboard, so it types Ctrl+Win into whatever it's plugged into, and RDP
forwards keystrokes to the remote session like any other typing. If your
dictation tool runs on the remote machine, the chord reaches it. This is the key
you press most, and it has no dependency on anything below.

**LED state and focus-on-press need a data channel**, and that is a genuinely
harder problem when the pad's machine is locked down.

**Option 1: pad's serial port is visible to the daemon.** Either the pad is
plugged directly into the daemon machine, or its COM port is redirected into an
RDP session (*Local Resources → More → Ports* in the client — a checkbox, not an
install). Keep `transport = "serial"` and check with:

```powershell
python -m macropad_daemon --ports
```

That lists every visible serial port, flags CircuitPython vendor ids, and probes
each for the pad. **This is the only option that requires nothing whatsoever on
the machine holding the pad**, so on a locked-down host it is the one that has
to work. USB-CDC redirection is not always reliable, so test it early.

**Option 2: run a bridge on the pad's machine.** A small relay forwards the pad
to the daemon over TCP. Two versions:

- `scripts/pad-bridge.ps1` — needs only Windows PowerShell, which ships with
  Windows. Uses built-in .NET types, so there is **nothing to install**.
- `scripts/pad_bridge.py` — same job, needs Python and `pyserial`.

This is the right choice when you work on a machine you control and RDP into the
machine running the Copilot app. See
[Using the network bridge](#using-the-network-bridge) — and note that the daemon
can dial *out* to the bridge, which matters when its own machine can't accept
inbound connections.

### When the pad's machine can run nothing at all

If the machine holding the pad permits no Python, no PowerShell, and no COM
redirection, then **there is no way to drive the LEDs or focus sessions from
it.** Worth stating plainly rather than half-trying workarounds:

- Both bridges need *something* executable on that machine. Ruled out by
  definition.
- Keyboard LED state is not a usable back-channel. It is the one host→device
  path a HID keyboard has, but
  [RDP does not sync lock-key state back to the local keyboard](https://github.com/MicrosoftDocs/SupportArticles-docs/blob/main/support/windows-server/remote/caps-lock-key-status-not-synced-to-client.md)
  — Microsoft documents this. The remote session's lock state never reaches the
  physical device, so a pad on the client cannot learn anything from it.
- Other RDP channels (clipboard, drive redirection) reach the *client machine*,
  not a USB device attached to it, so they cannot carry state to the pad either.

**What to do instead: plug the pad into the machine running the Copilot app.**
The daemon then talks to it directly over USB with `transport = "serial"` and
everything works — LEDs, session keys, the lot.

Over RDP that machine is the remote one, so this needs USB redirection at the
hypervisor or RDP layer (Hyper-V enhanced session, VMware/Parallels USB
passthrough, or a USB-over-IP appliance). Those attach the device to the remote
machine rather than running software on the local one, which is why they're
compatible with a locked-down client.

Dictation is unaffected by that choice: the pad's Ctrl+Win chord goes to
whichever machine it's attached to, and if your dictation tool runs on the
remote machine, that is exactly where you want it.

## Setup

### 1. Bootstrap the pad

Do this on any machine you can install software on — it doesn't have to be the
machine that ends up hosting the pad day to day.

Put CircuitPython on the pad if it isn't already: hold **BOOT** while plugging
it in, then copy the
[Keybow 2040 CircuitPython `.uf2`](https://circuitpython.org/board/pimoroni_keybow2040/)
onto the `RPI-RP2` volume that appears. It reboots as `CIRCUITPY`.

Install the two libraries the firmware needs, from the
[PMK library](https://github.com/pimoroni/pmk-circuitpython) and the
[Adafruit CircuitPython bundle](https://circuitpython.org/libraries):

```
<CIRCUITPY>/lib/pmk/
<CIRCUITPY>/lib/adafruit_hid/
```

Then copy the firmware across:

```bash
./scripts/flash-firmware.sh          # macOS / Linux
```
```powershell
.\scripts\flash-firmware.ps1         # Windows
```

**Unplug and replug the pad afterwards.** `boot.py` enables the USB serial data
port, and that only takes effect on a hard reset — a soft reload won't do it.

Once flashed, the pad is self-contained: dictation works on any machine you
plug it into, with no software on that machine at all.

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

Two things are worth confirming against your own hardware and app rather than
trusting the defaults.

**Physical key numbering.** `keybow/config.py` assumes the stock Keybow 2040
layout: switch 0 bottom-left, numbering upwards through each column. If your
unit disagrees, install the calibration firmware, press keys, and read the
numbers off the REPL:

```bash
./scripts/flash-firmware.sh --calibrate    # macOS / Linux
screen /dev/tty.usbmodem*                  # ctrl-a k to quit
```
```powershell
.\scripts\flash-firmware.ps1 -Calibrate    # Windows
```

Put the result into `ROWS` and reflash without the calibrate flag. That table is
the only place a physical key number appears.

**The dictation chord.** Defaults to Ctrl+Win for Wispr Flow on Windows. If your
dictation tool uses something else, change `DICTATION_CHORD` in
`keybow/config.py` — it's Adafruit HID keycode names, so no code change needed.

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

- Physical key numbering.
- Whether the two dictation keys land where you want them.
- The row 3 `approve` / `interrupt` / `new_session` keystrokes.

Unresolved: whether the daemon can reach a pad plugged into a locked-down
machine. See [Getting the daemon and the pad connected](#getting-the-daemon-and-the-pad-connected)
and [If the daemon can't reach the pad](#if-the-daemon-cant-reach-the-pad).
