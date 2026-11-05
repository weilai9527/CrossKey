import errno
import io
import socket
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auth import AuthenticationError, authenticate_client
from protocol import decode_event, encode_event
from screen_edges import ScreenBounds
from windows_monitors import WindowsMonitor
import win_server


class ServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(redirect_stdout(io.StringIO()))
        self.keyboard = Mock()
        self.mouse = Mock()
        self.patches.enter_context(patch.object(win_server, "keyboard_controller", self.keyboard))
        self.patches.enter_context(patch.object(win_server, "mouse_controller", self.mouse))
        self.patches.enter_context(patch.object(win_server, "list_monitors", return_value={
            1: WindowsMonitor(1, r"\\.\DISPLAY1", ScreenBounds(-1920, 0, 0, 1080)),
            2: WindowsMonitor(2, r"\\.\DISPLAY2", ScreenBounds(0, 0, 2560, 1440), True),
        }))
        self.broadcast_stops = []

        def broadcast(_port, _discovery_port, _connected, stop_event):
            self.broadcast_stops.append(stop_event)
            stop_event.wait(5)

        self.patches.enter_context(patch.object(win_server, "broadcast_receiver", side_effect=broadcast))
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()

    def start_receiver(self, port=0, callback=None):
        run = SimpleNamespace(stop=threading.Event(), condition=threading.Condition(),
                              events=[], errors=[], port=port)

        def status(event):
            with run.condition:
                run.events.append(event)
                run.condition.notify_all()
            if callback is not None:
                callback(event)

        def serve():
            try:
                win_server.serve("127.0.0.1", port, False, True, 0.7, 262144,
                                 5051, "123456", 30, stop_event=run.stop, on_status=status)
            except BaseException as exc:
                run.errors.append(exc)

        run.worker = threading.Thread(target=serve, daemon=True)
        run.worker.start()
        self.addCleanup(self.stop_receiver, run)
        event = self.wait_state(run, "listening")
        run.port = event["port"]
        return run

    def wait_state(self, run, state, count=1):
        with run.condition:
            self.assertTrue(run.condition.wait_for(
                lambda: sum(event["state"] == state for event in run.events) >= count,
                timeout=2,
            ), f"No {state} status; events={run.events}, errors={run.errors}")
            return [event for event in run.events if event["state"] == state][count - 1]

    def stop_receiver(self, run):
        run.stop.set()
        run.worker.join(2)
        self.assertFalse(run.worker.is_alive(), "Receiver did not stop promptly")
        self.assertEqual(run.errors, [])

    def connect(self, run):
        client = socket.create_connection(("127.0.0.1", run.port), timeout=2)
        self.addCleanup(client.close)
        authenticate_client(client, "123456", capabilities=("control_status_v1",))
        return client

    def test_stopping_idle_listener_releases_port_and_reports_monitor_metadata(self):
        run = self.start_receiver()
        listening = self.wait_state(run, "listening")
        self.assertGreater(run.port, 0)
        self.assertEqual(listening["host"], "127.0.0.1")
        self.assertEqual(listening["monitors"][0]["x"], -1920)
        self.assertEqual(listening["monitors"][1]["width"], 2560)
        self.assertTrue(listening["monitors"][1]["primary"])
        self.stop_receiver(run)
        self.assertEqual(run.events[-1]["state"], "stopped")
        self.assertTrue(all(signal.is_set() for signal in self.broadcast_stops))
        # A fresh lifecycle can bind the same port without a lingering listener.
        replacement = self.start_receiver(port=run.port)
        self.stop_receiver(replacement)

    def test_stop_interrupts_incomplete_authentication_without_waiting_ten_seconds(self):
        run = self.start_receiver()
        client = socket.create_connection(("127.0.0.1", run.port), timeout=2)
        with client, client.makefile("rb") as reader:
            self.assertEqual(decode_event(reader.readline())["type"], "auth_challenge")
            self.stop_receiver(run)
            self.assertEqual(reader.readline(), b"")
        self.assertNotIn("connected", [event["state"] for event in run.events])
        self.assertNotIn("error", [event["state"] for event in run.events])
        self.assertEqual(run.events[-1]["state"], "stopped")

    def test_stop_interrupts_connected_reader_and_releases_held_input(self):
        run = self.start_receiver()
        client = self.connect(run)
        with client.makefile("rb") as reader:
            client.sendall(encode_event({"type": "control_mode", "enabled": True,
                                         "session_id": "active"}))
            self.assertEqual(decode_event(reader.readline())["type"], "control_ack")
            client.sendall(encode_event({"type": "key_down", "char": "a"}))
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "processed"}))
            self.assertEqual(decode_event(reader.readline())["type"], "heartbeat_ack")
            self.wait_state(run, "controlling")
            self.keyboard.press.assert_called_once_with("a")
            self.stop_receiver(run)
            self.assertEqual(reader.readline(), b"")
        self.keyboard.release.assert_called_once_with("a")
        self.assertFalse(win_server.control_mode_enabled)
        self.assertFalse(win_server.pressed_remote_keys)
        self.assertEqual([event["state"] for event in run.events][-2:], ["disconnected", "stopped"])

    def test_disconnection_allows_a_second_authenticated_client(self):
        run = self.start_receiver()
        first = self.connect(run)
        self.wait_state(run, "connected")
        first.shutdown(socket.SHUT_RDWR)
        first.close()
        self.wait_state(run, "disconnected")
        second = self.connect(run)
        self.wait_state(run, "connected", count=2)
        with second.makefile("rb") as reader:
            second.sendall(encode_event({"type": "control_mode", "enabled": False,
                                         "session_id": "second"}))
            self.assertFalse(decode_event(reader.readline())["enabled"])
            self.wait_state(run, "local")
            second.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "still-alive"}))
            self.assertEqual(decode_event(reader.readline())["heartbeat_id"], "still-alive")
        self.stop_receiver(run)

    def test_status_callback_exception_does_not_stop_network_or_cleanup(self):
        def broken_callback(_event):
            raise RuntimeError("UI callback failed")

        run = self.start_receiver(callback=broken_callback)
        client = self.connect(run)
        with client.makefile("rb") as reader:
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "ui-error"}))
            self.assertEqual(decode_event(reader.readline())["heartbeat_id"], "ui-error")
        self.stop_receiver(run)
        self.assertEqual(run.events[-1]["state"], "stopped")

    def test_authentication_failure_reports_error_and_keeps_listener_usable(self):
        run = self.start_receiver()
        with socket.create_connection(("127.0.0.1", run.port), timeout=2) as wrong_client:
            with self.assertRaises(AuthenticationError):
                authenticate_client(wrong_client, "654321")
        error = self.wait_state(run, "error")
        self.assertEqual(error["stage"], "authentication")
        self.assertFalse(error["fatal"])
        self.assertNotIn("connected", [event["state"] for event in run.events])
        client = self.connect(run)
        self.wait_state(run, "connected")
        client.close()
        self.stop_receiver(run)

    def test_missing_monitor_reports_layout_error_without_losing_connection(self):
        run = self.start_receiver()
        client = self.connect(run)
        with client.makefile("rb") as reader:
            client.sendall(encode_event({"type": "control_mode", "enabled": True,
                "session_id": "missing", "edge": "top", "edge_margin": 2,
                "edge_monitors": {"top": 9}}))
            self.assertEqual(decode_event(reader.readline())["type"], "control_rejected")
            error = self.wait_state(run, "error")
            self.assertEqual(error["stage"], "layout")
            self.assertFalse(error["fatal"])
            self.assertFalse(error["control_enabled"])
            self.assertIn("9", error["message"])
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "after-layout"}))
            self.assertEqual(decode_event(reader.readline())["heartbeat_id"], "after-layout")
        self.stop_receiver(run)

    def test_stop_interrupts_a_socket_writer_with_an_unread_peer(self):
        sending = threading.Event()
        returned = threading.Event()

        def blocked_client(conn, *_args, **_kwargs):
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            sending.set()
            try:
                conn.sendall(b"x" * (8 * 1024 * 1024))
            finally:
                returned.set()

        with patch.object(win_server, "handle_client", side_effect=blocked_client):
            run = self.start_receiver()
            with socket.create_connection(("127.0.0.1", run.port), timeout=2):
                self.assertTrue(sending.wait(2))
                self.assertFalse(returned.wait(0.05))
                self.stop_receiver(run)
                self.assertTrue(returned.is_set())
                self.assertEqual(run.events[-1]["state"], "stopped")

    def test_bind_failure_reports_error_then_stopped_and_preserves_exception(self):
        statuses = []
        error = OSError(errno.EADDRINUSE, "occupied")
        with patch.object(win_server.socket, "socket") as factory:
            factory.return_value.__enter__.return_value.bind.side_effect = error
            with self.assertRaises(SystemExit) as raised:
                win_server.serve("127.0.0.1", 5050, False, True, 0.7, 262144,
                                 5051, "123456", 30, stop_event=threading.Event(),
                                 on_status=statuses.append)
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual([event["state"] for event in statuses], ["error", "stopped"])
        self.assertEqual(statuses[0]["stage"], "bind")
        self.assertTrue(statuses[0]["fatal"])
        self.assertIn("occupied", statuses[0]["message"])
        self.assertFalse(win_server._receiver_run_lock.locked())

    def test_duplicate_embedded_receiver_does_not_stop_the_first_instance(self):
        first = self.start_receiver()
        statuses = []
        with self.assertRaisesRegex(RuntimeError, "already running"):
            win_server.serve("127.0.0.1", 0, False, True, 0.7, 262144, 5051,
                             "123456", 30, stop_event=threading.Event(), on_status=statuses.append)
        self.assertEqual(statuses[0]["stage"], "startup")
        self.assertTrue(first.worker.is_alive())
        client = self.connect(first)
        with client.makefile("rb") as reader:
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "original"}))
            self.assertEqual(decode_event(reader.readline())["heartbeat_id"], "original")
        self.stop_receiver(first)

    def test_pre_cancelled_run_never_opens_a_socket(self):
        stop = threading.Event()
        stop.set()
        statuses = []
        with patch.object(win_server.socket, "socket") as factory:
            win_server.serve("127.0.0.1", 5050, False, True, 0.7, 262144, 5051,
                             "123456", 30, stop_event=stop, on_status=statuses.append)
            factory.assert_not_called()
        self.assertEqual([event["state"] for event in statuses], ["stopped"])

    def test_background_thread_start_failure_does_not_leave_receiver_locked(self):
        statuses = []
        with patch.object(win_server.threading, "Thread") as thread_factory:
            thread_factory.return_value.start.side_effect = RuntimeError("cannot start thread")
            thread_factory.return_value.is_alive.return_value = False
            with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                win_server.serve("127.0.0.1", 0, False, True, 0.7, 262144, 5051,
                                 "123456", 30, stop_event=threading.Event(), on_status=statuses.append)
            thread_factory.return_value.join.assert_not_called()
        self.assertEqual([event["state"] for event in statuses], ["error", "stopped"])
        self.assertEqual(statuses[0]["stage"], "startup")
        self.assertFalse(win_server._receiver_run_lock.locked())


if __name__ == "__main__":
    unittest.main()
