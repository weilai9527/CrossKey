import threading
import unittest
from unittest.mock import Mock, patch

from clipboard_sync import ClipboardSync, REMOTE_EVENT_QUEUE_SIZE


class BlockingClipboard:
    def __init__(self):
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.applied = threading.Event()
        self.writes = []
        self.version = 0

    def revision(self):
        return self.version

    def read(self):
        self.read_started.set()
        if not self.release_read.wait(3):
            raise RuntimeError("test clipboard read timed out")
        return "text", b"local clipboard"

    def write(self, kind, data):
        self.writes.append((kind, data))
        self.version += 1
        self.applied.set()
        return self.version


class ClipboardNonblockingTests(unittest.TestCase):
    def make_sync(self, poll_interval=0.01):
        backend = BlockingClipboard()
        sent = []
        sync = ClipboardSync(lambda event: not sent.append(event), lambda: True,
                             "windows", "Mac", True, True, poll_interval, 262144,
                             backend=backend)

        def cleanup():
            sync.stop()
            backend.release_read.set()
            for worker in (sync._poll_thread, sync._receiver_thread):
                if worker is not None:
                    worker.join(1)
                    self.assertFalse(worker.is_alive())

        self.addCleanup(cleanup)
        return sync, backend, sent

    def start_blocked_poll(self):
        sync, backend, sent = self.make_sync()
        sync.start()
        self.assertTrue(backend.read_started.wait(1))
        return sync, backend, sent

    def test_blocked_native_read_does_not_block_input_dispatch(self):
        sync, backend, _ = self.start_blocked_poll()
        dispatched_key = threading.Event()
        accepted = []

        def socket_reader():
            accepted.append(sync.submit_remote_event(
                {"type": "clipboard_text", "text": "remote", "source": "mac"}))
            # The next socket event remains runnable while clipboard native I/O waits.
            dispatched_key.set()

        reader = threading.Thread(target=socket_reader, daemon=True)
        reader.start()
        self.assertTrue(dispatched_key.wait(0.5))
        reader.join(1)
        self.assertEqual(accepted, [True])
        self.assertEqual(backend.writes, [])
        backend.release_read.set()
        self.assertTrue(backend.applied.wait(1))
        self.assertEqual(backend.writes, [("text", b"remote")])

    def test_stop_returns_during_native_read_and_old_work_does_not_resume(self):
        sync, backend, sent = self.start_blocked_poll()
        sync.submit_remote_event({"type": "clipboard_text", "text": "stale", "source": "mac"})
        stopped = threading.Event()

        def stop():
            sync.stop()
            stopped.set()

        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        self.assertTrue(stopped.wait(0.5))
        self.assertFalse(sync.submit_remote_event({"type": "clipboard_text", "text": "later"}))
        backend.release_read.set()
        stopper.join(1)
        sync._poll_thread.join(1)
        sync._receiver_thread.join(1)
        self.assertEqual(backend.writes, [])
        self.assertEqual(sent, [])

    def test_queue_overflow_pauses_sync_and_reports_once(self):
        sync, backend, sent = self.make_sync()
        event = {"type": "clipboard_text", "text": "pending", "source": "mac"}
        for _ in range(REMOTE_EVENT_QUEUE_SIZE):
            self.assertTrue(sync.submit_remote_event(event))
        with patch("builtins.print") as report:
            self.assertFalse(sync.submit_remote_event(event))
            self.assertFalse(sync.submit_remote_event(event))
            sync.start()
        report.assert_called_once()
        self.assertTrue(sync.stop_event.is_set())
        self.assertTrue(sync._remote_events.empty())
        self.assertIsNone(sync._receiver_thread)
        sync.handle_remote_event(event)
        self.assertEqual(backend.writes, [])
        self.assertEqual(sent, [])

    def test_reconnect_does_not_wait_for_native_read(self):
        sync, backend, sent = self.start_blocked_poll()
        reset_finished = threading.Event()
        results = []

        def reset():
            results.append(sync.reset_connection())
            reset_finished.set()

        resetter = threading.Thread(target=reset, daemon=True)
        resetter.start()
        self.assertTrue(reset_finished.wait(0.5))
        self.assertEqual(results, [False])
        self.assertTrue(sync.stop_event.is_set())
        backend.release_read.set()
        resetter.join(1)
        sync._poll_thread.join(1)
        self.assertEqual(sent, [])

    def test_reset_discards_event_already_dequeued_by_worker(self):
        sync, backend, _ = self.make_sync(poll_interval=100)
        dequeued = threading.Event()
        get = sync._remote_events.get

        def observe_get(*args, **kwargs):
            item = get(*args, **kwargs)
            dequeued.set()
            return item

        sync._remote_events.get = Mock(side_effect=observe_get)
        with sync.lock:
            sync.start()
            sync.submit_remote_event({"type": "clipboard_text", "text": "old", "source": "mac"})
            self.assertTrue(dequeued.wait(1))
            self.assertTrue(sync.reset_connection())
        sync.submit_remote_event({"type": "clipboard_text", "text": "new", "source": "mac"})
        self.assertTrue(backend.applied.wait(1))
        self.assertEqual(backend.writes, [("text", b"new")])

    def test_received_events_keep_wire_order(self):
        sync, backend, _ = self.make_sync(poll_interval=100)
        done = threading.Event()
        original_write = backend.write

        def write(kind, data):
            result = original_write(kind, data)
            if len(backend.writes) == 3:
                done.set()
            return result

        backend.write = write
        sync.start()
        for value in ("first", "second", "third"):
            sync.submit_remote_event({"type": "clipboard_text", "text": value, "source": "mac"})
        self.assertTrue(done.wait(1))
        self.assertEqual(backend.writes, [("text", b"first"), ("text", b"second"), ("text", b"third")])


if __name__ == "__main__":
    unittest.main()
