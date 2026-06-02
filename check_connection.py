from __future__ import annotations

import json
import socket
from pathlib import Path


CONFIG_PATH = Path(__file__).with_name("config.json")


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    host = config["windows_host"]
    port = int(config.get("port", 5050))

    print(f"Testing {host}:{port} ...")
    try:
        with socket.create_connection((host, port), timeout=5):
            print("OK: Mac can connect to Windows receiver.")
    except OSError as exc:
        print(f"FAILED: {exc}")
        print()
        print("Check Windows:")
        print("1. Run: python win_server.py")
        print("2. Allow Python in Windows Firewall")
        print("3. Confirm Windows IPv4 has not changed")


if __name__ == "__main__":
    main()
