from __future__ import annotations

import argparse
import queue
import socket
import sys
import threading
import time
import uuid
from typing import Any, Callable

from pynput import keyboard, mouse

from auth import AuthenticationError, authenticate_client
from clipboard_sync import ClipboardSync
from discovery import discover_receiver
from protocol import MAX_EVENT_LINE_BYTES, decode_event, encode_event
from screen_edges import EDGE_ORDER, ScreenBounds, validate_edge_monitors
from settings import load_settings
from version import __version__


DEFAULT_PORT = 5050
DEFAULT_DISCOVERY_PORT = 5051
DEFAULT_DISCOVERY_TIMEOUT = 8.0
DEFAULT_CLIPBOARD_POLL_INTERVAL = 0.7
DEFAULT_CLIPBOARD_MAX_BYTES = 262144
DEFAULT_SEND_QUEUE_SIZE = 2000
DEFAULT_HEARTBEAT_INTERVAL = 2.0
REMOTE_MODIFIER_KEYS = {"ctrl", "alt", "cmd", "shift"}
TOGGLE_KEYS = {"ctrl", "alt", "cmd", "8"}


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
    63: "fn",
    64: "f17",
    76: "enter",
    96: "f5",
    97: "f6",
    98: "f7",
    99: "f3",
    100: "f8",
    101: "f9",
    103: "f11",
    105: "f13",
    106: "f16",
    107: "f14",
    109: "f10",
    111: "f12",
    113: "f15",
    115: "home",
    116: "page_up",
    117: "delete",
    118: "f4",
    119: "end",
    120: "f2",
    121: "page_down",
    122: "f1",
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


ConnectionTargetResolver = Callable[[], tuple[str, int] | None]
IncomingEventHandler = Callable[[dict[str, Any]], None]


class EventSender:
    def __init__(
        self,
        resolve_target: ConnectionTargetResolver,
        reconnect_delay: float = 2.0,
        send_queue_size: int = DEFAULT_SEND_QUEUE_SIZE,
        pairing_code: str = "",
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self.resolve_target = resolve_target
        self.pairing_code = pairing_code
        self.reconnect_delay = reconnect_delay
        self.heartbeat_interval = heartbeat_interval
        self.send_queue: queue.Queue[bytes] = queue.Queue(maxsize=send_queue_size)
        self.clipboard_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self.send_wakeup = threading.Event()
        self.host = ""
        self.port = DEFAULT_PORT
        self.sock: socket.socket | None = None
        self.capabilities: set[str] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reconnect_thread: threading.Thread | None = None
        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
        )
        self.reconnect_callback: Callable[[], None] | None = None
        self.incoming_handler: IncomingEventHandler | None = None
        self.writer_thread.start()
        self.heartbeat_thread.start()

    def connect(self, target: tuple[str, int] | None = None) -> None:
        if target is None:
            target = self.resolve_target()
        if target is None:
            raise OSError("No Windows receiver target is available.")

        host, port = target
        sock = socket.create_connection((host, port), timeout=10)
        try:
            result = authenticate_client(sock, self.pairing_code, capabilities=("clipboard_v2",))
        except Exception:
            sock.close()
            raise
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.lock:
            self._close_socket_locked()
            self.host = host
            self.port = port
            self.sock = sock
            self.capabilities = set(result.get("capabilities", []))
        self._start_reader(sock)

    def send(self, event: dict[str, Any]) -> bool:
        data = encode_event(event)
        with self.lock:
            sock = self.sock
            if "edge_monitors" in event and "monitor_mapping_v1" not in self.capabilities:
                print("Multi-monitor mapping requires Windows receiver v0.5.0 or newer.", flush=True)
                return False
        if sock is None:
            self._start_reconnect()
            return False

        target_queue = self.clipboard_queue if event.get("type") == "clipboard_chunk" else self.send_queue
        try:
            target_queue.put_nowait(data)
        except queue.Full:
            if event.get("type") not in {"mouse_move", "clipboard_chunk"}:
                print("Send queue is full. Dropping event.", flush=True)
            return False
        self.send_wakeup.set()
        return True

    def close(self) -> None:
        self.stop_event.set()
        with self.lock:
            self._close_socket_locked()

    def set_reconnect_callback(self, callback: Callable[[], None]) -> None:
        self.reconnect_callback = callback

    def set_incoming_handler(self, handler: IncomingEventHandler) -> None:
        self.incoming_handler = handler

    def _close_socket_locked(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def _clear_send_queue(self) -> None:
        for pending in (self.send_queue, self.clipboard_queue):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break

    def _start_reconnect(self) -> None:
        if self.stop_event.is_set():
            return
        with self.lock:
            if self.reconnect_thread is not None and self.reconnect_thread.is_alive():
                return
            self.reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                daemon=True,
            )
            self.reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                print("Connection is down. Trying to reconnect...", flush=True)
                self._clear_send_queue()
                last_target = (self.host, self.port) if self.host else None
                if last_target is None:
                    self.connect()
                else:
                    try:
                        self.connect(last_target)
                    except OSError:
                        self.connect()
            except OSError as exc:
                print(f"Reconnect failed: {exc}", flush=True)
                self.stop_event.wait(self.reconnect_delay)
                continue

            print(f"Reconnected to Windows at {self.host}:{self.port}", flush=True)
            if self.reconnect_callback is not None:
                self.reconnect_callback()
            with self.lock:
                still_connected = self.sock is not None
            if still_connected:
                return

    def _start_reader(self, sock: socket.socket) -> None:
        threading.Thread(
            target=self._reader_loop,
            args=(sock,),
            daemon=True,
        ).start()

    def _reader_loop(self, sock: socket.socket) -> None:
        try:
            with sock.makefile("rb") as reader:
                while line := reader.readline(MAX_EVENT_LINE_BYTES + 1):
                    if len(line) > MAX_EVENT_LINE_BYTES:
                        print("Bad event from Windows: event is too large", flush=True)
                        break
                    if self.incoming_handler is None:
                        continue
                    try:
                        self.incoming_handler(decode_event(line))
                    except Exception as exc:
                        print(f"Bad event from Windows: {exc}", flush=True)
        except OSError as exc:
            if not self.stop_event.is_set():
                print(f"Connection lost while reading events: {exc}", flush=True)
        finally:
            if not self.stop_event.is_set():
                with self.lock:
                    if self.sock is sock:
                        self._close_socket_locked()
                        self._clear_send_queue()
                        should_reconnect = True
                    else:
                        should_reconnect = False
                if should_reconnect:
                    self._start_reconnect()

    def _writer_loop(self) -> None:
        while not self.stop_event.is_set():
            self.send_wakeup.clear()
            try:
                data = self.send_queue.get_nowait()
            except queue.Empty:
                try:
                    data = self.clipboard_queue.get_nowait()
                except queue.Empty:
                    self.send_wakeup.wait(0.2)
                    continue

            with self.lock:
                sock = self.sock
            if sock is None:
                self._clear_send_queue()
                self._start_reconnect()
                continue

            try:
                sock.sendall(data)
            except OSError as exc:
                print(f"Connection lost while sending event: {exc}", flush=True)
                with self.lock:
                    if self.sock is sock:
                        self._close_socket_locked()
                        self._clear_send_queue()
                self._start_reconnect()

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.heartbeat_interval):
            with self.lock:
                connected = self.sock is not None
            if connected:
                self.send({"type": "heartbeat"})


class MacControllerClient:
    def __init__(
        self,
        sender: EventSender,
        mouse_scale: float,
        map_cmd_to_ctrl: bool,
        debug_keys: bool,
        auto_switch_edge: str = "off",
        edge_margin: int = 2,
        edge_monitors: dict[str, int] | None = None,
    ) -> None:
        self.sender = sender
        self.mouse_scale = mouse_scale
        self.map_cmd_to_ctrl = map_cmd_to_ctrl
        self.debug_keys = debug_keys
        self.auto_switch_edge = auto_switch_edge
        self.edge_margin = edge_margin
        self.edge_monitors = validate_edge_monitors(edge_monitors or {})
        self.active_edge = next((edge for edge in EDGE_ORDER if edge in self.edge_monitors),
                                auto_switch_edge)
        self.enabled = False
        self.last_mouse_pos: tuple[int, int] | None = None
        self.pressed_keys: set[str] = set()
        self.state_lock = threading.RLock()
        self.session_id = ""
        self.edge_cooldown_until = 0.0
        self.warp_cursor: Callable[[tuple[int, int]], None] | None = None

    def screen_bounds(self) -> ScreenBounds | None:
        if not all(hasattr(self, name) for name in (
            "screen_min_x", "screen_min_y", "screen_max_x", "screen_max_y"
        )):
            return None
        return ScreenBounds(self.screen_min_x, self.screen_min_y,
                            self.screen_max_x, self.screen_max_y)

    def control_event(self, enabled: bool, position: float | None = None) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "control_mode", "enabled": enabled, "session_id": self.session_id
        }
        if enabled and self.active_edge != "off" and self.screen_bounds() is not None:
            event.update(edge=self.active_edge, edge_margin=self.edge_margin)
            if self.edge_monitors:
                event["edge_monitors"] = dict(self.edge_monitors)
            if position is not None:
                event["position"] = position
        return event

    def toggle_control_mode(self, position: float | None = None, edge: str | None = None) -> bool:
        with self.state_lock:
            enabled = not self.enabled
            if enabled:
                if edge is not None:
                    self.active_edge = edge
                self.session_id = uuid.uuid4().hex
            # Never capture local input if the activation cannot be queued.
            if not self.sender.send(self.control_event(enabled, position)) and enabled:
                return False
            self.enabled = enabled
            self.last_mouse_pos = None
            if not enabled:
                self.edge_cooldown_until = time.monotonic() + 0.25

        state = "ON" if enabled else "OFF"
        print(f"Control Windows mode: {state}", flush=True)
        return True

    def handle_remote_event(self, event: dict[str, Any]) -> None:
        if event.get("type") not in {"return_control", "control_rejected"}:
            return
        with self.state_lock:
            if not self.enabled or event["session_id"] != self.session_id:
                return
            rejected = event["type"] == "control_rejected"
            allowed_edges = self.edge_monitors or {self.auto_switch_edge: None}
            if not rejected and event["edge"] not in allowed_edges:
                return
            bounds = self.screen_bounds()
            self.enabled = False
            self.last_mouse_pos = None
            self.pressed_keys.clear()
            self.edge_cooldown_until = time.monotonic() + 0.25
            # Windows already stopped accepting input and released held buttons.
            # Warp while holding the same lock as the native event callback.
            if not rejected:
                self.active_edge = event["edge"]
            if not rejected and bounds is not None and self.warp_cursor is not None:
                try:
                    self.warp_cursor(bounds.landing(
                        self.active_edge, event["position"], self.edge_margin
                    ))
                except Exception as exc:
                    print(f"Could not reposition Mac cursor: {exc}", flush=True)
        if rejected:
            print(f"Windows rejected layout: {event['reason']}", flush=True)
        else:
            print("Returned to Mac from Windows screen edge.", flush=True)

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
            alternate_toggle_pressed = TOGGLE_KEYS.issubset(self.pressed_keys)

        if key == keyboard.Key.f8 or alternate_toggle_pressed:
            if self.is_enabled():
                self.release_remote_modifiers()
            self.toggle_control_mode()
            return

        if not self.is_enabled():
            return
        self.send_key_event(make_key_event("key_down", key))

    def on_key_up(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        with self.state_lock:
            self.pressed_keys.discard(normalize_key_name(key))

        if key == keyboard.Key.f8 or not self.is_enabled():
            return
        self.send_key_event(make_key_event("key_up", key))

    def release_remote_modifiers(self) -> None:
        for key_name in REMOTE_MODIFIER_KEYS:
            self.send_key_event({"type": "key_up", "key": key_name})

    def recover_remote_state_after_reconnect(self) -> None:
        with self.state_lock:
            self.last_mouse_pos = None
            if not self.enabled:
                return
            self.session_id = uuid.uuid4().hex
            if not self.sender.send(self.control_event(True)):
                self.enabled = False
                self.edge_cooldown_until = time.monotonic() + 0.25
                return
            self.release_remote_modifiers()

    def send_key_event(self, event: dict[str, Any]) -> None:
        mapped_event = self.map_remote_key_event(event)
        if self.debug_keys:
            print(f"send key: {mapped_event}", flush=True)
        self.sender.send(mapped_event)

    def map_remote_key_event(self, event: dict[str, Any]) -> dict[str, Any]:
        key_name = str(event.get("key", ""))
        if not self.map_cmd_to_ctrl or not key_name.startswith("cmd"):
            return event
        mapped_event = dict(event)
        mapped_event["key"] = "ctrl"
        return mapped_event

    def on_native_mouse_move(self, x: float, y: float, dx: int, dy: int) -> None:
        if not self.is_enabled():
            if self.try_activate_from_edge(x, dx, y, dy):
                return
            return

        if not dx and not dy:
            with self.state_lock:
                if self.last_mouse_pos is None:
                    self.last_mouse_pos = (int(x), int(y))
                    return
                last_x, last_y = self.last_mouse_pos
                self.last_mouse_pos = (int(x), int(y))
            dx = int(x) - last_x
            dy = int(y) - last_y
        else:
            with self.state_lock:
                self.last_mouse_pos = (int(x), int(y))

        scaled_dx = dx * self.mouse_scale
        scaled_dy = dy * self.mouse_scale
        if scaled_dx or scaled_dy:
            self.sender.send({"type": "mouse_move", "dx": scaled_dx, "dy": scaled_dy})

    def set_screen_horizontal_bounds(self, min_x: float, max_x: float) -> None:
        with self.state_lock:
            self.screen_min_x = min_x
            self.screen_max_x = max_x

    def set_screen_vertical_bounds(self, min_y: float, max_y: float) -> None:
        with self.state_lock:
            self.screen_min_y = min_y
            self.screen_max_y = max_y

    def try_activate_from_edge(
        self,
        x: float,
        dx: int,
        y: float | None = None,
        dy: int = 0,
    ) -> bool:
        with self.state_lock:
            if (self.enabled or (not self.edge_monitors and self.auto_switch_edge == "off")
                    or time.monotonic() < self.edge_cooldown_until):
                return False
            bounds = self.screen_bounds()
            if bounds is None:
                return False
            if y is None:
                y = (bounds.top + bounds.bottom - 1) / 2
            if not bounds.contains(x, y):
                return False
            edges = self.edge_monitors or {self.auto_switch_edge: None}
            candidates = [edge for edge in EDGE_ORDER if edge in edges and bounds.crossing(
                edge, x, y, dx, dy, self.edge_margin
            )]
            if not candidates:
                return False
            # At a corner, prefer the direction with the larger outward delta.
            edge = max(candidates, key=lambda item: abs(dx) if item in {"left", "right"} else abs(dy))
            position = bounds.fraction(edge, x, y)
            if not self.toggle_control_mode(position, edge):
                return False
        print(
            f"Activated Windows control from Mac {edge} edge. "
            "Move back across the Windows edge or press F8 to return.",
            flush=True,
        )
        return True

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
            alternate_toggle_pressed = TOGGLE_KEYS.issubset(self.pressed_keys)

        if self.debug_keys:
            print(f"native key down: keycode={keycode} name={key_name}", flush=True)

        if key_name == "f8" or alternate_toggle_pressed:
            if self.is_enabled():
                self.release_remote_modifiers()
            self.toggle_control_mode()
            return True

        if self.is_enabled():
            self.send_key_event(native_key_event("key_down", keycode))
            return True
        return False

    def on_native_key_up(self, keycode: int) -> bool:
        key_name = native_key_name(keycode)
        with self.state_lock:
            self.pressed_keys.discard(key_name)

        if self.debug_keys:
            print(f"native key up: keycode={keycode} name={key_name}", flush=True)

        if key_name == "f8":
            return True
        if self.is_enabled():
            self.send_key_event(native_key_event("key_up", keycode))
            return True
        return False

    def on_native_flags_changed(self, active_modifiers: set[str]) -> bool:
        modifier_events: list[dict[str, Any]] = []
        with self.state_lock:
            pressed_modifiers = self.pressed_keys & REMOTE_MODIFIER_KEYS
            released_modifiers = pressed_modifiers - active_modifiers
            newly_pressed_modifiers = active_modifiers - pressed_modifiers
            self.pressed_keys -= released_modifiers
            self.pressed_keys |= newly_pressed_modifiers

        if self.debug_keys:
            print(
                "native flags changed: "
                f"active={sorted(active_modifiers)} "
                f"pressed={sorted(newly_pressed_modifiers)} "
                f"released={sorted(released_modifiers)}",
                flush=True,
            )

        for key_name in sorted(released_modifiers):
            modifier_events.append({"type": "key_up", "key": key_name})
        for key_name in sorted(newly_pressed_modifiers):
            modifier_events.append({"type": "key_down", "key": key_name})

        if self.is_enabled():
            for event in modifier_events:
                self.send_key_event(event)
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

    main_display_bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    screen_min_x = float(main_display_bounds.origin.x)
    client.set_screen_horizontal_bounds(
        screen_min_x,
        screen_min_x + float(main_display_bounds.size.width),
    )
    screen_min_y = float(main_display_bounds.origin.y)
    client.set_screen_vertical_bounds(
        screen_min_y,
        screen_min_y + float(main_display_bounds.size.height),
    )

    def warp_cursor(point: tuple[int, int]) -> None:
        error = Quartz.CGWarpMouseCursorPosition(point)
        if error:
            raise RuntimeError(f"CGWarpMouseCursorPosition failed: {error}")

    client.warp_cursor = warp_cursor

    event_types = [
        Quartz.kCGEventKeyDown,
        Quartz.kCGEventKeyUp,
        Quartz.kCGEventFlagsChanged,
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

    modifier_flags = {
        "shift": Quartz.kCGEventFlagMaskShift,
        "ctrl": Quartz.kCGEventFlagMaskControl,
        "alt": Quartz.kCGEventFlagMaskAlternate,
        "cmd": Quartz.kCGEventFlagMaskCommand,
    }

    def active_modifiers_from_event(event: Any) -> set[str]:
        flags = Quartz.CGEventGetFlags(event)
        return {
            key_name
            for key_name, flag_mask in modifier_flags.items()
            if flags & flag_mask
        }

    def process_event(_proxy: Any, event_type: int, event: Any, _refcon: Any) -> Any:
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
            point = Quartz.CGEventGetLocation(event)
            client.on_native_mouse_move(point.x, point.y, dx, dy)
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

        if event_type == Quartz.kCGEventFlagsChanged:
            should_suppress = client.on_native_flags_changed(
                active_modifiers_from_event(event)
            )
            return None if should_suppress else event

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

    def callback(proxy: Any, event_type: int, event: Any, refcon: Any) -> Any:
        # Serialize local input with return notifications from the socket reader.
        with client.state_lock:
            return process_event(proxy, event_type, event, refcon)

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
    return load_settings()


def discover_windows_host(discovery_port: int, timeout: float) -> tuple[str, int] | None:
    print(f"Searching for Windows receiver on UDP port {discovery_port}...", flush=True)
    target = discover_receiver(discovery_port, timeout)
    if target is not None:
        print(f"Found Windows receiver at {target[0]}:{target[1]}", flush=True)
    return target


def make_connection_target_resolver(args: argparse.Namespace) -> ConnectionTargetResolver:
    configured_host = args.host
    configured_port = args.port
    use_discovery = args.discover and not args.host_from_cli

    def resolve_target() -> tuple[str, int] | None:
        if use_discovery:
            discovered = discover_windows_host(args.discovery_port, args.discovery_timeout)
            if discovered is not None:
                return discovered
            if configured_host:
                print(
                    "Discovery timed out. "
                    f"Falling back to configured host {configured_host}:{configured_port}.",
                    flush=True,
                )

        if configured_host:
            return configured_host, configured_port
        return None

    return resolve_target


def parse_args() -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(description="Capture Mac input and send it to Windows.")
    parser.add_argument("--host", help="Windows IP/host. Passing this skips UDP discovery for this run.")
    parser.add_argument("--port", type=int, help="Windows receiver TCP port.")
    parser.add_argument(
        "--pairing-code",
        default=str(config.get("pairing_code", "")),
        help="Six-digit code shown by the Windows receiver.",
    )
    parser.add_argument(
        "--discover",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("auto_discover", True)),
        help="Use UDP broadcast discovery before falling back to config.json.",
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=int(config.get("discovery_port", DEFAULT_DISCOVERY_PORT)),
        help="UDP port used to find the Windows receiver.",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=float(config.get("discovery_timeout", DEFAULT_DISCOVERY_TIMEOUT)),
        help="Seconds to wait for UDP discovery.",
    )
    parser.add_argument(
        "--mouse-scale",
        type=float,
        default=float(config.get("mouse_scale", 1.0)),
        help="Multiply Mac mouse movement before sending it to Windows.",
    )
    parser.add_argument(
        "--auto-switch-edge",
        choices=("off", "left", "right", "top", "bottom"),
        default=None,
        help="Automatically enter Windows control at a Mac main-display edge.",
    )
    parser.add_argument(
        "--edge-margin",
        type=int,
        default=int(config.get("edge_margin", 2)),
        help="Distance from the configured screen edge that activates control.",
    )
    parser.add_argument(
        "--suppress-local-mouse",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("suppress_local_mouse", False)),
        help="Prevent Mac from receiving mouse events while this client is running.",
    )
    parser.add_argument(
        "--map-cmd-to-ctrl",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("map_cmd_to_ctrl", True)),
        help="Send Mac Command as Windows Ctrl, useful for Command+C/V shortcuts.",
    )
    parser.add_argument(
        "--debug-keys",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("debug_keys", False)),
        help="Print captured key events and forwarded key events.",
    )
    parser.add_argument(
        "--send-queue-size",
        type=int,
        default=int(config.get("send_queue_size", DEFAULT_SEND_QUEUE_SIZE)),
        help="Maximum number of encoded events waiting for the socket writer.",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=float(config.get("heartbeat_interval", DEFAULT_HEARTBEAT_INTERVAL)),
        help="Seconds between connection heartbeat messages.",
    )
    parser.add_argument(
        "--sync-clipboard",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("sync_clipboard", True)),
        help="Synchronize text, images, files and rich text with Windows.",
    )
    parser.add_argument(
        "--clipboard-only-control-mode",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("clipboard_only_control_mode", True)),
        help="Only synchronize clipboard while control mode is ON.",
    )
    parser.add_argument(
        "--clipboard-poll-interval",
        type=float,
        default=float(
            config.get("clipboard_poll_interval", DEFAULT_CLIPBOARD_POLL_INTERVAL)
        ),
        help="Seconds between local clipboard checks.",
    )
    parser.add_argument(
        "--clipboard-max-bytes",
        type=int,
        default=int(config.get("clipboard_max_bytes", DEFAULT_CLIPBOARD_MAX_BYTES)),
        help="Maximum UTF-8 text size to synchronize.",
    )
    args = parser.parse_args()
    # An explicit single-edge override also disables any saved multi-screen map.
    edge_override = args.auto_switch_edge is not None
    args.edge_monitors = {} if edge_override else dict(config.get("edge_monitors", {}))
    if not edge_override:
        args.auto_switch_edge = str(config.get("auto_switch_edge", "off"))
    if not 1 <= args.edge_margin <= 100:
        parser.error("edge-margin must be between 1 and 100")
    args.host_from_cli = args.host is not None
    args.host = args.host if args.host is not None else config.get("windows_host")
    args.port = args.port if args.port is not None else int(config.get("port", DEFAULT_PORT))
    if not args.pairing_code:
        parser.error("pairing code is missing; set pairing_code in config.json or use --pairing-code")
    return args


def main() -> None:
    args = parse_args()
    if (args.auto_switch_edge != "off" or args.edge_monitors) and (
        sys.platform != "darwin" or not args.suppress_local_mouse
    ):
        print(
            "WARNING: edge switching requires macOS native input suppression; "
            "edge switching is disabled for this run.",
            flush=True,
        )
        args.auto_switch_edge = "off"
        args.edge_monitors = {}
    print(f"CrossKey Mac v{__version__}", flush=True)
    resolve_target = make_connection_target_resolver(args)
    initial_target = resolve_target()
    if initial_target is None:
        raise SystemExit(
            "Missing Windows host. Start win_server.py for auto discovery, set config.json, "
            "or pass --host 192.168.x.x"
        )

    sender = EventSender(
        resolve_target,
        send_queue_size=args.send_queue_size,
        pairing_code=args.pairing_code,
        heartbeat_interval=args.heartbeat_interval,
    )
    initial_host, initial_port = initial_target
    print(f"Connecting to Windows at {initial_host}:{initial_port}...", flush=True)
    try:
        sender.connect(initial_target)
    except (OSError, AuthenticationError, ValueError) as exc:
        sender.close()
        print("", flush=True)
        print(f"ERROR: Cannot connect to Windows: {exc}", flush=True)
        print("", flush=True)
        print("请检查：", flush=True)
        print("1. Windows 端是否正在运行 python win_server.py", flush=True)
        print("2. Windows 防火墙是否允许 Python", flush=True)
        print(f"3. Windows 当前 IP 是否还是 {initial_host}", flush=True)
        print("4. Mac 和 Windows 是否还在同一个局域网/Wi-Fi", flush=True)
        return
    if args.edge_monitors and "monitor_mapping_v1" not in sender.capabilities:
        sender.close()
        raise SystemExit("Multi-monitor mapping requires Windows receiver v0.5.0 or newer. "
                         "Update and restart win_server.py first.")
    print("Connected and authenticated with Windows", flush=True)
    print("Press F8 to control Windows. Press F8 again to stop.", flush=True)
    print("If F8 is a media key, use Control + Option + Command + 8.", flush=True)
    if args.suppress_local_mouse and sys.platform == "darwin":
        print("Mac local input suppression is ON only in control mode.", flush=True)
    elif args.suppress_local_mouse:
        print("Mac local mouse suppression is ON. Press Ctrl+C to quit.", flush=True)
    if args.map_cmd_to_ctrl:
        print("Mac Command is mapped to Windows Ctrl for shortcuts.", flush=True)
    if args.debug_keys:
        print("Keyboard debug logging is ON.", flush=True)
    if args.edge_monitors:
        for edge in EDGE_ORDER:
            if edge in args.edge_monitors:
                print(f"Mac {edge} edge -> Windows monitor {args.edge_monitors[edge]}", flush=True)
    elif args.auto_switch_edge != "off":
        print(
            f"Mac {args.auto_switch_edge} edge activates Windows control; "
            "the opposite Windows edge or F8 returns to Mac.",
            flush=True,
        )

    client = MacControllerClient(
        sender,
        args.mouse_scale,
        args.map_cmd_to_ctrl,
        args.debug_keys,
        args.auto_switch_edge,
        args.edge_margin,
        args.edge_monitors,
    )
    clipboard_sync = ClipboardSync(
        sender.send,
        client.is_enabled,
        "mac",
        "Windows",
        args.sync_clipboard,
        args.clipboard_only_control_mode,
        args.clipboard_poll_interval,
        args.clipboard_max_bytes,
        supports_extended=lambda: "clipboard_v2" in sender.capabilities,
    )
    def recover_after_reconnect() -> None:
        clipboard_sync.reset_connection()
        client.recover_remote_state_after_reconnect()

    sender.set_reconnect_callback(recover_after_reconnect)
    def handle_incoming_event(event: dict[str, Any]) -> None:
        if event.get("type") in {"return_control", "control_rejected"}:
            client.handle_remote_event(event)
        else:
            clipboard_sync.handle_remote_event(event)

    sender.set_incoming_handler(handle_incoming_event)
    clipboard_sync.start()
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
            clipboard_sync.stop()
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
        clipboard_sync.stop()
        sender.close()


if __name__ == "__main__":
    main()
