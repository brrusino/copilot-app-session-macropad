# SPDX-License-Identifier: MIT
"""Daemon entry point.

Wires the four moving parts together:

* :mod:`.hook_server` receives push events from the Copilot CLI hooks.
* :mod:`.copilot_db` reconciles against the app's own state, read-only.
* :mod:`.state` merges those two into one state string per LED slot.
* :mod:`.serial_link` carries states to the pad and key events back.

Useful without hardware::

    python -m macropad_daemon --status         # resolve slots and print them
    python -m macropad_daemon --ports          # is the pad's serial port visible?
    python -m macropad_daemon --print-hooks    # show the hook config
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from . import config as config_module
from . import actions, hooks_install
from .copilot_db import CopilotDB
from .hook_server import PortInUseError
from .state import StateStore, section_attention

log = logging.getLogger("macropad")

#: How long to keep the pad's press pulse running while waiting for the app to
#: focus a session.
#:
#: Derived from measurement, and re-derived once the pad started typing the
#: app's own Ctrl+<n> instead of firing a deep link. Across 70 observed
#: navigations, 69 completed within one second; the deep link this replaced was
#: timed at ~4.5s, which is where the old 12s failsafe came from.
#:
#: The failsafe now only bounds the *failure* case -- a press whose keystroke
#: never reached the app, which happened 7 times in that same sample and left
#: the key pulsing white for a full 12 seconds each time. Three seconds is
#: three times the observed p99 and cuts that visible cost fourfold.
NAVIGATION_TIMEOUT = 3.0

#: Pad protocol version this daemon needs. Bumped alongside
#: ``FIRMWARE_VERSION`` in keybow/code.py whenever the daemon starts relying on
#: a message an older pad would silently ignore.
REQUIRED_FIRMWARE = 6


class Daemon:
    def __init__(self, cfg: config_module.Config) -> None:
        self.cfg = cfg
        self.db = CopilotDB(cfg.db_path)
        self.store = StateStore(slot_count=cfg.slot_count)
        self.link = self._build_link(cfg)
        self.link.set_on_connect(self._on_pad_connect)
        self.dev_tunnel = self._build_dev_tunnel(cfg)
        self.hooks = hook_server_for(cfg, self._on_hook)
        self._last_pushed: list[str] | None = None
        self._last_states: list[str] | None = None
        #: Session key most recently pressed, used to target actions that need
        #: a subject (interrupt) without guessing.
        self._last_session_slot: int | None = None
        #: Where the attention key last landed, so repeated presses walk the
        #: list rather than sticking on the first match.
        self._last_attention_slot: int | None = None
        #: Pad protocol version, learned from its hello or heartbeat.
        self._pad_firmware: int | None = None
        #: Whether the app was frontmost at the last check, so the pad is only
        #: told when it changes.
        self._app_focused: bool | None = None
        #: The current sidebar sections ("Pinned", then one per explicit
        #: group), refreshed every reconcile. Rows 1-2 show whichever section
        #: ``_section_index`` currently points at.
        self._sections: list = []
        #: Index into ``_sections``. Starts at 0 ("Pinned") and is clamped
        #: whenever the section list shrinks -- e.g. a group emptying out.
        self._section_index: int = 0
        #: Last action_states payload sent, so a repeat reconcile with nothing
        #: to report does not resend the same two LED states every tick.
        self._last_section_states: dict | None = None

    def _build_link(self, cfg: config_module.Config):
        """Pick the pad transport.

        ``serial``  - pad's serial port is visible to this machine.
        ``network`` - a bridge relays the pad from another machine.
        ``both``    - accept either, so changing RDP clients needs no reconfig.

        Imported lazily so the hardware-free modes (--status, --print-hooks,
        --install-hooks) work without pyserial installed.
        """
        def network_link():
            from .network_link import NetworkLink, load_or_create_token

            token = cfg.bridge_token or load_or_create_token(cfg.copilot_home)
            return NetworkLink(
                on_event=self._on_pad_event,
                host=cfg.bridge_host,
                port=cfg.bridge_port,
                token=token,
                mode=cfg.bridge_mode,
            )

        from .serial_link import SerialLink

        def serial_link():
            return SerialLink(
                on_event=self._on_pad_event,
                port=cfg.serial_port,
                baud=cfg.serial_baud,
            )

        if cfg.pad_transport == "network":
            return network_link()
        if cfg.pad_transport == "both":
            from .multi_link import MultiLink

            return MultiLink((serial_link(), network_link()))
        return serial_link()

    @staticmethod
    def _build_dev_tunnel(cfg: config_module.Config):
        if not cfg.devtunnel_id:
            return None
        from .dev_tunnel import DevTunnelHost

        return DevTunnelHost(
            tunnel_id=cfg.devtunnel_id,
            port=cfg.bridge_port,
            command=cfg.devtunnel_command,
            log_file=cfg.copilot_home / "macropad-devtunnel.log",
        )

    # -- inputs ----------------------------------------------------------

    def _on_hook(self, event_type: str, session_id: str, payload: dict) -> None:
        log.debug("hook %s session=%s", event_type, session_id[:8] if session_id else "?")
        self.store.apply_hook(event_type, session_id)
        # Hooks are the low-latency path; push straight away rather than
        # waiting for the next reconcile tick.
        self._push_states()

    def _on_pad_connect(self) -> None:
        # Send the palette entries for states the pad's firmware may predate.
        # Without this a slot resolving to one of them is silently ignored by
        # the firmware, which validates incoming states against its palette --
        # so the LED would keep showing the previous state and the new one
        # would appear simply not to work.
        palette = dict(NEW_STATE_PALETTE)
        palette.update(_section_color_palette(self.cfg.section_colors))
        palette.update(self.cfg.palette or {})
        self.link.send({"t": "palette", "v": palette})
        if self.cfg.brightness is not None:
            self.link.send({"t": "brightness", "v": self.cfg.brightness})
        if self.cfg.brightness_levels:
            self.link.send({"t": "levels", "v": self.cfg.brightness_levels})
        # Resend on connect: a pad that just came up assumes the app is focused,
        # which is wrong as often as it is right.
        self._push_focus(force=True)
        self._last_pushed = None
        self._push_states()

    def _note_firmware(self, version) -> None:
        """Record the pad's protocol version, warning once if it is too old.

        An old pad ignores messages it does not recognise, so the actions that
        depend on them fail silently and look like the app ignoring its own
        shortcuts. Saying so once, loudly, is the difference between a reflash
        and an afternoon of debugging.
        """
        version = version if isinstance(version, int) else 0
        if version == self._pad_firmware:
            return
        self._pad_firmware = version
        if version < REQUIRED_FIRMWARE:
            log.warning(
                "pad firmware is v%s but this daemon needs v%s: reflash keybow/ "
                "onto the pad, or the newest keys will silently do nothing",
                version,
                REQUIRED_FIRMWARE,
            )
        else:
            # Says so out loud because "did that flash actually land?" is
            # otherwise unanswerable from this side: an old pad and a new one
            # behave identically right up until you press the key that changed.
            log.info("pad firmware v%s", version)

    def _on_pad_event(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "hello":
            log.info(
                "pad firmware v%s, %s slots", message.get("fw"), message.get("slots")
            )
            self._note_firmware(message.get("fw"))
            return
        if kind == "hb":
            # The pad reports its version on every heartbeat, so a daemon that
            # started after the pad still learns it.
            self._note_firmware(message.get("fw"))
            return
        if kind != "down":
            # Releases matter only for the dictation chord, which the firmware
            # handles entirely on its own.
            return

        role = message.get("role")
        if role == "session":
            self._activate_session(
                message.get("slot"), typed_by_pad=bool(message.get("typed"))
            )
        elif role == "action":
            self._run_action(str(message.get("action") or ""))
        elif role == "free":
            log.info("unbound key %s pressed", message.get("k"))

    # -- actions ---------------------------------------------------------

    def _activate_session(self, slot, typed_by_pad: bool = False) -> None:
        if not isinstance(slot, int):
            return
        session = self.store.session_for_slot(slot)
        if session is None or not session.focusable:
            log.info("slot %s has no session to focus", slot)
            self.link.send({"t": "busy_done", "k": slot})
            return
        self._last_session_slot = slot
        if typed_by_pad:
            # Firmware before v4 still types the positional shortcut. Let that
            # complete rather than racing it with the exact click; the daemon's
            # version warning tells the user why child sessions still count.
            log.info("slot %s -> %s (legacy positional shortcut)", slot, session.name)
            target = self._await_navigation
            args = (slot, session)
        else:
            log.info("focus exact parent slot %s -> %s", slot, session.name)
            target = self._focus_parent_session
            args = (slot, session)
        # UI Automation and navigation are off-thread so the serial/LED path is
        # never blocked by a Tauri tree walk or app transition.
        threading.Thread(
            target=target,
            args=args,
            name="macropad-nav",
            daemon=True,
        ).start()

    def _focus_parent_session(self, slot: int, session) -> None:
        # InvokePattern is semantic rather than coordinate-backed, so exact
        # parent selection can happen while the pad's Win+<n> focus chord is
        # still bringing the app forward.
        collapsed_group = (
            self.db.collapsed_group_for(session.workspace_id)
            if session.workspace_id
            else None
        )
        selected = actions.focus_pinned_session(
            session.workspace_id, session.session_id, collapsed_group=collapsed_group
        )
        if not selected:
            # Exact and safe, but slower because the URI handler routes through
            # github.exe. This is the failure path, not a positional shortcut.
            actions.focus_session(session.session_id or "")
        self._await_navigation(slot, session)

    def _await_navigation(self, slot: int, session) -> None:
        """Clear the pad's busy pulse once the app has actually navigated.

        Focusing takes several seconds and the duration varies, so the pad
        cannot guess it. Poll the app's own focus state until it matches, and
        give up after a bounded wait so a failed navigation still clears the
        pulse rather than blinking forever.
        """
        targets = {t for t in (session.workspace_id, session.session_id) if t}
        deadline = time.monotonic() + NAVIGATION_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.15)
            if targets & self.db.focused_ids():
                log.info("slot %s focused", slot)
                break
        else:
            log.warning("slot %s did not appear to focus within %.0fs", slot, NAVIGATION_TIMEOUT)
        self.link.send({"t": "busy_done", "k": slot})

    def _switch_to(self, slot: int | None) -> None:
        """Select a slot the daemon chose, rather than one you pressed.

        Goes through the pad for the same reason everything else does: the pad
        is the only thing here that can type. The deep link stays as the
        fallback for slots past the app's single-digit shortcuts.
        """
        if slot is None:
            return
        session = self.store.session_for_slot(slot)
        if session is None or not session.focusable:
            return
        self._last_session_slot = slot
        if slot < actions.MAX_SHORTCUT_SLOT and self.link.connected:
            self._type_chord(f"ctrl+{slot + 1}")
        else:
            actions.focus_session(session.session_id or "")

    def _type_chord(self, chord: str) -> None:
        """Have the pad type a chord.

        Not the daemon's own SendInput: that reaches nothing unless this
        process happens to be on the interactive desktop, and over RDP the
        keyboard belongs to the client machine anyway. The pad is a real USB
        keyboard, so it is the only thing here that can actually type.
        """
        if not chord:
            return
        self.link.send({"t": "type", "v": chord})

    def _focused_slot(self) -> int | None:
        """The slot the app currently has open, if it is one of ours."""
        focused = self.db.focused_ids()
        if not focused:
            return None
        for slot in range(self.cfg.slot_count):
            session = self.store.session_for_slot(slot)
            if session is None:
                continue
            if {session.workspace_id, session.session_id} & focused:
                return slot
        return None

    def _run_action(self, name: str) -> None:
        log.info("action %s", name)

        if name == "next_attention":
            slot = self.store.next_attention_slot(after=self._last_attention_slot)
            if slot is None:
                log.info("nothing needs attention")
                return
            self._last_attention_slot = slot
            self._switch_to(slot)
            return

        if name == "section_up":
            self._change_section(-1)
            return

        if name == "section_down":
            self._change_section(1)
            return

        log.warning("unknown action %r", name)

    def _change_section(self, delta: int) -> None:
        """Move rows 1-2 to the previous/next section. A no-op at either end.

        Sections do not wrap: stepping past the last one, or before the
        first, leaves the current section exactly where it was.
        """
        if not self._sections:
            return
        new_index = self._section_index + delta
        if not (0 <= new_index < len(self._sections)):
            return
        self._section_index = new_index
        section = self._sections[self._section_index]
        log.info(
            "section -> %s (%s/%s)",
            section.name,
            self._section_index + 1,
            len(self._sections),
        )
        self.store.apply_snapshot(section.sessions)
        self._push_states()
        self._push_section_leds()
        # The LEDs are the feedback for the tap, so they go first. Re-caching
        # the sidebar rows for the new section is a full tree walk (seconds),
        # so it runs off-thread like the navigation path does.
        targets = [(s.workspace_id, s.session_id) for s in section.sessions]
        threading.Thread(
            target=actions.warm_session_controls,
            args=(targets,),
            name="macropad-warm",
            daemon=True,
        ).start()

    def _section_indicator_state(self) -> str:
        """LED state name for the section-indicator key (``section_up``,
        physical row 3 key 1).

        Section 0 ("Pinned") is the plain resting colour; every group section
        gets its own solid colour by group index, so a glance at this one key
        says which section rows 1-2 are showing. Deliberately no boundary
        dimming here -- unlike the elsewhere-attention key, this key always
        names the section you are looking at, whether or not there is
        anywhere further to go.
        """
        return section_indicator_state(self._sections, self._section_index, self.cfg.section_colors)

    def _elsewhere_attention_state(self) -> str:
        """LED state name for the elsewhere-needs-you key (``section_down``,
        physical row 3 key 2).

        Rolled-up attention pooled across every section OTHER than the one
        on screen -- not just the ones in one direction, since a group
        needing you could be either above or below the current section.
        """
        return elsewhere_attention_state(self._sections, self._section_index)

    # -- outputs ---------------------------------------------------------

    def _push_states(self) -> None:
        states = self.store.slot_states()

        # Log on every *computed* change, not just successful sends, so the
        # state machine stays debuggable with no pad plugged in.
        if states != self._last_states:
            log.info("states %s", " ".join(states))
            self._last_states = states

        if states == self._last_pushed:
            return
        if self.link.send({"t": "states", "v": states}):
            self._last_pushed = states

    def _push_section_leds(self) -> None:
        """Tell the pad what the two section-nav keys should show.

        Sent by action name, never by physical key number -- see
        _ACTION_NAME_TO_KEY in keybow/code.py: the daemon keeps no copy of the
        physical layout.
        """
        states = {
            "section_up": self._section_indicator_state(),
            "section_down": self._elsewhere_attention_state(),
        }
        if states == self._last_section_states:
            return
        if self.link.send({"t": "action_states", "v": states}):
            self._last_section_states = states

    def _push_focus(self, force: bool = False) -> None:
        """Tell the pad whether the app is frontmost.

        The pad cannot see this, and it needs it: Win+<n> toggles, so a pad
        that raised the app blindly would minimise it just as often. The daemon
        can observe focus even though it cannot change it.
        """
        focused = actions.app_is_foreground()
        if focused == self._app_focused and not force:
            return
        self._app_focused = focused
        self.link.send({"t": "focus", "v": focused})

    def reconcile(self) -> None:
        # Cheap, and it must be timely: a stale answer here means either a
        # keystroke typed into the wrong window, or the app minimised by a
        # focus chord it did not need.
        self._push_focus()
        try:
            self._sections = self.db.sections(self.cfg.slot_count)
        except Exception:
            log.exception("database reconcile failed")
            return
        # Clamp rather than reset: a group emptying out (or the whole set
        # shrinking) must not silently bounce you back to "Pinned".
        if self._sections:
            self._section_index = max(0, min(self._section_index, len(self._sections) - 1))
        else:
            self._section_index = 0
        sessions = self._sections[self._section_index].sessions if self._sections else ()
        self.store.apply_snapshot(sessions)
        actions.warm_session_controls(
            (session.workspace_id, session.session_id) for session in sessions
        )
        self._push_states()
        self._push_section_leds()

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        if not self.db.available():
            log.error("cannot read %s -- is the Copilot app installed?", self.cfg.db_path)
            return 2

        try:
            self.hooks.start()
        except PortInUseError as exc:
            log.error("%s", exc)
            log.error("stop the other daemon, or change [hooks] port in your config")
            return 3

        self.link.start()
        if self.dev_tunnel is not None:
            try:
                self.dev_tunnel.start()
            except (OSError, RuntimeError) as exc:
                log.error("cannot start dev tunnel: %s", exc)
                self.link.stop()
                self.hooks.stop()
                return 5
        self._quit = threading.Event()
        self.hooks.set_quit_callback(self._quit.set)
        log.info(
            "macropad daemon up: %s slots, reconciling every %.2fs",
            self.cfg.slot_count,
            self.cfg.reconcile_interval,
        )

        try:
            while not self._quit.is_set():
                try:
                    self.reconcile()
                except Exception:
                    # A transient read failure must not take the daemon down.
                    # Dying here is what makes the pad go dead with no
                    # explanation: under pythonw there is no console, so the
                    # traceback would otherwise be lost entirely.
                    log.exception("reconcile failed; continuing")
                # Heartbeat so the pad knows we are alive even when nothing
                # changed; without it the LEDs fall back to "disconnected".
                self.link.send({"t": "hb"})
                self._quit.wait(self.cfg.reconcile_interval)
            log.info("shutdown requested; closing the pad port cleanly")
            return 0
        except KeyboardInterrupt:
            log.info("shutting down")
            return 0
        except Exception:
            log.exception("daemon stopped on an unhandled error")
            return 4
        finally:
            if self.dev_tunnel is not None:
                self.dev_tunnel.stop()
            self.link.stop()
            self.hooks.stop()


def hook_server_for(cfg: config_module.Config, callback):
    from .hook_server import HookServer

    return HookServer(cfg.hook_host, cfg.hook_port, callback)


def _quit_running_daemon(cfg: config_module.Config) -> int:
    """Ask a running daemon to stop, and wait until its port is released."""
    import http.client
    import urllib.error
    import urllib.request

    url = f"http://{cfg.hook_host}:{cfg.hook_port}/quit"
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=3)
    except (ConnectionResetError, http.client.HTTPException):
        # Expected: the daemon begins shutting down as soon as it has answered,
        # so the socket can close before the reply is fully read. The health
        # poll below is what actually decides whether it stopped.
        pass
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ConnectionResetError):
            print(f"no daemon answered on {cfg.hook_host}:{cfg.hook_port} ({exc.reason})")
            return 1

    # Wait for the hook port to actually close, which only happens after the
    # serial port has been closed too. Reporting success earlier would invite a
    # restart that collides with the outgoing daemon.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            urllib.request.urlopen(
                f"http://{cfg.hook_host}:{cfg.hook_port}/health", timeout=1
            )
        except (urllib.error.URLError, ConnectionResetError, http.client.HTTPException):
            print("daemon stopped")
            return 0
    print("daemon acknowledged the request but is still running")
    return 1

def _print_status(cfg: config_module.Config) -> int:
    db = CopilotDB(cfg.db_path)
    if not db.available():
        print(f"cannot read {cfg.db_path}", file=sys.stderr)
        return 2
    sections = db.sections(cfg.slot_count)
    section_index = 0
    store = StateStore(slot_count=cfg.slot_count)
    store.apply_snapshot(sections[section_index].sessions if sections else ())
    states = store.slot_states()

    print(f"database : {cfg.db_path}")
    print(f"hooks    : {cfg.hook_url_base}")
    print(f"installed: {hooks_install.is_installed(cfg)}")
    if sections:
        section = sections[section_index]
        print(f"section  : {section.name} ({section_index + 1}/{len(sections)})")
    else:
        print("section  : (none)")
    indicator = section_indicator_state(sections, section_index, cfg.section_colors)
    elsewhere = elsewhere_attention_state(sections, section_index)
    print(f"  key A (section_up)   {indicator:<18} {STATE_COLOURS.get(indicator, indicator)}")
    print(f"  key B (section_down) {elsewhere:<18} {STATE_COLOURS.get(elsewhere, elsewhere)}")
    print()
    for slot in range(cfg.slot_count):
        session = store.session_for_slot(slot)
        name = session.name if session else "-"
        colour = STATE_COLOURS.get(states[slot], "")
        print(f"  key {slot + 1}  {states[slot]:<15} {colour:<22} {name}")
    return 0


#: What each state looks like on the pad. Kept beside the status output so the
#: LED can always be translated back to a meaning without guessing.
STATE_COLOURS = {
    "working": "blue, breathing",
    "unread": "green, solid",
    "needs_approval": "orange, blinking",
    "interrupted": "red, blinking",
    "error": "red, solid",
    "idle": "dim white",
    "empty": "off",
}

#: Palette entries for states newer than some flashed firmware.
#:
#: The pad validates incoming states against its own palette and ignores any it
#: does not recognise, so a slot resolving to a state the firmware predates
#: would silently keep showing the previous colour. Sending these on connect
#: means a new state works without reflashing the pad.
NEW_STATE_PALETTE = {
    "interrupted": [[255, 20, 20], "pulse"],
}


def _section_color_palette(colors) -> dict:
    """Palette entries for the per-group section-indicator colours.

    Registered on every connect through the same generic ``palette`` message
    as :data:`NEW_STATE_PALETTE`, for the same reason: the firmware validates
    incoming state names against its own palette, and an unregistered
    ``section_color_N`` would just be silently ignored -- leaving the
    indicator key showing whatever it last showed instead of the section's
    colour.
    """
    return {f"section_color_{i}": [list(c), "solid"] for i, c in enumerate(colors)}


def section_indicator_state(sections, section_index: int, colours) -> str:
    """LED state name for the section-indicator key, given a resolved
    section list, the current index into it, and the configured colours.

    Shared by :meth:`Daemon._section_indicator_state` and ``--status``, which
    has no running :class:`Daemon` to ask. Section 0 ("Pinned") is the plain
    resting colour; every group section after it cycles through ``colours``
    by group index. No boundary dimming: this key always names the section
    on screen, whether or not there is anywhere further to go.
    """
    if section_index <= 0 or not sections or not colours:
        return "action"
    group_index = section_index - 1
    return f"section_color_{group_index % len(colours)}"


def elsewhere_attention_state(sections, section_index: int) -> str:
    """LED state name for the elsewhere-needs-you key, given a resolved
    section list and the current index into it.

    Shared by :meth:`Daemon._elsewhere_attention_state` and ``--status``.
    Rolled-up attention pooled across every section OTHER than the one on
    screen -- not just the ones in one direction, since a group needing you
    could be either above or below the current section.
    """
    if len(sections) <= 1:
        return "action"
    pooled = [
        s
        for i, section in enumerate(sections)
        if i != section_index
        for s in section.sessions
    ]
    return section_attention(pooled) or "action"


def _print_colours() -> int:
    print("What the session keys mean:")
    print()
    for state, colour in STATE_COLOURS.items():
        print(f"  {colour:<20}  {state}")
    print()
    print("  dim blue              daemon not connected (no live state)")
    print("  bright white flash    your key press was registered")
    print()
    print("Notes:")
    print("  * 'working' includes work done by a session's CHILD sessions, so a")
    print("    parent shows blue while its subagents run.")
    print("  * 'unread' means output you have not looked at yet; focusing the")
    print("    session clears it. 'working' is not cleared by focusing.")
    print("  * The bottom-left two keys are the dictation chord, not a session.")
    return 0


def _print_ports() -> int:
    """List serial ports and say which could be the pad.

    Mainly for confirming whether RDP COM redirection is actually forwarding
    the pad into this session, which is otherwise guesswork.
    """
    try:
        from serial.tools import list_ports

        from .serial_link import (
            CIRCUITPYTHON_VIDS,
            PROBE_BUSY,
            PROBE_PAD,
            candidate_ports,
            probe_port,
            registry_ports,
        )
    except ImportError:
        print("pyserial is not installed:  pip install pyserial", file=sys.stderr)
        return 2

    ports = list(list_ports.comports())
    enumerated = {p.device.upper() for p in ports}
    # Redirected ports show up in the registry but not in pyserial's SetupAPI
    # enumeration, so report them separately rather than appearing to see nothing.
    redirected = [p for p in registry_ports() if p.upper() not in enumerated]

    if not ports and not redirected:
        print("No serial ports visible at all.")
        print()
        _print_redirection_help()
        return 1

    if ports:
        print(f"{len(ports)} enumerated serial port(s):")
        for info in ports:
            vid = f"{info.vid:04X}" if info.vid else "----"
            note = "  <- CircuitPython vendor id" if info.vid in CIRCUITPYTHON_VIDS else ""
            print(f"  {info.device:<10} VID:{vid}  {info.description or ''}{note}")

    if redirected:
        print(f"{len(redirected)} registry-only port(s), typically RDP-redirected:")
        for name in redirected:
            print(f"  {name}")

    print()
    print("Probing for the pad (each candidate gets a few seconds)...")
    busy: list[str] = []
    for port in candidate_ports():
        outcome = probe_port(port, 115200)
        if outcome == PROBE_PAD:
            print(f"  FOUND: {port} speaks the macropad protocol")
            print()
            print(f'Set [serial] port = "{port}" to skip discovery, or leave it')
            print("unset and the daemon will find it the same way.")
            return 0
        if outcome == PROBE_BUSY:
            busy.append(port)
            print(f"  {port}: BUSY (access denied)")
        else:
            print(f"  {port}: no response")

    print()
    print("No port answered.")
    print()
    if busy:
        print(f"{', '.join(busy)} could not be opened at all -- something already")
        print("holds it. That is almost certainly why the pad is unreachable.")
        print()
        print("With an RDP-redirected port this usually means the redirection")
        print("wedged because a process was killed while holding the port open.")
        print("It does not clear on its own. Either:")
        print("  - unplug and replug the pad on your local machine, or")
        print("  - disconnect and reconnect the RDP session.")
        print()
    print("If the pad is plugged into THIS machine, check that you replugged it")
    print("after the first firmware install so boot.py could enable the USB")
    print("serial data port.")
    print()
    _print_redirection_help()
    return 1


def _print_redirection_help() -> None:
    print("If the pad is plugged into the machine you are RDP'ing FROM, enable")
    print("COM port redirection in the RDP client and reconnect:")
    print()
    print("  Remote Desktop Connection -> Show Options -> Local Resources")
    print("    -> More... -> tick 'Ports'")
    print()
    print("  or add this line to your .rdp file:")
    print("      redirectcomports:i:1")
    print()
    print("Neither needs administrator rights or any software installed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="macropad_daemon", description=__doc__)
    parser.add_argument("--config", help="path to macropad.toml")
    parser.add_argument("--status", action="store_true", help="print resolved slots and exit")
    parser.add_argument(
        "--colours",
        "--colors",
        dest="colours",
        action="store_true",
        help="explain what each LED colour means",
    )
    parser.add_argument(
        "--ports",
        action="store_true",
        help="list serial ports and probe for the pad (checks COM redirection)",
    )
    parser.add_argument("--print-hooks", action="store_true", help="print hook config JSON")
    parser.add_argument("--install-hooks", action="store_true", help="write the hook config")
    parser.add_argument(
        "--quit",
        action="store_true",
        help="ask a running daemon to shut down cleanly (do not kill it: "
        "killing it while it holds the pad's RDP-redirected port wedges that "
        "port until the pad is physically replugged)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--log-file",
        help="append logs to this file instead of the console "
        "(used when running at startup, where there is no console)",
    )
    args = parser.parse_args(argv)

    from pathlib import Path

    cfg = config_module.load(Path(args.config) if args.config else None)

    level = logging.DEBUG if args.verbose else getattr(logging, cfg.log_level, logging.INFO)
    log_format = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S" if args.log_file else "%H:%M:%S"

    if args.log_file:
        log_path = Path(args.log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate rather than growing without bound: this runs for weeks.
        handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
        logging.basicConfig(level=level, handlers=[handler])
    else:
        logging.basicConfig(level=level, format=log_format, datefmt=date_format)

    if args.print_hooks:
        print(json.dumps(hooks_install.build_config(cfg), indent=2))
        return 0

    if args.quit:
        return _quit_running_daemon(cfg)

    if args.install_hooks:
        path = hooks_install.install(cfg)
        print(f"wrote {path}")
        return 0

    if args.status:
        return _print_status(cfg)

    if args.colours:
        return _print_colours()

    if args.ports:
        return _print_ports()

    return Daemon(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
