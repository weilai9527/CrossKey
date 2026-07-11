from __future__ import annotations

import json
import socket
import threading


DISCOVERY_APP = "mac-control-windows"
DISCOVERY_INTERVAL_SECONDS = 2.0


def parse_announcement(data: bytes, host: str) -> tuple[str, int] | None:
    try:
        message = json.loads(data.decode("utf-8"))
        port = int(message.get("tcp_port", 5050))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if message.get("app") != DISCOVERY_APP or not 1 <= port <= 65535:
        return None
    return host, port


def discover_receiver(discovery_port: int, timeout: float) -> tuple[str, int] | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.bind(("", discovery_port))
        while True:
            try:
                data, address = sock.recvfrom(2048)
            except socket.timeout:
                return None
            target = parse_announcement(data, address[0])
            if target is not None:
                return target


def broadcast_receiver(
    tcp_port: int,
    discovery_port: int,
    client_connected: threading.Event,
    stop_event: threading.Event | None = None,
) -> None:
    payload = json.dumps(
        {"app": DISCOVERY_APP, "tcp_port": tcp_port}, separators=(",", ":")
    ).encode("utf-8")
    stop_event = stop_event or threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not stop_event.is_set():
            if not client_connected.is_set():
                try:
                    sock.sendto(payload, ("255.255.255.255", discovery_port))
                except OSError as exc:
                    print(f"Discovery broadcast failed: {exc}", flush=True)
            stop_event.wait(DISCOVERY_INTERVAL_SECONDS)
