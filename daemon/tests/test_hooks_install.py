# SPDX-License-Identifier: MIT
"""Tests for hook generation and installation.

The safety properties asserted here are the ones that protect other tools and
the agent itself, so they are worth pinning down explicitly.
"""

import json

from macropad_daemon import hooks_install
from macropad_daemon.config import Config


def make_config(tmp_path, port=7830):
    cfg = Config()
    cfg.copilot_home = tmp_path
    cfg.hook_port = port
    return cfg


def test_registers_exactly_the_needed_events(tmp_path):
    document = hooks_install.build_config(make_config(tmp_path))
    assert set(document["hooks"]) == set(hooks_install.EVENTS)


def test_noisy_per_tool_events_are_not_registered(tmp_path):
    """preToolUse/postToolUse fire on every tool call and buy us nothing."""
    hooks = hooks_install.build_config(make_config(tmp_path))["hooks"]
    for event in ("preToolUse", "postToolUse", "subagentStart", "subagentStop"):
        assert event not in hooks


def test_permission_request_is_observed(tmp_path):
    hooks = hooks_install.build_config(make_config(tmp_path))["hooks"]
    assert "permissionRequest" in hooks


def test_no_hook_can_emit_a_decision(tmp_path):
    """Every command must discard stdout.

    permissionRequest can return allow/deny on stdout, and Agency already
    registers a handler on it. Emitting anything would make the outcome
    undefined, so all our commands are silent.
    """
    hooks = hooks_install.build_config(make_config(tmp_path))["hooks"]
    for event, entries in hooks.items():
        for entry in entries:
            assert ">/dev/null" in entry["bash"], event
            assert "1>$null" in entry["powershell"], event


def test_every_hook_always_succeeds(tmp_path):
    """A dead daemon must never fail an agent turn."""
    hooks = hooks_install.build_config(make_config(tmp_path))["hooks"]
    for event, entries in hooks.items():
        for entry in entries:
            assert entry["bash"].rstrip().endswith("|| true"), event
            assert entry["powershell"].rstrip().endswith("exit 0"), event


def test_hooks_are_time_bounded(tmp_path):
    hooks = hooks_install.build_config(make_config(tmp_path))["hooks"]
    for entries in hooks.values():
        for entry in entries:
            assert "--connect-timeout" in entry["bash"]
            assert "--max-time" in entry["bash"]
            assert entry["timeoutSec"] > 0


def test_port_is_threaded_through(tmp_path):
    hooks = hooks_install.build_config(make_config(tmp_path, port=9111))["hooks"]
    assert "127.0.0.1:9111" in hooks["agentStop"][0]["bash"]


def test_schema_version_is_declared(tmp_path):
    assert hooks_install.build_config(make_config(tmp_path))["version"] == 1


def test_install_writes_only_our_file(tmp_path):
    """Installation must never touch Agency's or anyone else's hook file."""
    cfg = make_config(tmp_path)
    cfg.hooks_dir.mkdir(parents=True)
    foreign = cfg.hooks_dir / "agency.json"
    original = json.dumps({"version": 1, "hooks": {"agentStop": []}})
    foreign.write_text(original, encoding="utf-8")

    hooks_install.install(cfg)

    assert foreign.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in cfg.hooks_dir.iterdir()) == ["agency.json", "macropad.json"]


def test_install_is_idempotent(tmp_path):
    cfg = make_config(tmp_path)
    first = hooks_install.install(cfg).read_text(encoding="utf-8")
    second = hooks_install.install(cfg).read_text(encoding="utf-8")
    assert first == second


def test_installed_file_is_valid_json(tmp_path):
    cfg = make_config(tmp_path)
    path = hooks_install.install(cfg)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_is_installed_and_uninstall(tmp_path):
    cfg = make_config(tmp_path)
    assert hooks_install.is_installed(cfg) is False
    hooks_install.install(cfg)
    assert hooks_install.is_installed(cfg) is True
    assert hooks_install.uninstall(cfg) is True
    assert hooks_install.is_installed(cfg) is False
    assert hooks_install.uninstall(cfg) is False
