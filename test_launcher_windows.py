import io
import queue
import socket
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

from auth import authenticate_client
from launcher import Launcher, Session
from launcher_support import BoundedLog
from protocol import decode_event, encode_event
from screen_edges import ScreenBounds
from windows_monitors import WindowsMonitor
import win_server


class WindowsLauncherIntegrationTests(unittest.TestCase):
    """Exercise the actual Session/server boundary without Tk or native input."""

    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(redirect_stdout(io.StringIO()))
        self.keyboard = Mock()
        self.patches.enter_context(patch.object(win_server, "keyboard_controller", self.keyboard))
        self.patches.enter_context(patch.object(win_server, "mouse_controller", Mock()))
        self.patches.enter_context(patch.object(win_server, "list_monitors", return_value={
            1: WindowsMonitor(1, r"\\.\DISPLAY1", ScreenBounds(-1920, 0, 0, 1080)),
            2: WindowsMonitor(2, r"\\.\DISPLAY2", ScreenBounds(0, 0, 2560, 1440), True),
        }))
        self.patches.enter_context(patch.object(
            win_server, "broadcast_receiver", side_effect=lambda *args: args[-1].wait(3)))
        self.session = Session()
        self.callbacks = []
        self.statuses = []
        self.addCleanup(self.stop_session)

    def start_session(self, port=0):
        def target(stop, publish):
            self.callbacks.append(publish)

            def status(event):
                self.statuses.append(event)
                publish(event)

            win_server.serve("127.0.0.1", port, False, True, 0.7, 262144, 5051,
                             "123456", 30, stop_event=stop, on_status=status)

        self.assertTrue(self.session.start(target))

    def stop_session(self):
        self.session.stop()
        if self.session.thread is not None:
            self.session.thread.join(2)
            self.assertFalse(self.session.running)

    def wait_state(self, state):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                event = self.session.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if event["state"] == state:
                return event
        self.fail(f"No {state} event: {self.statuses}")

    def headless_window(self):
        # Only the mailbox/state reducer runs; no Tk objects or windows exist.
        window = Launcher.__new__(Launcher)
        window.session = self.session
        window.role = "windows"
        window.root = Mock()
        window.instance = Mock()
        window.instance.poll_show.return_value = False
        window.network_events = queue.Queue(maxsize=1)
        window.hidden = window.quitting = window.was_running = window.ip_busy = False
        window.connected_target = None
        window.phase = "idle"
        window.message = window.last_error = ""
        window.monitors = []
        window.ips = []
        window.details_text = None
        window.log = BoundedLog()
        window.render = Mock()
        return window

    def test_server_monitor_payload_can_be_rendered_by_windows_view(self):
        self.start_session()
        listening = self.wait_state("listening")
        window = self.headless_window()
        self.session.publish(listening, self.session.generation)
        window.tick()
        self.assertEqual(window.phase, "listening")
        self.assertEqual(window.monitors[0]["x"], -1920)
        window.canvas = Mock()
        window.route = Mock()
        window.font = "test"
        window.colors = {"panel": "white", "accent": "blue", "line": "gray",
                         "fg": "black", "muted": "gray"}
        window.draw_layout()
        self.assertEqual(window.canvas.create_rectangle.call_count, 2)
        self.assertEqual(window.canvas.create_rectangle.call_args_list[0].args[0], 10)
        self.assertGreater(window.canvas.create_rectangle.call_args_list[1].args[0], 10)

    def test_real_connection_stop_and_restart_use_new_session_generation(self):
        self.start_session()
        listening = self.wait_state("listening")
        first_generation = self.session.generation
        first_stop = self.session.stop_event
        with socket.create_connection(("127.0.0.1", listening["port"]), timeout=2) as client:
            authenticate_client(client, "123456", capabilities=("control_status_v1",))
            with client.makefile("rb") as reader:
                client.sendall(encode_event({"type": "control_mode", "enabled": True,
                                             "session_id": "launcher"}))
                self.assertEqual(decode_event(reader.readline())["type"], "control_ack")
                client.sendall(encode_event({"type": "key_down", "char": "a"}))
                client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "processed"}))
                self.assertEqual(decode_event(reader.readline())["type"], "heartbeat_ack")
                self.wait_state("controlling")
                self.stop_session()
                self.assertEqual(reader.readline(), b"")
        self.keyboard.release.assert_called_once_with("a")
        window = self.headless_window()
        window.tick()
        self.assertEqual(window.phase, "stopped")
        self.assertEqual(window.last_error, "")
        self.start_session(port=listening["port"])
        self.callbacks[0]({"state": "error", "message": "old run must not change this window"})
        new_listening = self.wait_state("listening")
        self.assertEqual(new_listening["port"], listening["port"])
        self.assertGreater(new_listening["generation"], first_generation)
        self.assertIsNot(first_stop, self.session.stop_event)
        self.assertTrue(first_stop.is_set())
        window.tick()
        self.assertEqual(window.last_error, "")

    def test_port_conflict_keeps_actionable_error_after_worker_stops(self):
        window = self.headless_window()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as incumbent:
            incumbent.bind(("127.0.0.1", 0))
            incumbent.listen(1)
            self.start_session(incumbent.getsockname()[1])
            self.session.thread.join(2)
            self.assertFalse(self.session.running)
            window.tick()
        self.assertEqual(window.phase, "error")
        self.assertTrue(window.last_error)
        self.assertFalse(any(event["state"] == "listening" for event in self.statuses))
        self.assertTrue(any(event["state"] == "error" and event["fatal"]
                            for event in self.statuses))


if __name__ == "__main__":
    unittest.main()
