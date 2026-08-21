# SPDX-License-Identifier: MIT
"""Tests for configuration loading and validation."""

import pytest

from macropad_daemon import config as config_module


def write(tmp_path, text):
    path = tmp_path / "macropad.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_config_file(tmp_path):
    cfg = config_module.load(tmp_path / "does-not-exist.toml")
    assert cfg.pad_transport == "serial"
    assert cfg.bridge_mode == "listen"
    assert cfg.hook_port == config_module.DEFAULT_HOOK_PORT
    assert cfg.bridge_port == config_module.DEFAULT_BRIDGE_PORT


def test_connect_mode_round_trips(tmp_path):
    path = write(
        tmp_path,
        """
        [pad]
        transport = "network"
        bridge_mode = "connect"
        bridge_host = "my-pc"
        bridge_port = 9001
        bridge_token = "secret"
        """,
    )
    cfg = config_module.load(path)
    assert cfg.pad_transport == "network"
    assert cfg.bridge_mode == "connect"
    assert cfg.bridge_host == "my-pc"
    assert cfg.bridge_port == 9001
    assert cfg.bridge_token == "secret"


def test_rejects_unknown_transport(tmp_path):
    path = write(tmp_path, '[pad]\ntransport = "carrier-pigeon"\n')
    with pytest.raises(ValueError, match="transport"):
        config_module.load(path)


def test_rejects_unknown_bridge_mode(tmp_path):
    """A typo here would silently listen instead of dialling, or vice versa."""
    path = write(tmp_path, '[pad]\nbridge_mode = "sideways"\n')
    with pytest.raises(ValueError, match="bridge_mode"):
        config_module.load(path)


def test_bridge_mode_is_case_insensitive(tmp_path):
    path = write(tmp_path, '[pad]\nbridge_mode = "CONNECT"\n')
    assert config_module.load(path).bridge_mode == "connect"


def test_led_overrides(tmp_path):
    path = write(
        tmp_path,
        """
        [leds]
        brightness = 0.25
        [leds.palette]
        idle = [[1, 2, 3], "solid"]
        """,
    )
    cfg = config_module.load(path)
    assert cfg.brightness == 0.25
    assert cfg.palette["idle"] == [[1, 2, 3], "solid"]


def test_brightness_levels_are_sorted_and_deduplicated(tmp_path):
    """The pad steps to the next level up, so the order is what makes the key
    predictable rather than a matter of how they were typed."""
    path = write(tmp_path, "[leds]\nlevels = [1.6, 0.35, 1.0, 1.6]\n")
    assert config_module.load(path).brightness_levels == [0.35, 1.0, 1.6]


def test_unusable_brightness_levels_are_dropped(tmp_path):
    """Zero is not a level, it is the pad looking broken."""
    path = write(tmp_path, "[leds]\nlevels = [0, 0.5, -1]\n")
    assert config_module.load(path).brightness_levels == [0.5]


def test_no_levels_configured_leaves_the_firmware_defaults(tmp_path):
    path = write(tmp_path, "[leds]\nbrightness = 1.0\n")
    assert config_module.load(path).brightness_levels == []


def test_daemon_section(tmp_path):
    path = write(
        tmp_path,
        """
        [daemon]
        slot_count = 4
        reconcile_interval = 2.5
        log_level = "DEBUG"
        """,
    )
    cfg = config_module.load(path)
    assert cfg.slot_count == 4
    assert cfg.reconcile_interval == 2.5
    assert cfg.log_level == "DEBUG"


def test_hook_url_base_uses_configured_port(tmp_path):
    path = write(tmp_path, '[hooks]\nhost = "127.0.0.1"\nport = 9999\n')
    assert config_module.load(path).hook_url_base == "http://127.0.0.1:9999"
