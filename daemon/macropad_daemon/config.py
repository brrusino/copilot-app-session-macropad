# SPDX-License-Identifier: MIT
"""Daemon configuration, loaded from TOML with sane defaults.

Search order for the config file:

1. ``$MACROPAD_CONFIG``
2. ``<repo>/config/macropad.toml``
3. ``~/.copilot/macropad.toml``

Every field has a default, so the daemon runs with no config file at all.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: USB vendor ids that CircuitPython boards present with. Used to auto-detect
#: the pad's serial port so the user does not have to hardcode a COM number.
#: 0x2E8A = Raspberry Pi (RP2040), 0x239A = Adafruit, 0x16D0 = MCS/Pimoroni.
CIRCUITPYTHON_VIDS = (0x2E8A, 0x239A, 0x16D0)

DEFAULT_HOOK_PORT = 7830

#: Port the pad bridge connects to when the pad lives on another machine.
DEFAULT_BRIDGE_PORT = 7831

DEFAULT_SLOT_COUNT = 8

#: How often to re-read the database. Hooks carry fast transitions, so this only
#: needs to be quick enough to catch things hooks cannot see (you reading a
#: session, re-pinning). Tune against measured app write latency; it is a
#: trade-off between LED freshness and idle CPU, not a correctness boundary.
DEFAULT_RECONCILE_INTERVAL = 1.0


@dataclass
class ActionBindings:
    """Keystrokes for the row 3 global actions.

    ``approve`` and ``interrupt`` are sent to the Copilot app after focusing the
    relevant session. The defaults are starting points -- confirm them against
    the running app and adjust here rather than in code.

    ``new_session`` has no verified deep link, so it is a plain chord sent to
    the app; treat its default as a guess to be calibrated.
    """

    approve: str = "enter"
    interrupt: str = "escape"
    new_session: str = "ctrl+n"


@dataclass
class Config:
    copilot_home: Path = field(default_factory=lambda: Path.home() / ".copilot")
    #: "serial" for a pad on this machine, "network" for one relayed by
    #: scripts/pad_bridge.py from the machine it is plugged into.
    pad_transport: str = "serial"
    serial_port: str | None = None
    serial_baud: int = 115200
    bridge_host: str = "0.0.0.0"
    bridge_port: int = DEFAULT_BRIDGE_PORT
    bridge_token: str | None = None
    #: "listen" = the bridge dials in to us. "connect" = we dial out to the
    #: bridge, which is what you need when this machine cannot accept inbound
    #: connections (a Cloud PC or VM behind a gateway, or a firewall you are not
    #: an admin on).
    bridge_mode: str = "listen"
    hook_host: str = "127.0.0.1"
    hook_port: int = DEFAULT_HOOK_PORT
    slot_count: int = DEFAULT_SLOT_COUNT
    reconcile_interval: float = DEFAULT_RECONCILE_INTERVAL
    brightness: float | None = None
    palette: dict[str, list] = field(default_factory=dict)
    actions: ActionBindings = field(default_factory=ActionBindings)
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.copilot_home / "data.db"

    @property
    def hooks_dir(self) -> Path:
        return self.copilot_home / "hooks"

    @property
    def hook_url_base(self) -> str:
        return f"http://{self.hook_host}:{self.hook_port}"


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("MACROPAD_CONFIG")
    if env:
        paths.append(Path(env))
    repo_config = Path(__file__).resolve().parents[2] / "config" / "macropad.toml"
    paths.append(repo_config)
    paths.append(Path.home() / ".copilot" / "macropad.toml")
    return paths


def load(path: Path | None = None) -> Config:
    """Load configuration, falling back to defaults for anything unset."""
    candidates = [path] if path else _candidate_paths()
    data: dict = {}
    for candidate in candidates:
        if candidate and candidate.is_file():
            with candidate.open("rb") as handle:
                data = tomllib.load(handle)
            break

    cfg = Config()

    if "copilot_home" in data:
        cfg.copilot_home = Path(data["copilot_home"]).expanduser()

    serial = data.get("serial", {})
    cfg.serial_port = serial.get("port") or None
    cfg.serial_baud = int(serial.get("baud", cfg.serial_baud))

    pad = data.get("pad", {})
    transport = str(pad.get("transport", cfg.pad_transport)).lower()
    if transport not in ("serial", "network"):
        raise ValueError(f"[pad] transport must be 'serial' or 'network', got {transport!r}")
    cfg.pad_transport = transport
    cfg.bridge_host = pad.get("bridge_host", cfg.bridge_host)
    cfg.bridge_port = int(pad.get("bridge_port", cfg.bridge_port))
    cfg.bridge_token = pad.get("bridge_token") or None

    bridge_mode = str(pad.get("bridge_mode", cfg.bridge_mode)).lower()
    if bridge_mode not in ("listen", "connect"):
        raise ValueError(
            f"[pad] bridge_mode must be 'listen' or 'connect', got {bridge_mode!r}"
        )
    cfg.bridge_mode = bridge_mode

    hooks = data.get("hooks", {})
    cfg.hook_host = hooks.get("host", cfg.hook_host)
    cfg.hook_port = int(hooks.get("port", cfg.hook_port))

    leds = data.get("leds", {})
    if "brightness" in leds:
        cfg.brightness = float(leds["brightness"])
    palette = leds.get("palette", {})
    if isinstance(palette, dict):
        cfg.palette = palette

    daemon = data.get("daemon", {})
    cfg.slot_count = int(daemon.get("slot_count", cfg.slot_count))
    cfg.reconcile_interval = float(daemon.get("reconcile_interval", cfg.reconcile_interval))
    cfg.log_level = daemon.get("log_level", cfg.log_level)

    actions = data.get("actions", {})
    cfg.actions = ActionBindings(
        approve=actions.get("approve", ActionBindings.approve),
        interrupt=actions.get("interrupt", ActionBindings.interrupt),
        new_session=actions.get("new_session", ActionBindings.new_session),
    )

    return cfg
