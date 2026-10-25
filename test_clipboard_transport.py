import queue
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from auth import authenticate_client
from clipboard_formats import pack_rich
from clipboard_sync import ClipboardSync
from mac_client import EventSender
from protocol import decode_event, encode_event
from test_clipboard_sync import FakeClipboard, chunks
import win_server


class ClipboardTransportTests(unittest.TestCase):
    def test_authenticated_server_routes_clipboard_in_both_directions(self):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(3)
        connected = threading.Event()
        connected.set()
        backend = FakeClipboard()
        errors = []
        created = []

        def make_sync(*args, **kwargs):
            sync = ClipboardSync(*args, backend=backend, **kwargs)
            created.append(sync)
            return sync

        def serve():
            try:
                win_server.handle_client(server_sock, ("local", 1), connected,
                                         True, False, 0.01, 262144, "123456", 3)
            except Exception as exc:
                errors.append(exc)

        with patch.object(win_server, "ClipboardSync", side_effect=make_sync):
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                result = authenticate_client(client_sock, "123456", capabilities=("clipboard_v2",))
                self.assertIn("clipboard_v2", result["capabilities"])
                rich = pack_rich("hello", b"<b>hello</b>", None)
                with client_sock.makefile("rb") as reader:
                    for event in chunks("rich", rich, source="mac"):
                        client_sock.sendall(encode_event(event))
                    ack = decode_event(reader.readline())
                    self.assertEqual(ack["type"], "clipboard_ack")
                    self.assertTrue(ack["accepted"])
                    self.assertEqual(backend.writes, [("rich", rich)])
                    # Simulate a new Windows Copy, then verify the reverse network route.
                    with created[0].lock:
                        backend.copy(("image", b"mock-native-png"))
                    event = decode_event(reader.readline())
                    self.assertEqual(event["type"], "clipboard_chunk")
                    self.assertEqual(event["source"], "windows")
                    client_sock.sendall(encode_event({"type": "clipboard_ack",
                        "clipboard_id": event["clipboard_id"], "accepted": True}))
            finally:
                client_sock.close()
                worker.join(4)
                server_sock.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_input_and_heartbeat_are_sent_before_queued_bulk_data(self):
        # Deterministic queue scheduling without threads or a native desktop.
        sender = EventSender.__new__(EventSender)
        sender.send_queue = queue.Queue()
        sender.clipboard_queue = queue.Queue(maxsize=8)
        sender.send_wakeup = threading.Event()
        sender.stop_event = threading.Event()
        sender.lock = threading.Lock()
        sender.sock = Mock()
        sender.capabilities = {"clipboard_v2"}
        sender.send(chunks("image", b"png", source="mac")[0])
        sender.send({"type": "key_down", "char": "v"})
        sender.send({"type": "heartbeat"})
        sent = []

        def record(data):
            sent.append(decode_event(data)["type"])
            if len(sent) == 3:
                sender.stop_event.set()

        sender.sock.sendall.side_effect = record
        sender._writer_loop()
        self.assertEqual(sent, ["key_down", "heartbeat", "clipboard_chunk"])


if __name__ == "__main__":
    unittest.main()
