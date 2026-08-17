# SPDX-License-Identifier: MIT
"""Generates and installs ``~/.copilot/hooks/macropad.json``.

The Copilot CLI reads every ``*.json`` in its hooks directory, so this is purely
additive: we write **our own file** and never read, merge or modify
``agency.json``, ``constellation.json`` or anything else already there.

Which events we register
------------------------
Only the six that the LED state machine actually needs:

===================== ====================================================
``sessionStart``      reset a slot's state
``sessionEnd``        reset a slot's state
``userPromptSubmitted`` a turn began -> working
``agentStop``         the turn finished -> idle / unread
``permissionRequest`` the agent is blocked on you -> needs approval
``errorOccurred``     the turn failed -> error
===================== ====================================================

``preToolUse`` and ``postToolUse`` are deliberately **not** registered. They fire
on every single tool call, and they tell the state machine nothing it does not
already know from ``userPromptSubmitted`` and ``agentStop`` -- so registering
them would add an HTTP round trip to every tool invocation to buy nothing. The
same reasoning excludes ``subagentStart``/``subagentStop``. If the daemon starts
mid-turn, the database reconcile picks the session up via ``sessions.is_running``.

Safety properties of the generated commands
-------------------------------------------
* **Silent.** stdout is discarded, so no hook ever emits a decision. This matters
  most for ``permissionRequest``, which *can* return allow/deny -- Agency already
  registers a handler there and two hooks both answering is undefined behaviour.
  We watch that event only to turn a LED amber.
* **Always succeeds.** Every command ends in ``|| true`` / ``exit 0``, so a dead
  daemon can never fail an agent turn.
* **Fast.** Short connect and total timeouts keep the agent from ever waiting on
  us.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config

HOOK_FILENAME = "macropad.json"

#: Only these events; see the module docstring for why the noisy ones are out.
EVENTS = (
    "sessionStart",
    "sessionEnd",
    "userPromptSubmitted",
    "agentStop",
    "permissionRequest",
    "errorOccurred",
)

CONNECT_TIMEOUT = 1
MAX_TIME = 2
TIMEOUT_SEC = 5


def _bash_command(url: str) -> str:
    return (
        f"curl -sS --connect-timeout {CONNECT_TIMEOUT} --max-time {MAX_TIME} "
        f"-H 'Content-Type: application/json' --data-binary @- '{url}' "
        f">/dev/null 2>&1 || true"
    )


def _powershell_command(url: str) -> str:
    return (
        f"try {{ curl.exe -sS --connect-timeout {CONNECT_TIMEOUT} --max-time {MAX_TIME} "
        f"-H 'Content-Type: application/json' --data-binary '@-' '{url}' "
        f"1>$null 2>$null }} catch {{ }}; exit 0"
    )


def build_config(cfg: Config) -> dict:
    """Build the hook document for this daemon's endpoint."""
    hooks: dict[str, list[dict]] = {}
    for event in EVENTS:
        url = f"{cfg.hook_url_base}/hook/{event}"
        hooks[event] = [
            {
                "type": "command",
                "comment": "Copilot macropad LED state (observe-only, never emits a decision)",
                "cwd": ".",
                "bash": _bash_command(url),
                "powershell": _powershell_command(url),
                "timeoutSec": TIMEOUT_SEC,
            }
        ]
    return {"version": 1, "hooks": hooks}


def hook_path(cfg: Config) -> Path:
    return cfg.hooks_dir / HOOK_FILENAME


def is_installed(cfg: Config) -> bool:
    return hook_path(cfg).is_file()


def install(cfg: Config) -> Path:
    """Write our hook file. Touches no other file in the hooks directory."""
    target = hook_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = build_config(cfg)
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return target


def uninstall(cfg: Config) -> bool:
    target = hook_path(cfg)
    if target.is_file():
        target.unlink()
        return True
    return False
