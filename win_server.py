from __future__ import annotations

import argparse
import socket
import threading
import time
from typing import Any

from pynput import keyboard, mouse

from auth import AuthenticationError, authenticate_server, generate_pairing_code
from clipboard_sync import ClipboardSync
from discovery import broadcast_receiver
from protocol import MAX_EVENT_LINE_BYTES, decode_event, encode_event
from settings import load_settings, save_settings
from version import __version__


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5050
DEFAULT_DISCOVERY_PORT = 5051
DEFAULT_CLIPBOARD_POLL_INTERVAL = 0.7
DEFAULT_CLIPBOARD_MAX_BYTES = 262144
DEFAULT_HEARTBEAT_TIMEOUT = 8.0


mouse_controller = mouse.Controller()
keyboard_controller = keyboard.Controller()
event_counts = {
    "mouse_move": 0,
    "mouse_click": 0,
    "key": 0,
}
control_mode_enabled = False
pressed_remote_keys: set[str | keyboard.Key] = set()
pressed_remote_buttons: set[mouse.Button] = set()
input_state_lock = threading.Lock()


SPECIAL_KEY_ALIASES = {
    "cmd": "cmd",
    "win": "cmd",
    "windows": "cmd",
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
    "insert": "insert",
    "menu": "menu",
    "f1": "f1",
    "f2": "f2",
    "f3": "f3",
    "f4": "f4",
    "f5": "f5",
    "f6": "f6",
    "f7": "f7",
    "f8": "f8",
    "f9": "f9",
    "f10": "f10",
    "f11": "f11",
    "f12": "f12",
    "f13": "f13",
    "f14": "f14",
    "f15": "f15",
    "f16": "f16",
    "f17": "f17",
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


def release_all_remote_inputs() -> None:
    """Release anything the remote Mac left pressed.

    Network loss can otherwise leave Ctrl, Shift, or a mouse button held on Windows
    until the user presses it locally. Clear the tracked state before calling the
    controllers so this function remains safe when it is called more than once.
    """
    with input_state_lock:
        keys = tuple(pressed_remote_keys)
        buttons = tuple(pressed_remote_buttons)
        pressed_remote_keys.clear()
        pressed_remote_buttons.clear()

    for key in keys:
        try:
            keyboard_controller.release(key)
        except (ValueError, OSError):
            pass
    for button in buttons:
        try:
            mouse_controller.release(button)
        except (ValueError, OSError):
            pass

    if keys or buttons:
        print(
            f"Released remote input state: {len(keys)} key(s), "
            f"{len(buttons)} mouse button(s)",
            flush=True,
        )


def handle_event(event: dict[str, Any]) -> None:
    global control_mode_enabled

    event_type = event.get("type")

    if event_type == "control_mode":
        enabled = bool(event.get("enabled"))
        if not enabled:
            release_all_remote_inputs()
        control_mode_enabled = enabled
        state = "enabled" if control_mode_enabled else "disabled"
        print(f"Mac control mode {state}", flush=True)
        return

    if not control_mode_enabled:
        return

    if event_type == "mouse_move":
        dx = int(round(float(event.get("dx", 0))))
        dy = int(round(float(event.get("dy", 0))))
        if dx or dy:
            mouse_controller.move(dx, dy)
            event_counts["mouse_move"] += 1
            if event_counts["mouse_move"] == 1 or event_counts["mouse_move"] % 50 == 0:
                print(
                    f"mouse_move events: {event_counts['mouse_move']} latest dx={dx} dy={dy}",
                    flush=True,
                )
        return

    if event_type == "mouse_click":
        button = button_from_name(str(event.get("button", "")))
        if button is None:
            return
        event_counts["mouse_click"] += 1
        print(
            f"mouse_click events: {event_counts['mouse_click']} button={button} pressed={event.get('pressed')}",
            flush=True,
        )
        if event.get("pressed"):
            mouse_controller.press(button)
            with input_state_lock:
                pressed_remote_buttons.add(button)
        else:
            mouse_controller.release(button)
            with input_state_lock:
                pressed_remote_buttons.discard(button)
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
        event_counts["key"] += 1
        if event_counts["key"] == 1 or event_counts["key"] % 20 == 0:
            print(f"key events: {event_counts['key']} latest={event}", flush=True)
        try:
            if event_type == "key_down":
                keyboard_controller.press(key)
                with input_state_lock:
                    pressed_remote_keys.add(key)
            else:
                keyboard_controller.release(key)
                with input_state_lock:
                    pressed_remote_keys.discard(key)
        except ValueError:
            print(f"Skipped unsupported key: {event}", flush=True)


def handle_client(
    conn: socket.socket,
    address: tuple[str, int],
    client_connected: threading.Event,
    sync_clipboard: bool,
    clipboard_only_control_mode: bool,
    clipboard_poll_interval: float,
    clipboard_max_bytes: int,
    pairing_code: str,
    heartbeat_timeout: float,
) -> None:
    global control_mode_enabled

    control_mode_enabled = False
    print(f"Mac connected from {address[0]}:{address[1]}", flush=True)
    try:
        authenticate_server(conn, pairing_code)
    except (AuthenticationError, OSError, ValueError) as exc:
        print(f"Authentication failed for {address[0]}: {exc}", flush=True)
        conn.close()
        client_connected.clear()
        time.sleep(1)
        return
    print("Mac authenticated", flush=True)
    print("Now press F8 on Mac, or Control + Option + Command + 8.", flush=True)
    conn.settimeout(heartbeat_timeout)
    send_lock = threading.Lock()

    def send_clipboard_event(event: dict[str, Any]) -> bool:
        try:
            with send_lock:
                conn.sendall(encode_event(event))
            return True
        except OSError as exc:
            print(f"Clipboard send failed: {exc}", flush=True)
            return False

    clipboard_sync = ClipboardSync(
        send_clipboard_event,
        lambda: control_mode_enabled,
        "windows",
        "Mac",
        sync_clipboard,
        clipboard_only_control_mode,
        clipboard_poll_interval,
        clipboard_max_bytes,
    )
    clipboard_sync.start()
    with conn, conn.makefile("rb") as reader:
        try:
            while line := reader.readline(MAX_EVENT_LINE_BYTES + 1):
                if len(line) > MAX_EVENT_LINE_BYTES:
                    print("Bad event: event is too large", flush=True)
                    break
                try:
                    event = decode_event(line)
                    if event.get("type") == "clipboard_text":
                        clipboard_sync.handle_remote_event(event)
                    else:
                        handle_event(event)
                except Exception as exc:
                    print(f"Bad event: {exc}", flush=True)
        except (OSError, TimeoutError) as exc:
            print(f"Mac connection ended: {exc}", flush=True)
        finally:
            clipboard_sync.stop()
            release_all_remote_inputs()
            control_mode_enabled = False
            client_connected.clear()
    print("Mac disconnected", flush=True)
    print("Discovery broadcast resumed", flush=True)


def serve(
    host: str,
    port: int,
    sync_clipboard: bool,
    clipboard_only_control_mode: bool,
    clipboard_poll_interval: float,
    clipboard_max_bytes: int,
    discovery_port: int,
    pairing_code: str,
    heartbeat_timeout: float,
) -> None:
    client_connected = threading.Event()
    discovery_thread = threading.Thread(
        target=broadcast_receiver,
        args=(port, discovery_port, client_connected),
        daemon=True,
    )
    discovery_thread.start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(1)
        print(f"CrossKey Windows v{__version__}", flush=True)
        print("Windows Receiver started", flush=True)
        print(f"Listening on {host}:{port}", flush=True)
        print(f"Broadcasting discovery on UDP port {discovery_port}", flush=True)
        print(f"Pairing code: {pairing_code}", flush=True)
        while True:
            print("Waiting for Mac connection...", flush=True)
            conn, address = server.accept()
            client_connected.set()
            print("Discovery broadcast paused while Mac is connected", flush=True)
            handle_client(
                conn,
                address,
                client_connected,
                sync_clipboard,
                clipboard_only_control_mode,
                clipboard_poll_interval,
                clipboard_max_bytes,
                pairing_code,
                heartbeat_timeout,
            )


def parse_args() -> argparse.Namespace:
    config = load_settings()
    parser = argparse.ArgumentParser(description="Receive Mac input events on Windows.")
    parser.add_argument("--host", default=str(config.get("host", DEFAULT_HOST)))
    parser.add_argument("--port", type=int, default=int(config.get("port", DEFAULT_PORT)))
    parser.add_argument(
        "--pairing-code",
        default=str(config.get("pairing_code", "")),
        help="Six-digit pairing code. A code is generated on first start if omitted.",
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=int(config.get("discovery_port", DEFAULT_DISCOVERY_PORT)),
        help="UDP port used to broadcast this receiver to the Mac.",
    )
    parser.add_argument(
        "--sync-clipboard",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("sync_clipboard", True)),
        help="Synchronize plain text clipboard contents with Mac.",
    )
    parser.add_argument(
        "--clipboard-only-control-mode",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("clipboard_only_control_mode", True)),
        help="Only synchronize clipboard while Mac control mode is ON.",
    )
    parser.add_argument(
        "--clipboard-poll-interval",
        type=float,
        default=float(config.get("clipboard_poll_interval", DEFAULT_CLIPBOARD_POLL_INTERVAL)),
        help="Seconds between local clipboard checks.",
    )
    parser.add_argument(
        "--clipboard-max-bytes",
        type=int,
        default=int(config.get("clipboard_max_bytes", DEFAULT_CLIPBOARD_MAX_BYTES)),
        help="Maximum UTF-8 text size to synchronize.",
    )
    parser.add_argument(
        "--heartbeat-timeout",
        type=float,
        default=float(config.get("heartbeat_timeout", DEFAULT_HEARTBEAT_TIMEOUT)),
        help="Release remote input after this many seconds without a Mac message.",
    )
    args = parser.parse_args()
    if not args.pairing_code:
        args.pairing_code = generate_pairing_code()
        config["pairing_code"] = args.pairing_code
        save_settings(config)
        print("Generated a new pairing code and saved it to config.json", flush=True)
    return args


if __name__ == "__main__":
    args = parse_args()
    serve(
        args.host,
        args.port,
        args.sync_clipboard,
        args.clipboard_only_control_mode,
        args.clipboard_poll_interval,
        args.clipboard_max_bytes,
        args.discovery_port,
        args.pairing_code,
        args.heartbeat_timeout,
    )
