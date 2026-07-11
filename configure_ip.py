from __future__ import annotations

import argparse
import ipaddress
import re
from typing import Any

from auth import validate_pairing_code
from settings import CONFIG_PATH, load_settings, save_settings

DEFAULT_PORT = 5050
DEFAULT_DISCOVERY_PORT = 5051
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(localhost|[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*)$"
)


def load_config() -> dict[str, Any]:
    return load_settings(CONFIG_PATH)


def save_config(config: dict[str, Any]) -> None:
    save_settings(config, CONFIG_PATH)


def validate_host(host: str) -> str:
    value = host.strip()
    if not value:
        raise ValueError("Windows IP cannot be empty.")

    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass

    if HOSTNAME_PATTERN.fullmatch(value):
        return value
    raise ValueError("Enter a valid IP address or host name, for example 192.168.1.25.")


def validate_port(port: str | int) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be a number.") from exc
    if not 1 <= value <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return value


def prompt_value(label: str, current: str) -> str:
    value = input(f"{label} [{current}]: ").strip()
    return current if value == "" else value


def prompt_config(config: dict[str, Any]) -> dict[str, Any]:
    current_host = str(config.get("windows_host", ""))
    current_port = str(config.get("port", DEFAULT_PORT))
    current_auto = bool(config.get("auto_discover", True))
    current_pairing_code = str(config.get("pairing_code", ""))

    print("Manual Windows connection configuration")
    print("Press Enter to keep the current value.")
    print()

    while True:
        try:
            config["windows_host"] = validate_host(prompt_value("Windows IP/host", current_host))
            break
        except ValueError as exc:
            print(f"ERROR: {exc}")

    while True:
        try:
            config["port"] = validate_port(prompt_value("Port", current_port))
            break
        except ValueError as exc:
            print(f"ERROR: {exc}")

    auto_default = "Y" if current_auto else "n"
    auto_value = input(f"Use UDP auto discovery first? [{auto_default}]: ").strip().lower()
    if auto_value:
        config["auto_discover"] = auto_value in {"y", "yes", "1", "true", "on"}
    else:
        config["auto_discover"] = current_auto

    while True:
        try:
            config["pairing_code"] = validate_pairing_code(
                prompt_value("Windows pairing code", current_pairing_code)
            )
            break
        except ValueError as exc:
            print(f"ERROR: {exc}")

    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure the Windows receiver IP manually.")
    parser.add_argument("--host", help="Windows IP address or host name.")
    parser.add_argument("--port", type=int, help="Windows receiver port.")
    parser.add_argument("--pairing-code", help="Six-digit code shown by Windows.")
    parser.add_argument(
        "--auto-discover",
        action=argparse.BooleanOptionalAction,
        help="Use UDP auto discovery before falling back to the manual IP.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    try:
        if args.host is None and args.port is None and args.pairing_code is None:
            config = prompt_config(config)
        else:
            if args.host is not None:
                config["windows_host"] = validate_host(args.host)
            if args.port is not None:
                config["port"] = validate_port(args.port)
            if args.pairing_code is not None:
                config["pairing_code"] = validate_pairing_code(args.pairing_code)
            if args.auto_discover is not None:
                config["auto_discover"] = args.auto_discover
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    config.setdefault("mouse_scale", 1.0)
    config.setdefault("suppress_local_mouse", True)
    config.setdefault("auto_discover", True)
    config.setdefault("discovery_port", DEFAULT_DISCOVERY_PORT)
    config.setdefault("sync_clipboard", True)
    config.setdefault("clipboard_only_control_mode", True)
    config.setdefault("clipboard_poll_interval", 0.7)
    config.setdefault("clipboard_max_bytes", 262144)
    save_config(config)

    print()
    print(f"Saved Windows receiver: {config['windows_host']}:{config['port']}")
    print(f"UDP auto discovery: {'on' if config['auto_discover'] else 'off'}")


if __name__ == "__main__":
    main()
