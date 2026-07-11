from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auth import validate_pairing_code


CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULTS: dict[str, Any] = {
    "windows_host": "",
    "host": "0.0.0.0",
    "port": 5050,
    "auto_discover": True,
    "discovery_port": 5051,
    "discovery_timeout": 8.0,
    "mouse_scale": 1.0,
    "suppress_local_mouse": True,
    "map_cmd_to_ctrl": True,
    "debug_keys": False,
    "send_queue_size": 2000,
    "sync_clipboard": True,
    "clipboard_only_control_mode": True,
    "clipboard_poll_interval": 0.7,
    "clipboard_max_bytes": 262144,
    "pairing_code": "",
}


def load_settings(path: Path = CONFIG_PATH) -> dict[str, Any]:
    settings = dict(DEFAULTS)
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("config.json must contain a JSON object")
        settings.update(value)
    validate_settings(settings)
    return settings


def save_settings(settings: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    merged = dict(DEFAULTS)
    merged.update(settings)
    validate_settings(merged)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def validate_settings(settings: dict[str, Any]) -> None:
    for name in ("port", "discovery_port"):
        value = int(settings[name])
        if not 1 <= value <= 65535:
            raise ValueError(f"{name} must be between 1 and 65535")
    if float(settings["discovery_timeout"]) <= 0:
        raise ValueError("discovery_timeout must be positive")
    if float(settings["mouse_scale"]) <= 0:
        raise ValueError("mouse_scale must be positive")
    if int(settings["send_queue_size"]) <= 0:
        raise ValueError("send_queue_size must be positive")
    if float(settings["clipboard_poll_interval"]) <= 0:
        raise ValueError("clipboard_poll_interval must be positive")
    if int(settings["clipboard_max_bytes"]) <= 0:
        raise ValueError("clipboard_max_bytes must be positive")
    if settings.get("pairing_code"):
        validate_pairing_code(str(settings["pairing_code"]))
