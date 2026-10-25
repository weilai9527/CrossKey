import base64
import hashlib
import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

from clipboard_formats import pack_rich
from clipboard_sync import ClipboardSync
from protocol import CLIPBOARD_CHUNK_BYTES, decode_event, encode_event


class FakeClipboard:
    def __init__(self, content=None):
        self.content = content
        self.version = 0
        self.writes = []
        self.reads = 0

    def revision(self):
        return self.version

    def read(self):
        self.reads += 1
        return self.content

    def write(self, kind, data):
        self.writes.append((kind, data))
        self.copy((kind, data))
        return self.version

    def copy(self, content):
        self.content = content
        self.version += 1


def chunks(kind, data, transfer_id="transfer", source="windows"):
    return [{"type": "clipboard_chunk", "kind": kind, "data": base64.b64encode(
        data[offset:offset + CLIPBOARD_CHUNK_BYTES]).decode(), "source": source,
        "size": len(data), "index": offset // CLIPBOARD_CHUNK_BYTES,
        "clipboard_id": transfer_id, "sha256": hashlib.sha256(data).hexdigest()}
        for offset in range(0, len(data), CLIPBOARD_CHUNK_BYTES)]


class ClipboardSyncTests(unittest.TestCase):
    def make_sync(self, sent=None, enabled=True, backend=None, source="mac"):
        sent = sent if sent is not None else []
        return ClipboardSync(lambda event: not sent.append(event), lambda: enabled,
                             source, "peer", True, True, 0.7, 8,
                             backend=backend or FakeClipboard())

    def test_remote_text_is_applied(self):
        sync = self.make_sync()
        sync.handle_remote_event({"type": "clipboard_text", "text": "hello", "source": "windows"})
        self.assertEqual(sync.backend.writes, [("text", b"hello")])

    def test_oversized_remote_text_is_ignored(self):
        sync = self.make_sync()
        sync.handle_remote_event({"type": "clipboard_text", "text": "123456789", "source": "windows"})
        self.assertEqual(sync.backend.writes, [])

    def test_clipboard_waits_for_control_mode(self):
        sync = self.make_sync(enabled=False)
        for event in chunks("image", b"png"):
            sync.handle_remote_event(event)
        sync.handle_remote_event({"type": "clipboard_text", "text": "hello"})
        sync._poll_once()
        self.assertEqual(sync.backend.writes, [])
        self.assertEqual(sync.backend.reads, 0)

    def test_all_extended_types_round_trip_both_directions_and_do_not_echo(self):
        output = io.BytesIO()
        Image.new("RGBA", (8, 8), (4, 50, 100, 120)).save(output, "PNG")
        payloads = [("image", output.getvalue()),
                    ("rich", pack_rich("你好", "<b>你好</b>".encode(), br"{\rtf1 hello}")),
                    ("files", os.urandom(3 * CLIPBOARD_CHUNK_BYTES + 10))]
        for source in ("mac", "windows"):
            for content in payloads:
                with self.subTest(source=source, kind=content[0]):
                    wire, replies = [], []
                    left = self.make_sync(wire, backend=FakeClipboard(content), source=source)
                    right = self.make_sync(replies, source="windows" if source == "mac" else "mac")
                    left._poll_once()
                    for event in wire:
                        right.handle_remote_event(decode_event(encode_event(event)))
                    self.assertEqual(right.backend.writes, [content])
                    for event in replies:
                        left.handle_remote_event(decode_event(encode_event(event)))
                    wire.clear()
                    replies.clear()
                    left._poll_once()
                    right._poll_once()
                    self.assertEqual(wire, [])
                    self.assertEqual(replies, [])

    def test_partial_out_of_order_or_corrupted_transfer_never_replaces_clipboard(self):
        data = b"x" * (CLIPBOARD_CHUNK_BYTES + 5)
        events = chunks("files", data)
        for broken in ([events[0]], [events[1]],
                       [events[0], dict(events[1], sha256="0" * 64)],
                       [dict(e, sha256="0" * 64) for e in events]):
            with self.subTest(broken=len(broken)):
                sync = self.make_sync()
                for event in broken:
                    sync.handle_remote_event(event)
                self.assertEqual(sync.backend.writes, [])

    def test_local_copy_during_transfer_wins(self):
        sent = []
        sync = self.make_sync(sent)
        events = chunks("files", b"x" * (CLIPBOARD_CHUNK_BYTES + 1))
        sync.handle_remote_event(events[0])
        sync.backend.copy(("text", b"new"))
        sync.handle_remote_event(events[1])
        self.assertEqual(sync.backend.writes, [])
        self.assertFalse(sent[-1]["accepted"])

    def test_busy_send_queue_retries_exact_chunk(self):
        sent = []
        sync = self.make_sync(sent, backend=FakeClipboard(("files", b"x" * (CLIPBOARD_CHUNK_BYTES + 1))))
        original = sync.send_event
        sync.send_event = lambda event: False
        sync._poll_once()
        self.assertEqual(sync._outgoing["offset"], 0)
        sync.send_event = original
        sync._poll_once()
        self.assertEqual([event["index"] for event in sent], [0, 1])

    def test_lost_ack_retry_does_not_write_twice(self):
        sync = self.make_sync()
        events = chunks("image", b"png")
        sync.handle_remote_event(events[0])
        sync.handle_remote_event(events[0])
        self.assertEqual(len(sync.backend.writes), 1)

    def test_new_clipboard_and_reconnect_restart_partial_transfer(self):
        sync = self.make_sync(backend=FakeClipboard(("files", b"x" * (8 * CLIPBOARD_CHUNK_BYTES))))
        sync._poll_once()
        first_id = sync._outgoing["id"]
        sync.reset_connection()
        sync._poll_once()
        self.assertNotEqual(sync._outgoing["id"], first_id)
        sync.backend.copy(("image", b"new"))
        sync._poll_once()
        self.assertEqual(sync._outgoing["data"], b"new")

    def test_text_image_text_and_recopy_remote_content(self):
        sent = []
        sync = self.make_sync(sent)
        sync.handle_remote_event({"type": "clipboard_text", "text": "old", "source": "windows"})
        sync.backend.copy(("text", b"new"))
        sync._poll_once()
        sync.backend.copy(("image", b"png"))
        sync._poll_once()
        sync.backend.copy(("text", b"old"))
        sync._poll_once()
        self.assertEqual([e["type"] for e in sent], ["clipboard_text", "clipboard_chunk", "clipboard_text"])

    def test_disabled_and_legacy_peer_do_not_send_extended_payload(self):
        sent = []
        sync = self.make_sync(sent, backend=FakeClipboard(("image", b"png")))
        sync.supports_extended = lambda: False
        sync._poll_once()
        self.assertEqual(sent, [])
        sync.enabled = False
        sync.supports_extended = lambda: True
        sync._poll_once()
        self.assertEqual(sent, [])

    def test_timeout_discards_partial_transfer(self):
        sync = self.make_sync()
        event = chunks("files", b"x" * (CLIPBOARD_CHUNK_BYTES + 1))[0]
        sync.handle_remote_event(event)
        with patch("clipboard_sync.time.monotonic", return_value=sync._incoming["updated"] + 61):
            sync._poll_once()
        self.assertIsNone(sync._incoming)

    def test_read_is_cached_until_native_clipboard_changes(self):
        sync = self.make_sync(backend=FakeClipboard(("files", b"x" * (8 * CLIPBOARD_CHUNK_BYTES))))
        sync._poll_once()
        sync._poll_once()
        self.assertEqual(sync.backend.reads, 1)

    def test_write_error_is_nacked_without_changing_local_cached_state(self):
        sent = []
        sync = self.make_sync(sent)
        with patch.object(sync.backend, "write", side_effect=OSError("clipboard is busy")):
            sync.handle_remote_event(chunks("image", b"png")[0])
        self.assertIsNone(sync._last_sent)
        self.assertFalse(sent[-1]["accepted"])

    def test_recopy_after_rejection_retries_the_same_bytes(self):
        sent = []
        sync = self.make_sync(sent, backend=FakeClipboard(("image", b"png")))
        sync._poll_once()
        first_id = sent[-1]["clipboard_id"]
        sync.handle_remote_event({"type": "clipboard_ack", "clipboard_id": first_id, "accepted": False})
        sync._poll_once()
        self.assertEqual(len(sent), 1)
        sync.backend.copy(("image", b"png"))
        sync._poll_once()
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[-1]["clipboard_id"], first_id)

    def test_recopy_during_partial_transfer_starts_a_fresh_transfer(self):
        sync = self.make_sync(backend=FakeClipboard(("files", b"x" * (8 * CLIPBOARD_CHUNK_BYTES))))
        sync._poll_once()
        transfer_id = sync._outgoing["id"]
        sync.backend.copy(sync.backend.content)
        sync._poll_once()
        self.assertNotEqual(sync._outgoing["id"], transfer_id)

    def test_legacy_peer_gets_plain_text_from_rich_content(self):
        sent = []
        content = ("rich", pack_rich("hello", b"<b>hello</b>", None))
        sync = self.make_sync(sent, backend=FakeClipboard(content))
        sync.supports_extended = lambda: False
        sync._poll_once()
        self.assertEqual(sent[0]["type"], "clipboard_text")
        self.assertEqual(sent[0]["text"], "hello")

    def test_pyperclip_fallback_does_not_resend_unchanged_text(self):
        sent = []
        sync = self.make_sync(sent)
        sync.backend = None
        with patch("clipboard_sync.pyperclip.paste", return_value="hello"):
            sync._poll_once()
            sync._poll_once()
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
