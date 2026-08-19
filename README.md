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
|attention|  duck   |  mode   | compact |   act on state, and on the app
+---------+---------+---------+---------+
|  clear  |    dictation      |  enter  |   composer keys + push-to-talk
+---------+---------+---------+---------+
```

Every key except the two dictation keys **raises the Copilot app first**, so a
press lands where you meant it even when you're in another window.

### LED states

| Colour | Meaning |
|---|---|
| dim white | idle |
| blue, breathing | agent is working |
| green | finished, output unread |
| amber, pulsing | waiting on you to answer something |
| red, blinking | interrupted part-way, needs a nudge |
| red, solid | the turn errored |
| white flash | your key press registered; pulses until the app has switched |
| dim blue | the daemon isn't connected, so no state is known |
| off | no session pinned in that slot |

Two behaviours are worth knowing, because both look like bugs otherwise:

- **A working session stays blue between turns.** `is_running` isn't
  continuous — it drops to false in the gap between turns and comes back,
  measured at up to 5.8s. Following it literally made the LED cycle
  blue → green → blue while work was plainly still going, so a working slot
  holds its colour across that gap. The cost is that a genuinely finished
  session takes a few seconds to turn green.
- **Unread is the app's own, never rolled up from children.** A parent whose
  subagent has unread output is not itself unread, and pretending otherwise
  produced a green light nothing could clear: one pin had 51 unread
  descendants. Work and questions *do* roll up, because those clear
  themselves — work stops on its own, and a question is answered from the
  parent.

## How it works

Three pieces, each with one job.

**Firmware** on the pad handles key scanning, LED animation and the HID
keyboard. It receives *semantic* state from the host ("slot 3 is working")
rather than pixel values, so animation runs locally, serial stays quiet, and
the lights keep breathing even if the host stalls.

**A host daemon** works out what each slot should show. It merges three
sources, because none is sufficient alone:

- **Copilot CLI hooks** push events the instant they happen — a turn started, a
  tool ran, the turn errored. Fast and precise, but hooks can't see *you*:
  reading a session clears its unread badge inside the app and no hook fires.
- **The app's database** is authoritative for unread state and pin order, but we
  only observe it on a poll, so it lags a fast agent.
- **The app's activity feed** is the only place a real question is recorded.
  `permissionRequest` fires before *every* tool call and its payload has no
  field distinguishing a genuine question from an auto-approval, so amber comes
  from `agent_asking` / `agent_plan_ready` in the feed instead.

Where they disagree, the rules are specific rather than "newest wins":

- The database is authoritative for **still running**. Hooks may only make the
  pad react *faster* to work starting, never contradict the app into idle —
  `agentStop` fires at the end of every turn, so letting it win made the LED
  flap once per turn during a long task.
- A question is retired by **hook evidence**, not by the feed. The app writes no
  activity item until the whole turn ends, so answering a question leaves
  `agent_asking` newest for minutes; a tool call after the question proves the
  agent is executing again.

**A hook config** at `~/.copilot/hooks/macropad.json` wires the two together.

### Things it will not do

The daemon **never writes to the Copilot app's database**. That database is live
and in WAL mode while the app runs, so every connection is opened read-only.
Everything that changes state goes through a surface the app owns — its own
keyboard shortcuts, typed by the pad, and `ghapp://` deep links.

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
[the network bridge](#using-the-network-bridge) — or just accept losing the
LEDs, since the pad types its own shortcuts and keeps working without a daemon.

### Getting the daemon and the pad connected

**Session switching and dictation work regardless, and need nothing installed.**
The pad is a USB HID keyboard: it types the app's `Ctrl+<n>` shortcut and the
Ctrl+Win dictation chord into whatever it's plugged into, and RDP forwards
keystrokes to the remote session like any other typing. No daemon required for
either.

**What the daemon adds is the LEDs** — live session state — and for that it
needs a two-way channel to the pad. In order of preference:

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

**If neither is available**, you still get session switching, actions and
dictation — the pad types those itself. You just lose the LEDs, because a
keyboard has no return path.

### When the pad's machine can run nothing at all

A locked-down machine that permits no installs and no PowerShell rules out both
bridges. What's left:

- **COM port redirection (Option 1) still works** — it's a client-side checkbox,
  not software. This is the path to try, and it gives you everything.
- **The pad keeps typing** — keyboards always work, so switching and dictation
  survive with nothing installed anywhere. Input only, no LEDs.

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

## How keystrokes reach the app

This is the part worth understanding, because it's the opposite of how the
project started.

**The pad types; the daemon never does.** Pressing a session key makes the pad
type the app's own `Ctrl+<n>` shortcut over USB HID. The daemon's role is
lights and bookkeeping.

That isn't a stylistic choice. The daemon's only mechanism is Win32
`SendInput`, and that reaches nothing unless the daemon happens to be running
on the interactive desktop — sending `Win+R` from a service-like context
produces no Run dialog at all. Over RDP it's worse in principle: the keyboard
belongs to the machine in front of you, not the one the daemon runs on. The pad
is a real USB keyboard, so its keystrokes are forwarded like any other. The
dictation chord worked from day one for exactly this reason.

Anything the *daemon* decides — which session is "previous" or "next" — is sent
to
the pad as a chord for the pad to type.

The `ghapp://sessions/<id>` deep link is still there as a fallback for slots
past the app's single-digit shortcuts, or when the pad is disconnected. It
works, but it hands a URL to the shell, which spawns `github.exe` to route it:
**measured at ~4.5s** versus about a second for the keystroke.

### Shortcuts this relies on

All confirmed against a running instance — the first eight read straight off
the app's own accessibility labels, the last two verified in use:

```
Ctrl+<n>          select the nth pinned session
Ctrl+B            toggle sidebar
Ctrl+K            search
Ctrl+Comma        settings
Ctrl+T            add tab
Ctrl+Alt+B        toggle review panel
Ctrl+[ / Ctrl+]   back / forward
Ctrl+Alt+\        open plan
Ctrl+N            new session
Ctrl+Shift+O      new chat
Shift+Tab         cycle mode (plan / interactive / autopilot)
```

Nothing here is a guess any more. The pad types all of them; the fixed ones
live in `keybow/config.py` so they keep working with no daemon running.

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

> If any script gives **"permission denied"**, it just isn't marked executable
> on your machine. Either run it with `bash` in front —
> `bash scripts/flash-firmware.sh --install-circuitpython` — or fix it once with
> `chmod +x scripts/*.sh`.

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

## What rows 3 and 4 do

| key | keys sent |
|---|---|
| next attention | `Ctrl+<n>` for whichever session wants you |
| rubber duck | the prompt, then `Enter` |
| cycle mode | `Shift+Tab` |
| compact | `/compact` + `Enter` |
| clear | `Ctrl+A`, `Delete` |
| enter | `Enter` |

**Seven of the eight are typed by the pad itself**, like dictation: they're
fixed chords with no session logic, so routing them through the daemon would
only add latency and a dependency on it being up.

**Next attention is the exception, and the reason row 3 exists.** Rows 1 and 2
already give random access to all eight pins, so a key that merely *steps*
through them adds nothing — measured 187 direct session presses against 16
steps. This one acts on state instead: it goes to whichever session is asking,
errored or unread, in that order, and repeated presses walk the list. That
information is what the LEDs show and what nothing else on the pad can reach.

**The rubber-duck key types a request, not a keystroke.** An earlier version of
this key typed a single `/` to open the command palette, which saved exactly one
character you were already positioned to type — useless. It now types a prompt
that names the `rubber-duck` agent explicitly, so the request lands as a
dispatch rather than an invitation to muse, and appends to whatever is already
in the composer so you can write the context first. Reword it via
`RUBBER_DUCK_PROMPT` in `keybow/config.py`.

**Enter is also how you approve.** There's no separate approve key: it types
into whatever you're looking at, which is simpler and safer than having the
daemon pick a session to confirm on your behalf.

### Raising the app

Every key except dictation brings the Copilot app forward first, by typing
Windows' own `Win+<n>` "focus the nth taskbar app" shortcut.

It has to be the pad that does this. Windows refuses `SetForegroundWindow` from
a process that didn't receive the last input event — verified here, the call
returns `False` as soon as any other app has focus — so the daemon cannot raise
its own app. A keyboard can.

Set `FOCUS_APP_CHORD` in `keybow/config.py` to your app's taskbar position
(counting left to right, ignoring Start / Search / Task View), or `None` to
never steal focus.

Two details that matter:

- **`Win+<n>` toggles.** Sent while the app is already focused it *minimises*
  it. So the pad only sends it when the daemon has told it the app isn't in
  front, and assumes focused when there's no daemon to ask.
- **The keystroke waits.** Activation is asynchronous, so typing straight after
  the focus chord races the window coming up and lands wherever focus still
  was. The pad holds the keystroke until the daemon confirms the app is
  frontmost, and drops it if that never happens.

## Which session is on which key

Keys 1-8 map to `Ctrl+1` … `Ctrl+8`, which is the app's own shortcut for
selecting the nth **pinned session** — so the keys follow your pins in sidebar
order, and re-ordering your pins re-orders the keys. Because the app resolves
the number itself, the pad and the app can never disagree about which session
key 3 means.

The daemon reads the same pinned list to decide what each LED shows. It skips
archived pins, and skips child sessions so a key always addresses something you
drive directly — though a child's *work* still lights up its parent.

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

**Row 3 and 4 keystrokes** live in `TYPING_KEYS` in `keybow/config.py`. Each
value is a tuple sent in order, and an entry is one of three things: a chord
name (`ctrl+a`), a number meaning "pause this many seconds", or `text:...` for
literal text. So a key can clear the composer with `Ctrl+A` then `Delete`, or
type a whole sentence and submit it.

The pause matters for anything that opens the app's command menu. Typing `/`
opens it and each character filters it, which is async work in a webview — and
the pad types far faster than that settles, so an Enter sent immediately after
arrives before the menu is ready and leaves the command sitting unsent. Tune
the wait via `MENU_SETTLE`.

**Your taskbar position.** `FOCUS_APP_CHORD` in `keybow/config.py` must match
where the Copilot app sits on your taskbar, or every key will raise the *wrong*
app and then type into it. Count left to right, ignoring Start / Search and
Task View. Set it to `None` to turn focus-stealing off entirely.

**LED timing you may notice, both deliberate.** A working session holds blue for
a few seconds after it stops, because `is_running` drops out between turns (up
to 5.8s measured) and following it literally made the light cycle while work was
still going. And a question clears as soon as the agent runs its next tool, not
when the app records the turn — the app writes nothing to its activity feed
until the whole turn ends, which would leave the light amber for minutes after
you'd answered.

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
It's also the diagnostic that matters most: if dictation types but session keys
don't, the pad is fine and the problem is the daemon or the serial link.

**Never kill the daemon — use `--quit`.**

```powershell
python -m macropad_daemon --quit
```

Over RDP the pad's port is redirected, and a process dying while holding it open
leaves the redirection **wedged**: every later open fails with *"Access is
denied"* and it does not clear on its own. Recovering means unplugging the pad
or reconnecting the RDP session. `--quit` closes the port first.

If it does wedge, `--ports` now says `BUSY (access denied)` rather than
reporting the pad as missing — that distinction matters, because "not found"
sends you looking for an unplugged pad instead of a stuck port.

**Ports vanish entirely when RDP disconnects.** COM3/COM4 exist only while the
session is connected, so `keybow not found` after you disconnect is correct, not
a fault. The daemon reconnects on its own when you come back.

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

Confirmed working on real hardware, against the live app:

- Session keys switch sessions by typing `Ctrl+<n>` — **about a second**,
  versus ~4.5s for the deep link it replaced.
- Pinned sessions resolve to slots in sidebar order, with real names, unread
  flags and run state, read-only.
- LEDs track live state, including rolling a child's *work* up to its parent.
- The dictation chord, as a pure-firmware HID hold with a two-key refcount.
- RDP COM port redirection carries the pad into the session; the daemon
  discovers it on COM4 and reconnects by itself after a replug.
- The generated hook commands run, exit 0, emit nothing, and drive the state
  machine.
- Installing hooks leaves every other file in `~/.copilot/hooks/` byte-identical.
- Autostart via the Startup folder, and `--quit` for a clean stop.

Still unverified:

- **Rows 3 and 4 as a whole**, and raising the app with `Win+7`. Implemented
  and unit-tested, and every shortcut they use is confirmed — but not yet
  exercised on hardware, because they need the firmware reflashing first.
- **The `interrupted` state.** Implemented and unit-tested, but never seen
  against real data: `was_interrupted` was false on every pinned session and all
  61 descendants when it was checked, so the signal is unconfirmed.
- Whether the dictation keys land where you want them under your thumb now
  they've moved to the middle pair.

Physical key numbering is **not** an unknown: it's confirmed against Pimoroni's
PMK documentation and already correct in `keybow/config.py`.
