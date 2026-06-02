from __future__ import annotations

import argparse
import socket
from typing import Any

from pynput import keyboard, mouse

from protocol import decode_event


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5050


mouse_controller = mouse.Controller()
keyboard_controller = keyboard.Controller()


SPECIAL_KEY_ALIASES = {
    "cmd": "cmd",
    "cmd_l": "cmd_l",
    "cmd_r": "cmd_r",
    "ctrl": "ctrl",
    "ctrl_l": "ctrl_l",
    "ctrl_r": "ctrl_r",
    "alt": "alt",
    "alt_l": "alt_l",
    "alt_r": "alt_r",
    "shift": "shift",
    "shift_l": "shift_l",
    "shift_r": "shift_r",
    "enter": "enter",
    "return": "enter",
    "space": "space",
    "tab": "tab",
    "backspace": "backspace",
    "delete": "delete",
    "esc": "esc",
    "escape": "esc",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "page_up": "page_up",
    "page_down": "page_down",
    "caps_lock": "caps_lock",
}


def button_from_name(name: str) -> mouse.Button | None:
    return getattr(mouse.Button, name, None)


def key_from_event(event: dict[str, Any]) -> str | keyboard.Key | None:
    if "char" in event and event["char"] is not None:
        return str(event["char"])

    name = str(event.get("key", ""))
    name = name.removeprefix("Key.")
    name = SPECIAL_KEY_ALIASES.get(name, name)
    return getattr(keyboard.Key, name, None)


def handle_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")

    if event_type == "control_mode":
        state = "enabled" if event.get("enabled") else "disabled"
        print(f"Mac control mode {state}", flush=True)
        return

    if event_type == "mouse_move":
        dx = int(round(float(event.get("dx", 0))))
        dy = int(round(float(event.get("dy", 0))))
        if dx or dy:
            mouse_controller.move(dx, dy)
        return

    if event_type == "mouse_click":
        button = button_from_name(str(event.get("button", "")))
        if button is None:
            return
        if event.get("pressed"):
            mouse_controller.press(button)
        else:
            mouse_controller.release(button)
        return

    if event_type == "mouse_scroll":
        mouse_controller.scroll(
            int(event.get("dx", 0)),
            int(event.get("dy", 0)),
        )
        return

    if event_type in {"key_down", "key_up"}:
        key = key_from_event(event)
        if key is None:
            return
        try:
            if event_type == "key_down":
                keyboard_controller.press(key)
            else:
                keyboard_controller.release(key)
        except ValueError:
            print(f"Skipped unsupported key: {event}", flush=True)


def handle_client(conn: socket.socket, address: tuple[str, int]) -> None:
    print(f"Mac connected from {address[0]}:{address[1]}", flush=True)
    print("Now press F8 on Mac, or Control + Option + Command + 8.", flush=True)
    with conn, conn.makefile("rb") as reader:
        for line in reader:
            try:
                handle_event(decode_event(line))
            except Exception as exc:
                print(f"Bad event: {exc}", flush=True)
    print("Mac disconnected", flush=True)


def serve(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(1)
        print("Windows Receiver started", flush=True)
        print(f"Listening on {host}:{port}", flush=True)
        while True:
            print("Waiting for Mac connection...", flush=True)
            conn, address = server.accept()
            handle_client(conn, address)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive Mac input events on Windows.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    serve(args.host, args.port)
