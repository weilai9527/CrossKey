import select
import socket
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from auth import authenticate_client
from clipboard_sync import ClipboardSync
from protocol import decode_event, encode_event
import win_server


class ControlStatusServerTests(unittest.TestCase):
    def setUp(self):
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()
        self.mouse = Mock()
        self.keyboard = Mock()
        for name, controller in (("mouse_controller", self.mouse),
                                 ("keyboard_controller", self.keyboard)):
            patcher = patch.object(win_server, name, controller)
            patcher.start()
            self.addCleanup(patcher.stop)

    @contextmanager
    def connection(self, capabilities=("control_status_v1",), *, sync_clipboard=False):
        client, server = socket.socketpair()
        client.settimeout(2)
        connected = threading.Event()
        connected.set()
        errors = []

        def serve():
            try:
                win_server.handle_client(server, ("local", 1), connected,
                                         sync_clipboard, True, 0.7, 262144, "123456", 2)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        reader = client.makefile("rb")
        try:
            auth = authenticate_client(client, "123456", capabilities=capabilities)
            self.assertIn("control_status_v1", auth["capabilities"])
            yield client, reader
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            reader.close()
            client.close()
            worker.join(3)
            server.close()
            self.assertFalse(worker.is_alive())
            self.assertFalse(connected.is_set())
            self.assertEqual(errors, [])

    def test_confirmed_control_and_heartbeat_follow_input_processing(self):
        with self.connection() as (client, reader):
            client.sendall(encode_event({"type": "control_mode", "enabled": True,
                                         "session_id": "current"}))
            reply = decode_event(reader.readline())
            self.assertEqual(reply["type"], "control_ack")
            self.assertEqual(reply["session_id"], "current")
            self.assertTrue(reply["enabled"])
            self.assertTrue(win_server.control_mode_enabled)

            client.sendall(encode_event({"type": "key_down", "char": "a"}))
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "ping-1"}))
            reply = decode_event(reader.readline())
            self.assertEqual(reply["type"], "heartbeat_ack")
            self.assertEqual(reply["heartbeat_id"], "ping-1")
            self.keyboard.press.assert_called_once_with("a")

            client.sendall(encode_event({"type": "control_mode", "enabled": False,
                                         "session_id": "current"}))
            reply = decode_event(reader.readline())
            self.assertEqual(reply["type"], "control_ack")
            self.assertFalse(reply["enabled"])
            self.assertFalse(win_server.control_mode_enabled)
            self.keyboard.release.assert_called_once_with("a")

    def test_rejected_layout_does_not_also_receive_control_confirmation(self):
        with patch.object(win_server, "list_monitors", return_value={}):
            with self.connection() as (client, reader):
                client.sendall(encode_event({
                    "type": "control_mode", "enabled": True, "session_id": "missing-monitor",
                    "edge": "right", "edge_margin": 2, "edge_monitors": {"right": 1},
                }))
                reply = decode_event(reader.readline())
                self.assertEqual(reply["type"], "control_rejected")
                self.assertEqual(reply["session_id"], "missing-monitor")
                self.assertFalse(win_server.control_mode_enabled)
                client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "after-rejection"}))
                reply = decode_event(reader.readline())
                self.assertEqual(reply["type"], "heartbeat_ack")
                self.assertEqual(reply["heartbeat_id"], "after-rejection")

    def test_legacy_client_does_not_receive_new_status_events(self):
        processed = threading.Event()
        self.mouse.move.side_effect = lambda *_: processed.set()
        with self.connection(capabilities=()) as (client, _reader):
            # Even if an identifier is present, both endpoints must negotiate
            # support before introducing event types an older client cannot read.
            client.sendall(encode_event({"type": "control_mode", "enabled": True,
                                         "session_id": "legacy"}))
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "legacy-ping"}))
            client.sendall(encode_event({"type": "mouse_move", "dx": 1, "dy": 0}))
            self.assertTrue(processed.wait(2))
            self.assertEqual(select.select([client], [], [], 0.05)[0], [])

    def test_sessionless_control_has_no_confirmation(self):
        with self.connection() as (client, reader):
            client.sendall(encode_event({"type": "control_mode", "enabled": True}))
            client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "barrier"}))
            self.assertEqual(decode_event(reader.readline())["type"], "heartbeat_ack")
            self.assertTrue(win_server.control_mode_enabled)

    def test_blocked_clipboard_write_does_not_block_input_heartbeat_or_disconnect(self):
        entered = threading.Event()
        release = threading.Event()
        backend = Mock()
        backend.revision.return_value = 0
        backend.read.return_value = None
        syncs = []

        def write(*_args):
            entered.set()
            release.wait(5)
            return 1

        def make_sync(*args, **kwargs):
            sync = ClipboardSync(*args, backend=backend, **kwargs)
            syncs.append(sync)
            return sync

        backend.write.side_effect = write
        try:
            with patch.object(win_server, "ClipboardSync", side_effect=make_sync):
                with self.connection(sync_clipboard=True) as (client, reader):
                    client.sendall(encode_event({"type": "control_mode", "enabled": True,
                                                 "session_id": "clipboard-blocked"}))
                    self.assertEqual(decode_event(reader.readline())["type"], "control_ack")
                    client.sendall(encode_event({"type": "clipboard_text", "text": "hello",
                                                 "source": "mac"}))
                    self.assertTrue(entered.wait(2))
                    client.sendall(encode_event({"type": "key_down", "char": "a"}))
                    client.sendall(encode_event({"type": "heartbeat", "heartbeat_id": "while-blocked"}))
                    reply = decode_event(reader.readline())
                    self.assertEqual(reply["type"], "heartbeat_ack")
                    self.assertEqual(reply["heartbeat_id"], "while-blocked")
                    self.keyboard.press.assert_called_once_with("a")
                # The connection fixture also verifies the input reader exits;
                # this must succeed while native clipboard work is still blocked.
                self.keyboard.release.assert_called_once_with("a")
        finally:
            release.set()
            for sync in syncs:
                sync.stop()
                if sync._receiver_thread is not None:
                    sync._receiver_thread.join(2)


if __name__ == "__main__":
    unittest.main()
