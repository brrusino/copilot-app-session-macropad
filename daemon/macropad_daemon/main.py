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
import time

from . import config as config_module
from . import actions, hooks_install
from .copilot_db import CopilotDB
from .hook_server import PortInUseError
from .state import StateStore

log = logging.getLogger("macropad")


class Daemon:
    def __init__(self, cfg: config_module.Config) -> None:
        self.cfg = cfg
        self.db = CopilotDB(cfg.db_path)
        self.store = StateStore(slot_count=cfg.slot_count)
        self.link = self._build_link(cfg)
        self.link.set_on_connect(self._on_pad_connect)
        self.hooks = hook_server_for(cfg, self._on_hook)
        self._last_pushed: list[str] | None = None
        self._last_states: list[str] | None = None
        #: Session key most recently pressed, used to target actions that need
        #: a subject (interrupt) without guessing.
        self._last_session_slot: int | None = None
        self._last_attention_slot: int | None = None

    def _build_link(self, cfg: config_module.Config):
        """Pick the pad transport.

        ``serial`` is a pad on this machine. ``network`` accepts a relay from
        scripts/pad_bridge.py, which is what you want when the daemon runs on a
        different machine from the one the pad is plugged into -- an RDP
        session, for instance.

        Imported lazily so the hardware-free modes (--status, --print-hooks,
        --install-hooks) work without pyserial installed.
        """
        if cfg.pad_transport == "network":
            from .network_link import NetworkLink, load_or_create_token

            token = cfg.bridge_token or load_or_create_token(cfg.copilot_home)
            return NetworkLink(
                on_event=self._on_pad_event,
                host=cfg.bridge_host,
                port=cfg.bridge_port,
                token=token,
            )

        from .serial_link import SerialLink

        return SerialLink(
            on_event=self._on_pad_event,
            port=cfg.serial_port,
            baud=cfg.serial_baud,
        )

    # -- inputs ----------------------------------------------------------

    def _on_hook(self, event_type: str, session_id: str, payload: dict) -> None:
        log.debug("hook %s session=%s", event_type, session_id[:8] if session_id else "?")
        self.store.apply_hook(event_type, session_id)
        # Hooks are the low-latency path; push straight away rather than
        # waiting for the next reconcile tick.
        self._push_states()

    def _on_pad_connect(self) -> None:
        if self.cfg.palette:
            self.link.send({"t": "palette", "v": self.cfg.palette})
        if self.cfg.brightness is not None:
            self.link.send({"t": "brightness", "v": self.cfg.brightness})
        self._last_pushed = None
        self._push_states()

    def _on_pad_event(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "hello":
            log.info("pad firmware v%s, %s slots", message.get("fw"), message.get("slots"))
            return
        if kind != "down":
            # Releases matter only for the dictation chord, which the firmware
            # handles entirely on its own.
            return

        role = message.get("role")
        if role == "session":
            self._activate_session(message.get("slot"))
        elif role == "action":
            self._run_action(str(message.get("action") or ""))
        elif role == "free":
            log.info("unbound key %s pressed", message.get("k"))

    # -- actions ---------------------------------------------------------

    def _activate_session(self, slot) -> None:
        if not isinstance(slot, int):
            return
        session = self.store.session_for_slot(slot)
        if session is None or not session.focusable:
            log.info("slot %s has no session to focus", slot)
            return
        self._last_session_slot = slot
        log.info("focus slot %s -> %s", slot, session.name)
        actions.focus_session(session.session_id or "")

    def _run_action(self, name: str) -> None:
        log.info("action %s", name)

        if name == "next_attention":
            slot = self.store.next_attention_slot(after=self._last_attention_slot)
            if slot is None:
                log.info("nothing needs attention")
                return
            self._last_attention_slot = slot
            self._activate_session(slot)
            return

        if name == "approve":
            # Target the session actually asking, not whatever was last touched.
            slot = self.store.next_attention_slot()
            target = self.store.session_for_slot(slot) if slot is not None else None
            if target is None or not target.focusable:
                log.info("no session is waiting for approval")
                return
            actions.focus_then_chord(target.session_id or "", self.cfg.actions.approve)
            return

        if name == "interrupt":
            slot = self._last_session_slot
            if slot is None:
                # Fall back to whichever session is currently working.
                states = self.store.slot_states()
                slot = next((i for i, s in enumerate(states) if s == "working"), None)
            target = self.store.session_for_slot(slot) if slot is not None else None
            if target is None or not target.focusable:
                log.info("no session to interrupt")
                return
            actions.focus_then_chord(target.session_id or "", self.cfg.actions.interrupt)
            return

        if name == "new_session":
            actions.send_chord(self.cfg.actions.new_session)
            return

        log.warning("unknown action %r", name)

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

    def reconcile(self) -> None:
        try:
            sessions = self.db.pinned_sessions(self.cfg.slot_count)
        except Exception:
            log.exception("database reconcile failed")
            return
        self.store.apply_snapshot(sessions)
        self._push_states()

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
        log.info(
            "macropad daemon up: %s slots, reconciling every %.2fs",
            self.cfg.slot_count,
            self.cfg.reconcile_interval,
        )

        try:
            while True:
                self.reconcile()
                # Heartbeat so the pad knows we are alive even when nothing
                # changed; without it the LEDs fall back to "disconnected".
                self.link.send({"t": "hb"})
                time.sleep(self.cfg.reconcile_interval)
        except KeyboardInterrupt:
            log.info("shutting down")
            return 0
        finally:
            self.link.stop()
            self.hooks.stop()


def hook_server_for(cfg: config_module.Config, callback):
    from .hook_server import HookServer

    return HookServer(cfg.hook_host, cfg.hook_port, callback)

def _print_status(cfg: config_module.Config) -> int:
    db = CopilotDB(cfg.db_path)
    if not db.available():
        print(f"cannot read {cfg.db_path}", file=sys.stderr)
        return 2
    store = StateStore(slot_count=cfg.slot_count)
    store.apply_snapshot(db.pinned_sessions(cfg.slot_count))
    states = store.slot_states()

    print(f"database : {cfg.db_path}")
    print(f"hooks    : {cfg.hook_url_base}")
    print(f"installed: {hooks_install.is_installed(cfg)}")
    print()
    for slot in range(cfg.slot_count):
        session = store.session_for_slot(slot)
        name = session.name if session else "-"
        print(f"  key {slot + 1}  {states[slot]:<15} {name}")
    return 0


def _print_ports() -> int:
    """List serial ports and say which could be the pad.

    Mainly for confirming whether RDP COM redirection is actually forwarding
    the pad into this session, which is otherwise guesswork.
    """
    try:
        from serial.tools import list_ports

        from .serial_link import CIRCUITPYTHON_VIDS, candidate_ports, probe
    except ImportError:
        print("pyserial is not installed:  pip install pyserial", file=sys.stderr)
        return 2

    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports visible at all.")
        print()
        print("If the pad is plugged into an RDP client, enable COM port")
        print("redirection in the client (Local Resources -> More -> Ports),")
        print("then reconnect the session.")
        return 1

    print(f"{len(ports)} serial port(s) visible:")
    for info in ports:
        vid = f"{info.vid:04X}" if info.vid else "----"
        known = " <- CircuitPython vendor id" if info.vid in CIRCUITPYTHON_VIDS else ""
        print(f"  {info.device:<10} VID:{vid}  {info.description or ''}{known}")

    print()
    print("Probing for the pad (each candidate gets a few seconds)...")
    for port in candidate_ports():
        if probe(port, 115200):
            print(f"  FOUND: {port} speaks the macropad protocol")
            print()
            print(f'Set [serial] port = "{port}" to skip discovery, or leave it')
            print("unset and the daemon will find it the same way.")
            return 0
        print(f"  {port}: no response")

    print()
    print("No port answered. If the pad is plugged in and flashed, check that")
    print("you replugged it after the first install so boot.py could enable")
    print("the USB serial data port.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="macropad_daemon", description=__doc__)
    parser.add_argument("--config", help="path to macropad.toml")
    parser.add_argument("--status", action="store_true", help="print resolved slots and exit")
    parser.add_argument(
        "--ports",
        action="store_true",
        help="list serial ports and probe for the pad (checks COM redirection)",
    )
    parser.add_argument("--print-hooks", action="store_true", help="print hook config JSON")
    parser.add_argument("--install-hooks", action="store_true", help="write the hook config")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    from pathlib import Path

    cfg = config_module.load(Path(args.config) if args.config else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.print_hooks:
        print(json.dumps(hooks_install.build_config(cfg), indent=2))
        return 0

    if args.install_hooks:
        path = hooks_install.install(cfg)
        print(f"wrote {path}")
        return 0

    if args.status:
        return _print_status(cfg)

    if args.ports:
        return _print_ports()

    return Daemon(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
