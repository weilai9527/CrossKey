import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mac_client import EventSender, MacControllerClient
from protocol import encode_event


class ControlConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.sender = Mock(capabilities={"control_status_v1"}, response_timeout=8)
        self.sender.send.return_value = True
        self.client = MacControllerClient(self.sender, 1, True, False, "right")
        self.client.set_screen_horizontal_bounds(0, 1440)
        self.client.set_screen_vertical_bounds(0, 900)
        timer = patch("mac_client.threading.Timer")
        timer.start()
        self.addCleanup(timer.stop)

    def ack(self, session_id=None):
        self.client.handle_remote_event({"type": "control_ack", "enabled": True,
                                         "session_id": session_id or self.client.session_id})

    def test_input_remains_local_until_matching_windows_confirmation(self):
        self.client.toggle_control_mode()
        self.assertFalse(self.client.is_enabled())
        self.client.on_native_mouse_click("left", True)
        self.assertEqual(self.sender.send.call_count, 1)
        self.ack("stale-session")
        self.assertFalse(self.client.is_enabled())
        self.ack()
        self.assertTrue(self.client.is_enabled())
        self.client.on_native_mouse_click("left", True)
        self.assertEqual(self.sender.send.call_args.args[0]["type"], "mouse_click")

    def test_repeated_edge_movement_does_not_cancel_pending_activation(self):
        self.assertTrue(self.client.try_activate_from_edge(1439, 2, 400, 0))
        session = self.client.session_id
        self.assertFalse(self.client.try_activate_from_edge(1439, 2, 400, 0))
        self.assertTrue(self.client.pending_control)
        self.assertEqual(self.client.session_id, session)
        self.assertEqual(self.sender.send.call_count, 1)

    def test_cancel_timeout_and_rejection_ignore_late_success(self):
        for action in ("cancel", "timeout", "reject", "disconnect"):
            with self.subTest(action=action):
                self.client.toggle_control_mode()
                session = self.client.session_id
                if action == "cancel":
                    self.client.toggle_control_mode()
                elif action == "timeout":
                    self.client._control_confirmation_timed_out(session)
                elif action == "reject":
                    self.client.handle_remote_event({"type": "control_rejected",
                        "session_id": session, "reason": "Monitor unavailable"})
                else:
                    self.client.handle_connection_lost()
                self.ack(session)
                self.assertFalse(self.client.is_enabled())
                self.assertFalse(self.client.pending_control)

    def test_connection_loss_restores_local_input_and_does_not_recapture_on_reconnect(self):
        self.client.toggle_control_mode()
        self.ack()
        self.client.pressed_keys.add("ctrl")
        self.client.handle_connection_lost()
        self.sender.send.reset_mock()
        self.client.recover_remote_state_after_reconnect()
        self.assertFalse(self.client.is_enabled())
        self.assertFalse(self.client.pressed_keys)
        self.sender.send.assert_not_called()

    def test_blocked_confirmation_log_does_not_prevent_restoring_local_input(self):
        self.client.toggle_control_mode()
        printing = threading.Event()
        release_print = threading.Event()
        restored = threading.Event()

        def blocked_print(*_args, **_kwargs):
            printing.set()
            release_print.wait(2)

        def disconnect():
            self.client.handle_connection_lost()
            restored.set()

        with patch("builtins.print", side_effect=blocked_print):
            reader = threading.Thread(target=self.ack, daemon=True)
            reader.start()
            watchdog = threading.Thread(target=disconnect, daemon=True)
            try:
                self.assertTrue(printing.wait(1))
                watchdog.start()
                self.assertTrue(restored.wait(0.5))
                self.assertFalse(self.client.is_enabled())
            finally:
                release_print.set()
                reader.join(2)
                if watchdog.ident is not None:
                    watchdog.join(2)


class ResponseWatchdogTests(unittest.TestCase):
    def make_sender(self):
        with patch("mac_client.threading.Thread"):
            sender = EventSender(lambda: None, response_timeout=8)
        sender.sock = Mock()
        sender.capabilities = {"control_status_v1"}
        sender.disconnect_callback = Mock()
        sender._start_reconnect = Mock()
        self.addCleanup(sender.close)
        return sender

    def test_connected_socket_without_receiver_responses_is_closed(self):
        sender = self.make_sender()
        sock = sender.sock
        sender.last_heartbeat_ack = 10
        with patch("mac_client.time.monotonic", return_value=19):
            sender._heartbeat_tick()
        self.assertIsNone(sender.sock)
        sock.shutdown.assert_called_once()
        sender.disconnect_callback.assert_called_once()
        sender._start_reconnect.assert_called_once()

    def test_legacy_receiver_is_not_required_to_reply_but_stalled_send_is_detected(self):
        sender = self.make_sender()
        sender.capabilities.clear()
        sender.last_heartbeat_ack = 0
        with patch("mac_client.time.monotonic", return_value=100):
            sender._heartbeat_tick()
        self.assertIsNotNone(sender.sock)
        sender.send_started_at = 90
        with patch("mac_client.time.monotonic", return_value=100):
            sender._heartbeat_tick()
        self.assertIsNone(sender.sock)
        sender.disconnect_callback.assert_called_once()

    def test_stale_reader_failure_cannot_close_new_connection(self):
        sender = self.make_sender()
        healthy = sender.sock
        sender._connection_failed(Mock(), "stale failure")
        self.assertIs(sender.sock, healthy)
        sender.disconnect_callback.assert_not_called()

    def test_heartbeat_reply_is_consumed_without_entering_application_handler(self):
        sender = self.make_sender()
        local, peer = socket.socketpair()
        sender.sock = local
        sender.last_heartbeat_ack = 0
        sender.pending_heartbeats = {"probe"}
        received = threading.Event()
        sender.incoming_handler = lambda event: received.set()
        worker = threading.Thread(target=sender._reader_loop, args=(local,), daemon=True)
        worker.start()
        try:
            peer.sendall(encode_event({"type": "heartbeat_ack", "heartbeat_id": "probe"})
                         + encode_event({"type": "control_ack", "session_id": "s", "enabled": True}))
            self.assertTrue(received.wait(2))
            self.assertGreater(sender.last_heartbeat_ack, 0)
            self.assertFalse(sender.pending_heartbeats)
        finally:
            sender.close()
            peer.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_watchdog_interrupts_an_actual_blocked_sendall(self):
        sender = self.make_sender()
        local, peer = socket.socketpair()
        local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        sender.sock = local
        sender.capabilities.clear()
        sender.send_queue.put(b"x" * (4 * 1024 * 1024))
        worker = threading.Thread(target=sender._writer_loop, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while sender.send_started_at is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIsNotNone(sender.send_started_at)
            with sender.lock:
                sender.send_started_at = time.monotonic() - 10
            sender._heartbeat_tick()
            sender.disconnect_callback.assert_called_once()
            self.assertIsNone(sender.sock)
        finally:
            sender.close()
            peer.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
