from __future__ import annotations

import socket

from auth import AuthenticationError, authenticate_client
from discovery import discover_receiver
from settings import load_settings


def resolve_target(config: dict[str, object]) -> tuple[tuple[str, int] | None, str]:
    if bool(config.get("auto_discover", True)):
        discovery_port = int(config.get("discovery_port", 5051))
        discovery_timeout = float(config.get("discovery_timeout", 8.0))
        print(
            f"Scanning for Windows receiver on UDP {discovery_port} "
            f"for up to {discovery_timeout:g} seconds ..."
        )
        discovered = discover_receiver(discovery_port, discovery_timeout)
        if discovered is not None:
            return discovered, "auto-discovered"

    host = str(config.get("windows_host", "")).strip()
    if host:
        return (host, int(config.get("port", 5050))), "saved fallback"
    return None, "missing"


def main() -> None:
    config = load_settings()
    pairing_code = str(config.get("pairing_code", ""))

    if not pairing_code:
        raise SystemExit("FAILED: pairing_code is missing in config.json")

    target, source = resolve_target(config)
    if target is None:
        raise SystemExit(
            "FAILED: no receiver was discovered and windows_host is missing in config.json"
        )
    host, port = target

    print(f"Testing {host}:{port} ({source}) ...")
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            authenticate_client(sock, pairing_code)
            print("OK: network connection and pairing authentication succeeded.")
    except (OSError, AuthenticationError, ValueError) as exc:
        print(f"FAILED: {exc}")
        print()
        print("Check Windows:")
        print("1. Run: python win_server.py")
        print("2. Allow Python in Windows Firewall")
        print("3. Keep Mac and Windows on the same LAN")


if __name__ == "__main__":
    main()
