from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pynput import keyboard, mouse

from protocol import encode_event


CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_PORT = 5050


MAC_KEYCODE_NAMES = {
    36: "enter",
    48: "tab",
    49: "space",
    51: "backspace",
    53: "esc",
    55: "cmd",
    56: "shift",
    57: "caps_lock",
    58: "alt",
    59: "ctrl",
    60: "shift",
    61: "alt",
    62: "ctrl",
    76: "enter",
    100: "f8",
    115: "home",
    116: "page_up",
    117: "delete",
    119: "end",
    121: "page_down",
    123: "left",
    124: "right",
    125: "down",
    126: "up",
}


MAC_KEYCODE_CHARS = {
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    22: "6",
    23: "5",
    24: "=",
    25: "9",
    26: "7",
    27: "-",
    28: "8",
    29: "0",
    30: "]",
    31: "o",
    32: "u",
    33: "[",
    34: "i",
    35: "p",
    37: "l",
    38: "j",
    39: "'",
    40: "k",
    41: ";",
    42: "\\",
    43: ",",
    44: "/",
    45: "n",
    46: "m",
    47: ".",
    50: "`",
}


class EventSender:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, event: dict[str, Any]) -> None:
        if self.sock is None:
            return
        data = encode_event(event)
        with self.lock:
            self.sock.sendall(data)

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class MacControllerClient:
    def __init__(self, sender: EventSender, mouse_scale: float) -> None:
        self.sender = sender
        self.mouse_scale = mouse_scale
        self.enabled = False
        self.last_mouse_pos: tuple[int, int] | None = None
        self.pressed_keys: set[str] = set()
        self.cursor_guard_running = False
        self.state_lock = threading.Lock()

    def toggle_control_mode(self) -> None:
        with self.state_lock:
            self.enabled = not self.enabled
            self.last_mouse_pos = None
            enabled = self.enabled

        self.sender.send({"type": "control_mode", "enabled": enabled})
        state = "ON" if enabled else "OFF"
        print(f"Control Windows mode: {state}", flush=True)
        if enabled:
            self.start_cursor_guard()

    def is_enabled(self) -> bool:
        with self.state_lock:
            return self.enabled

    def on_mouse_move(self, x: int, y: int) -> None:
        if not self.is_enabled():
            return

        with self.state_lock:
            if self.last_mouse_pos is None:
                self.last_mouse_pos = (x, y)
                return
            last_x, last_y = self.last_mouse_pos
            self.last_mouse_pos = (x, y)

        dx = (x - last_x) * self.mouse_scale
        dy = (y - last_y) * self.mouse_scale
        if dx or dy:
            self.sender.send({"type": "mouse_move", "dx": dx, "dy": dy})

    def on_mouse_click(self, _x: int, _y: int, button: mouse.Button, pressed: bool) -> None:
        if not self.is_enabled():
            return
        self.sender.send(
            {
                "type": "mouse_click",
                "button": button.name,
                "pressed": pressed,
            }
        )

    def on_mouse_scroll(self, _x: int, _y: int, dx: int, dy: int) -> None:
        if not self.is_enabled():
            return
        self.sender.send({"type": "mouse_scroll", "dx": dx, "dy": dy})

    def on_key_down(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        with self.state_lock:
            self.pressed_keys.add(normalize_key_name(key))
            alternate_toggle_pressed = {"ctrl", "alt", "cmd", "8"}.issubset(
                self.pressed_keys
            )

        if key == keyboard.Key.f8 or alternate_toggle_pressed:
            if self.is_enabled():
                self.release_remote_modifiers()
            self.toggle_control_mode()
            return

        if not self.is_enabled():
            return
        self.sender.send(make_key_event("key_down", key))

    def on_key_up(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        with self.state_lock:
            self.pressed_keys.discard(normalize_key_name(key))

        if key == keyboard.Key.f8 or not self.is_enabled():
            return
        self.sender.send(make_key_event("key_up", key))

    def release_remote_modifiers(self) -> None:
        for key_name in ("ctrl", "alt", "cmd"):
            self.sender.send({"type": "key_up", "key": key_name})

    def start_cursor_guard(self) -> None:
        with self.state_lock:
            if self.cursor_guard_running:
                return
            self.cursor_guard_running = True

        thread = threading.Thread(target=self.cursor_guard_loop, daemon=True)
        thread.start()

    def cursor_guard_loop(self) -> None:
        if sys.platform != "darwin":
            return

        try:
            import Quartz
        except ImportError:
            return

        safe_point = Quartz.CGPointMake(10, 10)
        while self.is_enabled():
            Quartz.CGDisplayMoveCursorToPoint(Quartz.CGMainDisplayID(), safe_point)
            time.sleep(0.03)

        with self.state_lock:
            self.cursor_guard_running = False

    def on_native_mouse_move(self, dx: int, dy: int) -> None:
        if not self.is_enabled():
            return
        scaled_dx = dx * self.mouse_scale
        scaled_dy = dy * self.mouse_scale
        if scaled_dx or scaled_dy:
            self.sender.send({"type": "mouse_move", "dx": scaled_dx, "dy": scaled_dy})

    def on_native_mouse_click(self, button: str, pressed: bool) -> None:
        if not self.is_enabled():
            return
        self.sender.send(
            {
                "type": "mouse_click",
                "button": button,
                "pressed": pressed,
            }
        )

    def on_native_mouse_scroll(self, dx: int, dy: int) -> None:
        if not self.is_enabled():
            return
        self.sender.send({"type": "mouse_scroll", "dx": dx, "dy": dy})

    def on_native_key_down(self, keycode: int) -> bool:
        key_name = native_key_name(keycode)
        with self.state_lock:
            self.pressed_keys.add(key_name)
            alternate_toggle_pressed = {"ctrl", "alt", "cmd", "8"}.issubset(
                self.pressed_keys
            )

        if key_name == "f8" or alternate_toggle_pressed:
            if self.is_enabled():
                self.release_remote_modifiers()
            self.toggle_control_mode()
            return True

        if self.is_enabled():
            self.sender.send(native_key_event("key_down", keycode))
            return True
        return False

    def on_native_key_up(self, keycode: int) -> bool:
        key_name = native_key_name(keycode)
        with self.state_lock:
            self.pressed_keys.discard(key_name)

        if key_name == "f8":
            return True
        if self.is_enabled():
            self.sender.send(native_key_event("key_up", keycode))
            return True
        return False


def make_key_event(event_type: str, key: keyboard.Key | keyboard.KeyCode) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type}
    char = getattr(key, "char", None)
    if char is not None:
        event["char"] = char
    else:
        event["key"] = str(key).removeprefix("Key.")
    return event


def normalize_key_name(key: keyboard.Key | keyboard.KeyCode) -> str:
    char = getattr(key, "char", None)
    if char is not None:
        return str(char).lower()

    name = str(key).removeprefix("Key.")
    if name.startswith("ctrl"):
        return "ctrl"
    if name.startswith("alt"):
        return "alt"
    if name.startswith("cmd"):
        return "cmd"
    return name


def native_key_name(keycode: int) -> str:
    if keycode in MAC_KEYCODE_CHARS:
        return MAC_KEYCODE_CHARS[keycode]
    return MAC_KEYCODE_NAMES.get(keycode, f"keycode_{keycode}")


def native_key_event(event_type: str, keycode: int) -> dict[str, Any]:
    if keycode in MAC_KEYCODE_CHARS:
        return {"type": event_type, "char": MAC_KEYCODE_CHARS[keycode]}
    return {"type": event_type, "key": MAC_KEYCODE_NAMES.get(keycode, "")}


def run_native_macos_event_tap(client: MacControllerClient) -> None:
    import Quartz

    event_types = [
        Quartz.kCGEventKeyDown,
        Quartz.kCGEventKeyUp,
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGEventMouseMoved,
        Quartz.kCGEventLeftMouseDragged,
        Quartz.kCGEventRightMouseDragged,
        Quartz.kCGEventOtherMouseDragged,
        Quartz.kCGEventScrollWheel,
    ]
    event_mask = 0
    for event_type in event_types:
        event_mask |= Quartz.CGEventMaskBit(event_type)

    def callback(_proxy: Any, event_type: int, event: Any, _refcon: Any) -> Any:
        if event_type == Quartz.kCGEventTapDisabledByTimeout:
            Quartz.CGEventTapEnable(event_tap, True)
            return event

        if event_type in {
            Quartz.kCGEventMouseMoved,
            Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGEventRightMouseDragged,
            Quartz.kCGEventOtherMouseDragged,
        }:
            dx = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX)
            dy = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY)
            client.on_native_mouse_move(dx, dy)
            return None if client.is_enabled() else event

        if event_type in {Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp}:
            client.on_native_mouse_click(
                "left", event_type == Quartz.kCGEventLeftMouseDown
            )
            return None if client.is_enabled() else event

        if event_type in {Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp}:
            client.on_native_mouse_click(
                "right", event_type == Quartz.kCGEventRightMouseDown
            )
            return None if client.is_enabled() else event

        if event_type in {Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp}:
            client.on_native_mouse_click(
                "middle", event_type == Quartz.kCGEventOtherMouseDown
            )
            return None if client.is_enabled() else event

        if event_type == Quartz.kCGEventScrollWheel:
            dy = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventDeltaAxis1
            )
            dx = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventDeltaAxis2
            )
            client.on_native_mouse_scroll(dx, dy)
            return None if client.is_enabled() else event

        if event_type in {Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp}:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            should_suppress = (
                client.on_native_key_down(keycode)
                if event_type == Quartz.kCGEventKeyDown
                else client.on_native_key_up(keycode)
            )
            return None if should_suppress else event

        return event

    tap_locations = (
        ("HID", Quartz.kCGHIDEventTap),
        ("Session", Quartz.kCGSessionEventTap),
        ("AnnotatedSession", Quartz.kCGAnnotatedSessionEventTap),
    )
    event_tap = None
    selected_tap_name = ""
    for tap_name, tap_location in tap_locations:
        event_tap = Quartz.CGEventTapCreate(
            tap_location,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            event_mask,
            callback,
            None,
        )
        if event_tap is not None:
            selected_tap_name = tap_name
            break

    if event_tap is None:
        raise RuntimeError(
            "Cannot create macOS event tap. Enable Accessibility and Input Monitoring for Terminal."
        )

    run_loop_source = Quartz.CFMachPortCreateRunLoopSource(None, event_tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(),
        run_loop_source,
        Quartz.kCFRunLoopCommonModes,
    )
    Quartz.CGEventTapEnable(event_tap, True)
    print(f"macOS native event tap is ON: {selected_tap_name}", flush=True)
    if selected_tap_name != "HID":
        print(
            "WARNING: HID event tap was not available. Local Mac input may not be fully blocked.",
            flush=True,
        )
    Quartz.CFRunLoopRun()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(description="Capture Mac input and send it to Windows.")
    parser.add_argument("--host", default=config.get("windows_host"))
    parser.add_argument("--port", type=int, default=int(config.get("port", DEFAULT_PORT)))
    parser.add_argument(
        "--mouse-scale",
        type=float,
        default=float(config.get("mouse_scale", 1.0)),
        help="Multiply Mac mouse movement before sending it to Windows.",
    )
    parser.add_argument(
        "--suppress-local-mouse",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("suppress_local_mouse", False)),
        help="Prevent Mac from receiving mouse events while this client is running.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.host:
        raise SystemExit("Missing Windows host. Set config.json or pass --host 192.168.x.x")

    sender = EventSender(args.host, args.port)
    print(f"Connecting to Windows at {args.host}:{args.port}...", flush=True)
    try:
        sender.connect()
    except OSError as exc:
        print("", flush=True)
        print(f"ERROR: Cannot connect to Windows: {exc}", flush=True)
        print("", flush=True)
        print("请检查：", flush=True)
        print("1. Windows 端是否正在运行 python win_server.py", flush=True)
        print("2. Windows 防火墙是否允许 Python", flush=True)
        print(f"3. Windows 当前 IP 是否还是 {args.host}", flush=True)
        print("4. Mac 和 Windows 是否还在同一个局域网/Wi-Fi", flush=True)
        return
    print("Connected to Windows", flush=True)
    print("Press F8 to control Windows. Press F8 again to stop.", flush=True)
    print("If F8 is a media key, use Control + Option + Command + 8.", flush=True)
    if args.suppress_local_mouse and sys.platform == "darwin":
        print("Mac local input suppression is ON only in control mode.", flush=True)
    elif args.suppress_local_mouse:
        print("Mac local mouse suppression is ON. Press Ctrl+C to quit.", flush=True)

    client = MacControllerClient(sender, args.mouse_scale)
    if args.suppress_local_mouse and sys.platform == "darwin":
        try:
            run_native_macos_event_tap(client)
        except RuntimeError as exc:
            print("", flush=True)
            print(f"ERROR: {exc}", flush=True)
            print("", flush=True)
            print("请在 macOS 打开这两个权限，然后重新运行：", flush=True)
            print("1. 系统设置 -> 隐私与安全性 -> 辅助功能", flush=True)
            print("2. 系统设置 -> 隐私与安全性 -> 输入监控", flush=True)
            print("", flush=True)
            print("这两个页面里都要打开 Terminal。", flush=True)
            print("如果还是失败，再把 Python / Python Launcher 也加入并打开。", flush=True)
            print("Python 位置通常在：/Applications/Python 3.14/", flush=True)
            print("", flush=True)
            print("临时不拦截 Mac 本机输入也可以这样启动：", flush=True)
            print("python3 mac_client.py --no-suppress-local-mouse", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            sender.close()
        return

    mouse_listener = mouse.Listener(
        on_move=client.on_mouse_move,
        on_click=client.on_mouse_click,
        on_scroll=client.on_mouse_scroll,
        suppress=args.suppress_local_mouse,
    )
    keyboard_listener = keyboard.Listener(
        on_press=client.on_key_down,
        on_release=client.on_key_up,
    )

    try:
        mouse_listener.start()
        keyboard_listener.start()
        keyboard_listener.join()
    finally:
        mouse_listener.stop()
        sender.close()


if __name__ == "__main__":
    main()
