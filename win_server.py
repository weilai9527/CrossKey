from __future__ import annotations

import argparse
import ctypes
import errno
import os
import select
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from pynput import keyboard, mouse

from auth import AuthenticationError, authenticate_server, generate_pairing_code
from clipboard_sync import ClipboardSync
from discovery import broadcast_receiver
from protocol import MAX_EVENT_LINE_BYTES, decode_event, encode_event
from screen_edges import EDGE_ORDER, OPPOSITE_EDGE, ScreenBounds
from settings import load_settings, save_settings
from version import __version__
from windows_monitors import WindowsMonitor, describe_monitors, enable_dpi_awareness, list_monitors


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5050
DEFAULT_DISCOVERY_PORT = 5051
DEFAULT_CLIPBOARD_POLL_INTERVAL = 0.7
DEFAULT_CLIPBOARD_MAX_BYTES = 262144
DEFAULT_HEARTBEAT_TIMEOUT = 30.0
StatusCallback = Callable[[dict[str, Any]], None]
_receiver_run_lock = threading.Lock()


class _CancellableConnection:
    """Socket operations that observe stop without cross-thread socket closure.

    Winsock shutdown does not reliably wake Python's timeout/select wait. Keep
    the real socket nonblocking and bound each readiness wait instead. Timeout
    changes made by authentication apply to the whole operation, not each poll.
    """

    def __init__(self, conn: socket.socket, stop_event: threading.Event):
        self._socket = conn
        self._stop_event = stop_event
        self._timeout = conn.gettimeout()
        conn.setblocking(False)

    def _check_stopped(self) -> None:
        if self._stop_event.is_set():
            raise OSError("Receiver stopped")

    def _deadline(self) -> float | None:
        return None if self._timeout is None else time.monotonic() + self._timeout

    def _wait(self, *, reading: bool, deadline: float | None) -> None:
        while True:
            self._check_stopped()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise socket.timeout("timed out")
            interval = 0.05 if remaining is None else min(0.05, remaining)
            readable, writable, _ = select.select(
                [self._socket] if reading else [],
                [] if reading else [self._socket], [], interval,
            )
            if readable or writable:
                return

    def recv(self, size: int) -> bytes:
        deadline = self._deadline()
        while True:
            self._check_stopped()
            try:
                return self._socket.recv(size)
            except BlockingIOError:
                if self._timeout == 0:
                    raise
                self._wait(reading=True, deadline=deadline)

    def sendall(self, data: bytes) -> None:
        deadline = self._deadline()
        remaining = memoryview(data)
        while remaining:
            self._check_stopped()
            if deadline is not None and self._timeout != 0 and time.monotonic() >= deadline:
                raise socket.timeout("timed out")
            try:
                sent = self._socket.send(remaining)
            except BlockingIOError:
                if self._timeout == 0:
                    raise
                self._wait(reading=False, deadline=deadline)
                continue
            if sent == 0:
                raise OSError("Connection closed during send")
            remaining = remaining[sent:]

    def gettimeout(self) -> float | None:
        return self._timeout

    def settimeout(self, timeout: float | None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("Timeout value out of range")
        self._timeout = timeout

    def setsockopt(self, *args: Any) -> None:
        self._socket.setsockopt(*args)

    def shutdown(self, how: int) -> None:
        self._socket.shutdown(how)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> _CancellableConnection:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _event_lines(conn: socket.socket | _CancellableConnection):
    """Keep partial frames ourselves; socket.makefile cannot resume a timeout."""
    pending = bytearray()
    while True:
        chunk = conn.recv(min(65536, MAX_EVENT_LINE_BYTES + 1 - len(pending)))
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        pending.extend(chunk)
        while (end := pending.find(b"\n")) >= 0:
            yield bytes(pending[:end + 1])
            del pending[:end + 1]
        if len(pending) > MAX_EVENT_LINE_BYTES:
            yield bytes(pending)
            return


def _emit_status(callback: StatusCallback | None, state: str, **details: Any) -> None:
    """Callbacks run on the receiver worker; GUI callers should enqueue them."""
    if callback is None:
        return
    try:
        callback({"state": state, **details})
    except Exception as exc:
        try:
            print(f"Receiver status callback failed: {exc}", flush=True)
        except Exception:
            pass


def _shutdown_connection(conn: socket.socket | _CancellableConnection | None) -> None:
    if conn is None:
        return
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _monitor_status() -> dict[str, Any]:
    try:
        monitors = list_monitors()
        return {"monitors": [
            {"number": monitor.number, "device": monitor.device,
             "x": monitor.bounds.left, "y": monitor.bounds.top,
             "width": monitor.bounds.right - monitor.bounds.left,
             "height": monitor.bounds.bottom - monitor.bounds.top,
             "primary": monitor.primary}
            for monitor in monitors.values()
        ]}
    except Exception as exc:
        # Monitor discovery is useful GUI context, not a reason to refuse TCP.
        return {"monitors": [], "monitor_error": str(exc)}


# Keep primary-screen metrics and cursor coordinates in the same pixel space.
enable_dpi_awareness()

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


def primary_screen_bounds() -> ScreenBounds:
    if sys.platform != "win32":
        raise OSError("Screen-edge return requires Windows")
    user32 = ctypes.windll.user32
    width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if width <= 1 or height <= 1:
        raise OSError("Cannot read Windows primary display dimensions")
    return ScreenBounds(0, 0, width, height)


class EdgeReturnController:
    """Per-connection layout and return state; all input runs on the reader thread."""

    def __init__(self, send: Callable[[dict[str, Any]], bool],
                 get_bounds: Callable[[], ScreenBounds] | None = None,
                 get_monitors: Callable[[], dict[int, WindowsMonitor]] | None = None) -> None:
        self.send = send
        self.get_bounds = get_bounds or primary_screen_bounds
        self.get_monitors = get_monitors or list_monitors
        self.routes: dict[str, ScreenBounds] = {}
        self.mac_edge: str | None = None
        self.bounds: ScreenBounds | None = None
        self.session_id = ""
        self.margin = 2

    def return_to_mac(self, edge: str, position: float) -> bool:
        global control_mode_enabled
        control_mode_enabled = False
        release_all_remote_inputs()
        self.mac_edge = None
        self.routes = {}
        return self.send({"type": "return_control", "session_id": self.session_id,
                          "edge": edge, "position": position})

    def handle(self, event: dict[str, Any]) -> bool:
        global control_mode_enabled

        if event["type"] == "control_mode":
            self.mac_edge = None
            self.bounds = None
            self.routes = {}
            if event["enabled"] and event.get("edge_monitors"):
                try:
                    monitors = self.get_monitors()
                    mappings = event["edge_monitors"]
                    missing = sorted(set(mappings.values()) - monitors.keys())
                    if missing:
                        raise OSError(f"Monitor(s) {missing} unavailable; active numbers: {sorted(monitors)}")
                    routes = {edge: monitors[number].bounds for edge, number in mappings.items()}
                    bounds = routes[event["edge"]]
                    mouse_controller.position = bounds.landing(
                        OPPOSITE_EDGE[event["edge"]], event.get("position", 0.5), event["edge_margin"]
                    )
                except OSError as exc:
                    control_mode_enabled = False
                    release_all_remote_inputs()
                    print(f"Layout rejected: {exc}", flush=True)
                    return self.send({"type": "control_rejected", "session_id": event["session_id"],
                                      "reason": str(exc)[:256] or "Monitor unavailable"})
                self.routes = routes
                self.session_id = event["session_id"]
                self.margin = event["edge_margin"]
                handle_event(event)
                return True
            handle_event(event)
            if event["enabled"] and "edge" in event:
                try:
                    bounds = self.get_bounds()
                    if "position" in event:
                        mouse_controller.position = bounds.landing(
                            OPPOSITE_EDGE[event["edge"]], event["position"], event["edge_margin"]
                        )
                except OSError as exc:
                    print(f"Edge return unavailable; use F8: {exc}", flush=True)
                else:
                    self.bounds = bounds
                    self.mac_edge = event["edge"]
                    self.session_id = event["session_id"]
                    self.margin = event["edge_margin"]
            return True

        if event["type"] == "mouse_move" and control_mode_enabled and self.routes:
            x, y = mouse_controller.position
            dx, dy = (int(round(float(event[name]))) for name in ("dx", "dy"))
            for edge in EDGE_ORDER:
                if edge not in self.routes:
                    continue
                position = self.routes[edge].exit_fraction(
                    OPPOSITE_EDGE[edge], x, y, dx, dy, self.margin
                )
                if position is not None:
                    # Intercept before Windows can move onto an adjacent monitor.
                    return self.return_to_mac(edge, position)
            handle_event(event)
            return True

        if (event["type"] != "mouse_move" or not control_mode_enabled
                or self.bounds is None or self.mac_edge is None):
            handle_event(event)
            return True

        before_x, before_y = mouse_controller.position
        handle_event(event)
        x, y = mouse_controller.position
        edge = OPPOSITE_EDGE[self.mac_edge]
        if (self.bounds.contains(before_x, before_y) and self.bounds.crossing(
                edge, x, y, event["dx"], event["dy"], self.margin)):
            # Stop consuming queued input BEFORE notifying Mac. A fresh enable
            # message is required to begin another control session.
            return self.return_to_mac(self.mac_edge, self.bounds.fraction(edge, x, y))
        return True


def handle_client(
    conn: socket.socket | _CancellableConnection,
    address: tuple[str, int],
    client_connected: threading.Event,
    sync_clipboard: bool,
    clipboard_only_control_mode: bool,
    clipboard_poll_interval: float,
    clipboard_max_bytes: int,
    pairing_code: str,
    heartbeat_timeout: float,
    *,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
) -> None:
    global control_mode_enabled

    control_mode_enabled = False
    print(f"Mac connected from {address[0]}:{address[1]}", flush=True)
    try:
        peer = authenticate_server(conn, pairing_code,
                                   capabilities=("monitor_mapping_v1", "clipboard_v2", "control_status_v1"))
    except (AuthenticationError, OSError, ValueError) as exc:
        if stop_event is None or not stop_event.is_set():
            print(f"Authentication failed for {address[0]}: {exc}", flush=True)
            _emit_status(on_status, "error", stage="authentication", fatal=False,
                         message=str(exc), peer_host=address[0], peer_port=address[1])
        conn.close()
        client_connected.clear()
        if stop_event is None:
            time.sleep(1)
        else:
            stop_event.wait(1)
        return
    if stop_event is not None and stop_event.is_set():
        conn.close()
        client_connected.clear()
        return
    print("Mac authenticated", flush=True)
    print("Now press F8 on Mac, or Control + Option + Command + 8.", flush=True)
    _emit_status(on_status, "connected", peer_host=address[0], peer_port=address[1],
                 control_enabled=False)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    conn.settimeout(heartbeat_timeout)
    send_lock = threading.Lock()
    supports_control_status = "control_status_v1" in peer.get("capabilities", [])

    def send_server_event(event: dict[str, Any]) -> bool:
        try:
            with send_lock:
                conn.sendall(encode_event(event))
            if event["type"] == "return_control":
                _emit_status(on_status, "local", control_enabled=False)
            elif event["type"] == "control_rejected":
                _emit_status(on_status, "error", stage="layout", fatal=False,
                             message=event["reason"], control_enabled=False)
            return True
        except OSError as exc:
            print(f"Send to Mac failed: {exc}", flush=True)
            return False

    edge_return = EdgeReturnController(send_server_event)
    clipboard_sync = ClipboardSync(
        send_server_event,
        lambda: control_mode_enabled,
        "windows",
        "Mac",
        sync_clipboard,
        clipboard_only_control_mode,
        clipboard_poll_interval,
        clipboard_max_bytes,
        supports_extended=lambda: "clipboard_v2" in peer.get("capabilities", []),
    )
    clipboard_sync.start()
    with conn:
        try:
            for line in _event_lines(conn):
                if stop_event is not None and stop_event.is_set():
                    break
                if len(line) > MAX_EVENT_LINE_BYTES:
                    print("Bad event: event is too large", flush=True)
                    break
                try:
                    event = decode_event(line)
                    if event.get("type") in {"clipboard_text", "clipboard_chunk", "clipboard_ack"}:
                        clipboard_sync.submit_remote_event(event)
                    elif event["type"] == "heartbeat":
                        # Reply on the input reader thread: a live TCP socket alone
                        # does not prove that this thread is still processing input.
                        if supports_control_status and "heartbeat_id" in event:
                            if not send_server_event({"type": "heartbeat_ack",
                                                      "heartbeat_id": event["heartbeat_id"]}):
                                break
                    else:
                        if not edge_return.handle(event):
                            break
                        if (event["type"] == "control_mode"
                                and event["enabled"] == control_mode_enabled):
                            # A rejected layout already sent control_rejected; never
                            # follow that rejection with a successful control reply.
                            if supports_control_status and "session_id" in event:
                                if not send_server_event({"type": "control_ack",
                                                          "session_id": event["session_id"],
                                                          "enabled": control_mode_enabled}):
                                    break
                            _emit_status(on_status, "controlling" if control_mode_enabled else "local",
                                         control_enabled=control_mode_enabled)
                except Exception as exc:
                    print(f"Bad event: {exc}", flush=True)
        except (OSError, TimeoutError) as exc:
            if stop_event is None or not stop_event.is_set():
                print(f"Mac connection ended: {exc}", flush=True)
                _emit_status(on_status, "error", stage="receive", fatal=False,
                             message=str(exc), peer_host=address[0], peer_port=address[1])
        finally:
            clipboard_sync.stop()
            release_all_remote_inputs()
            control_mode_enabled = False
            client_connected.clear()
            _emit_status(on_status, "disconnected", peer_host=address[0], peer_port=address[1],
                         control_enabled=False)
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
    *,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
) -> None:
    """Run the receiver in the caller's thread; set stop_event to shut it down.

    on_status receives dictionaries on worker threads, never on a GUI main loop.
    An embedding UI must queue updates and should create a new Event for each run.
    """
    global control_mode_enabled

    bound_port = port

    def status(state: str, **details: Any) -> None:
        _emit_status(on_status, state, host=host, port=bound_port, **details)

    def client_status(event: dict[str, Any]) -> None:
        status(event["state"], **{key: value for key, value in event.items() if key != "state"})

    if not _receiver_run_lock.acquire(blocking=False):
        message = "A Windows receiver is already running in this process. Stop it before starting another one."
        status("error", stage="startup", fatal=True, message=message)
        status("stopped", message=message)
        raise RuntimeError(message)

    client_connected = threading.Event()
    run_stop = threading.Event()
    active_connection: socket.socket | _CancellableConnection | None = None
    discovery_thread: threading.Thread | None = None
    failure_message = ""
    failure_stage = "startup"

    try:
        if stop_event is not None and stop_event.is_set():
            return
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            if sys.platform == "win32":
                # Windows REUSEADDR permits competing listeners on one port.
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind((host, port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in {10048, 10013}:
                    hint = (
                        f"Port {port} may already be in use or reserved. Close the existing CrossKey "
                        "Windows receiver before starting another one. "
                        f"On Windows, use netstat -ano to check the listening PID for port {port}."
                    )
                else:
                    hint = "Check that the requested IP belongs to this computer and the port is available."
                failure_message = f"ERROR: Cannot bind Windows receiver to {host}:{port}: {exc}\n{hint}"
                status("error", stage="bind", fatal=True, message=failure_message)
                raise SystemExit(failure_message) from exc
            server.listen(1)
            if port == 0:
                bound_port = server.getsockname()[1]
            if stop_event is not None:
                server.settimeout(0.2)
            discovery_thread = threading.Thread(
                target=broadcast_receiver,
                args=(bound_port, discovery_port, client_connected, run_stop),
                name="crosskey-receiver-discovery", daemon=True,
            )
            discovery_thread.start()
            print(f"CrossKey Windows v{__version__}", flush=True)
            print("Windows Receiver started", flush=True)
            print(f"Receiver PID: {os.getpid()}", flush=True)
            print(f"Receiver script: {Path(__file__).resolve()}", flush=True)
            print(f"Listening on {host}:{bound_port}", flush=True)
            print(f"Broadcasting discovery on UDP port {discovery_port}", flush=True)
            print(f"Pairing code: {pairing_code}", flush=True)
            status("listening", **(_monitor_status() if on_status is not None else {}))
            failure_stage = "receive"
            print("Waiting for Mac connection...", flush=True)
            while stop_event is None or not stop_event.is_set():
                try:
                    conn, address = server.accept()
                except socket.timeout:
                    continue
                active_connection = conn
                if stop_event is not None and stop_event.is_set():
                    _shutdown_connection(conn)
                    conn.close()
                    break
                if stop_event is not None:
                    conn = _CancellableConnection(conn, stop_event)
                    active_connection = conn
                client_connected.set()
                print("Discovery broadcast paused while Mac is connected", flush=True)
                try:
                    handle_client(
                        conn, address, client_connected,
                        sync_clipboard, clipboard_only_control_mode,
                        clipboard_poll_interval, clipboard_max_bytes,
                        pairing_code, heartbeat_timeout,
                        stop_event=stop_event, on_status=client_status,
                    )
                finally:
                    _shutdown_connection(conn)
                    conn.close()
                    active_connection = None
                    client_connected.clear()
                if stop_event is None or not stop_event.is_set():
                    print("Waiting for Mac connection...", flush=True)
    except Exception as exc:
        if stop_event is None or not stop_event.is_set():
            failure_message = str(exc)
            status("error", stage=failure_stage, fatal=True, message=failure_message)
            raise
    finally:
        run_stop.set()
        conn = active_connection
        _shutdown_connection(conn)
        if conn is not None:
            conn.close()
        release_all_remote_inputs()
        control_mode_enabled = False
        client_connected.clear()
        try:
            for worker in (discovery_thread,):
                if (worker is not None and worker is not threading.current_thread()
                        and worker.is_alive()):
                    worker.join(timeout=0.5)
        finally:
            _receiver_run_lock.release()
            status("stopped", message=failure_message, control_enabled=False)


def parse_args() -> argparse.Namespace:
    config = load_settings()
    parser = argparse.ArgumentParser(description="Receive Mac input events on Windows.")
    parser.add_argument("--list-monitors", action="store_true",
                        help="Print active Windows monitor numbers and coordinates, then exit.")
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
        help="Synchronize text, images, files and rich text with Mac.",
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
    if args.list_monitors:
        return args
    if not args.pairing_code:
        args.pairing_code = generate_pairing_code()
        config["pairing_code"] = args.pairing_code
        save_settings(config)
        print("Generated a new pairing code and saved it to config.json", flush=True)
    return args


def main() -> None:
    args = parse_args()
    if args.list_monitors:
        try:
            for line in describe_monitors(list_monitors()):
                print(line)
        except OSError as exc:
            raise SystemExit(str(exc)) from exc
        return
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


if __name__ == "__main__":
    main()
