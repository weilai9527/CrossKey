import errno
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mac_client


def arguments():
    return SimpleNamespace(
        host="192.0.2.10", port=5050, host_from_cli=True, discover=False,
        discovery_port=5051, discovery_timeout=8, send_queue_size=32,
        pairing_code="123456", heartbeat_interval=2, heartbeat_timeout=30,
        mouse_scale=1, map_cmd_to_ctrl=True, debug_keys=False,
        auto_switch_edge="off", edge_margin=2, edge_monitors={},
        suppress_local_mouse=True, sync_clipboard=False,
        clipboard_only_control_mode=True, clipboard_poll_interval=0.7,
        clipboard_max_bytes=262144,
    )


class FakeSender:
    instances = []

    def __init__(self, resolver, **kwargs):
        self.lock = threading.Lock()
        self.sock = None
        self.host = ""
        self.port = 5050
        self.capabilities = {"control_status_v1", "monitor_mapping_v1"}
        self.close = Mock(side_effect=self._close)
        self.wait_closed = Mock()
        self.send = Mock(return_value=True)
        self.response_timeout = 30
        self.__class__.instances.append(self)

    def _close(self):
        self.sock = None

    def connect(self, target):
        self.host, self.port = target
        self.sock = object()

    def set_reconnect_callback(self, callback):
        self.reconnect = callback

    def set_disconnect_callback(self, callback):
        self.disconnect = callback

    def set_incoming_handler(self, callback):
        self.incoming = callback


class MacLifecycleTests(unittest.TestCase):
    def setUp(self):
        FakeSender.instances = []

    def test_gui_runs_and_stops_repeatedly_with_status_and_sleep_cleanup(self):
        all_events = []
        inhibitors = [Mock(), Mock()]
        created_clients = []

        def native(client, stop, ready):
            created_clients.append(client)
            ready()
            self.assertTrue(stop.wait(1))

        with patch.object(mac_client, "parse_args", side_effect=lambda argv: arguments()), \
             patch.object(mac_client.sys, "platform", "darwin"), \
             patch.object(mac_client, "EventSender", FakeSender), \
             patch.object(mac_client, "ClipboardSync") as clipboard, \
             patch.object(mac_client.subprocess, "Popen", side_effect=inhibitors), \
             patch.object(mac_client, "run_native_macos_event_tap", side_effect=native):
            for _ in range(2):
                stop = threading.Event()
                connected = threading.Event()
                events = []

                def status(event):
                    events.append(event)
                    if event["state"] == "connected":
                        connected.set()

                worker = threading.Thread(target=mac_client.main,
                                          args=([],), kwargs={"stop_event": stop, "on_status": status})
                worker.start()
                try:
                    self.assertTrue(connected.wait(1))
                finally:
                    stop.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(events[-1]["state"], "stopped")
                ready_event = next(e for e in events if e["state"] == "connected")
                self.assertEqual((ready_event["host"], ready_event["port"]), ("192.0.2.10", 5050))
                self.assertNotIn("pairing_code", ready_event)
                all_events.extend(events)
            self.assertEqual(clipboard.return_value.stop.call_count, 2)
        for inhibitor in inhibitors:
            inhibitor.terminate.assert_called_once()
            inhibitor.wait.assert_called_once_with(timeout=2)
        for sender in FakeSender.instances:
            self.assertIsNone(sender.sock)
            sender.wait_closed.assert_called_once()
        self.assertTrue(all(not client.is_enabled() for client in created_clients))
        self.assertFalse(any(t.name == "CrossKey-stop" for t in threading.enumerate()))

    def test_permission_failure_reports_error_without_claiming_input_is_ready(self):
        events = []
        inhibitor = Mock()
        with patch.object(mac_client, "parse_args", return_value=arguments()), \
             patch.object(mac_client.sys, "platform", "darwin"), \
             patch.object(mac_client, "EventSender", FakeSender), \
             patch.object(mac_client, "ClipboardSync"), \
             patch.object(mac_client.subprocess, "Popen", return_value=inhibitor), \
             patch.object(mac_client, "run_native_macos_event_tap",
                          side_effect=RuntimeError("Enable Accessibility and Input Monitoring")):
            mac_client.main([], on_status=events.append)
        self.assertNotIn("connected", [e["state"] for e in events])
        self.assertEqual([e["state"] for e in events][-2:], ["error", "stopped"])
        self.assertIn("Accessibility", events[-2]["message"])
        inhibitor.terminate.assert_called_once()
        self.assertIsNone(FakeSender.instances[0].sock)

    def test_cancelled_start_never_launches_a_helper_or_connection(self):
        stop = threading.Event()
        stop.set()
        events = []
        with patch.object(mac_client.subprocess, "Popen") as launch, \
             patch.object(mac_client, "run_client") as run:
            mac_client.main([], stop_event=stop, on_status=events.append)
        launch.assert_not_called()
        run.assert_not_called()
        self.assertEqual([e["state"] for e in events], ["stopped"])

    def test_status_callback_failure_does_not_break_control(self):
        sender = Mock(capabilities=set())
        sender.send.return_value = True
        callback = Mock(side_effect=RuntimeError("window destroyed"))
        client = mac_client.MacControllerClient(sender, 1, True, False, on_status=callback)
        self.assertTrue(client.toggle_control_mode())
        self.assertTrue(client.is_enabled())
        self.assertTrue(client.toggle_control_mode())
        self.assertFalse(client.is_enabled())

    def test_confirmed_control_and_return_emit_structured_states(self):
        events = []
        sender = Mock(capabilities={"control_status_v1"}, response_timeout=30)
        sender.send.return_value = True
        client = mac_client.MacControllerClient(sender, 1, True, False, on_status=events.append)
        try:
            client.toggle_control_mode()
            self.assertEqual(events[-1]["state"], "local")
            client.handle_remote_event({"type": "control_ack", "session_id": client.session_id,
                                        "enabled": True})
            self.assertEqual(events[-1]["state"], "controlling")
            client.toggle_control_mode()
            self.assertEqual(events[-1]["state"], "local")
        finally:
            client.handle_connection_lost()

    def test_discovery_stop_prevents_fallback_connection(self):
        args = arguments()
        args.discover, args.host_from_cli = True, False
        stop = threading.Event()

        def discover(port, timeout):
            self.assertLessEqual(timeout, 0.2)
            stop.set()
            return None

        with patch.object(mac_client, "discover_receiver", side_effect=discover):
            self.assertIsNone(mac_client.make_connection_target_resolver(args, stop)())


class EventSenderCancellationTests(unittest.TestCase):
    def make_sender(self):
        with patch.object(mac_client.threading, "Thread"):
            sender = mac_client.EventSender(lambda: ("192.0.2.10", 5050), pairing_code="123456")
        self.addCleanup(sender.close)
        return sender

    def test_stop_interrupts_pending_connect_and_cannot_install_socket(self):
        sender = self.make_sender()
        sock = Mock()
        sock.connect_ex.return_value = errno.EINPROGRESS
        started = threading.Event()
        errors = []

        def select(*args):
            started.set()
            sender.stop_event.wait(0.1)
            return [], [], []

        def connect():
            try:
                sender.connect(("192.0.2.10", 5050))
            except OSError as exc:
                errors.append(str(exc))

        with patch.object(mac_client.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.0.2.10", 5050))]), \
             patch.object(mac_client.socket, "socket", return_value=sock), \
             patch.object(mac_client.select, "select", side_effect=select):
            worker = threading.Thread(target=connect)
            worker.start()
            self.assertTrue(started.wait(1))
            sender.close()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(sender.sock)
        sock.shutdown.assert_called_once()

    def test_stop_during_authentication_does_not_resurrect_connection(self):
        sender = self.make_sender()
        sock = Mock()
        started, closed = threading.Event(), threading.Event()
        sock.shutdown.side_effect = lambda _: closed.set()
        errors = []

        def open_socket(target):
            sender._connecting_sock = sock
            return sock

        def authenticate(*args, **kwargs):
            started.set()
            self.assertTrue(closed.wait(1))
            return {"capabilities": ["control_status_v1"]}

        def connect():
            try:
                sender.connect(("192.0.2.10", 5050))
            except OSError as exc:
                errors.append(str(exc))

        with patch.object(sender, "_open_socket", side_effect=open_socket), \
             patch.object(sender, "_start_reader") as reader, \
             patch.object(mac_client, "authenticate_client", side_effect=authenticate):
            worker = threading.Thread(target=connect)
            worker.start()
            self.assertTrue(started.wait(1))
            sender.close()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(sender.sock)
        self.assertIsNone(sender._connecting_sock)
        self.assertEqual(sender.host, "")
        self.assertEqual(sender.capabilities, set())
        reader.assert_not_called()


class FakeInputListener(threading.Thread):
    instances = []
    auto_ready = False
    started_pair = threading.Event()

    def __init__(self, **callbacks):
        super().__init__(daemon=True)
        self.halt = threading.Event()
        self.stop_calls = 0
        self.ready_calls = 0
        FakeInputListener.instances.append(self)

    def run(self):
        self._run()

    def _run(self):
        if len(FakeInputListener.instances) == 2:
            FakeInputListener.started_pair.set()
        if self.auto_ready:
            self._mark_ready()
        self.halt.wait(2)

    def _mark_ready(self):
        self.ready_calls += 1

    def stop(self):
        self.stop_calls += 1
        self.halt.set()


class FallbackListenerLifecycleTests(unittest.TestCase):
    def setUp(self):
        FakeInputListener.instances = []
        FakeInputListener.started_pair.clear()

    def tearDown(self):
        for listener in FakeInputListener.instances:
            listener.stop()
            if listener.ident is not None:
                listener.join(1)
            self.assertFalse(listener.is_alive())

    def test_fallback_can_stop_before_readiness_without_reporting_connected(self):
        args = arguments()
        args.suppress_local_mouse = False
        stop = threading.Event()
        events = []
        with patch.object(mac_client, "parse_args", return_value=args), \
             patch.object(mac_client.sys, "platform", "darwin"), \
             patch.object(mac_client, "EventSender", FakeSender), \
             patch.object(mac_client, "ClipboardSync"), \
             patch.object(mac_client.subprocess, "Popen", return_value=Mock()), \
             patch.object(mac_client.mouse, "Listener", FakeInputListener), \
             patch.object(mac_client.keyboard, "Listener", FakeInputListener):
            worker = threading.Thread(target=mac_client.main, args=([],),
                                      kwargs={"stop_event": stop, "on_status": events.append})
            worker.start()
            try:
                self.assertTrue(FakeInputListener.started_pair.wait(1))
            finally:
                stop.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("connected", [event["state"] for event in events])
        self.assertEqual(events[-1]["state"], "stopped")
        self.assertEqual(len(FakeInputListener.instances), 2)
        self.assertTrue(all(not listener.is_alive() and listener.stop_calls
                            for listener in FakeInputListener.instances))

    def test_stop_before_thread_start_is_not_lost(self):
        listener = mac_client.make_stoppable_listener(FakeInputListener, threading.Event())
        listener.stop()
        listener.start()
        listener.join(0.5)
        self.assertFalse(listener.is_alive())
        self.assertEqual(listener.ready_calls, 0)

    def test_unready_live_listener_has_bounded_readiness_wait(self):
        stop = threading.Event()
        listener = mac_client.make_stoppable_listener(FakeInputListener, stop)
        listener.start()
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            mac_client.wait_for_input_listeners((listener,), stop, timeout=0.02)

    def test_listener_failure_before_readiness_is_reported(self):
        listener = mac_client.make_stoppable_listener(FakeInputListener, threading.Event())
        listener.stop()
        listener.start()
        listener.join(0.5)
        with self.assertRaisesRegex(RuntimeError, "Input listener stopped"):
            mac_client.wait_for_input_listeners((listener,), threading.Event())

    def test_partial_listener_creation_failure_cleans_up_first_listener(self):
        args = arguments()
        args.suppress_local_mouse = False
        events = []

        class BrokenKeyboard(FakeInputListener):
            def __init__(self, **kwargs):
                raise RuntimeError("keyboard initialization failed")

        with patch.object(mac_client, "parse_args", return_value=args), \
             patch.object(mac_client.sys, "platform", "darwin"), \
             patch.object(mac_client, "EventSender", FakeSender), \
             patch.object(mac_client, "ClipboardSync"), \
             patch.object(mac_client.subprocess, "Popen", return_value=Mock()), \
             patch.object(mac_client.mouse, "Listener", FakeInputListener), \
             patch.object(mac_client.keyboard, "Listener", BrokenKeyboard):
            mac_client.main([], on_status=events.append)
        self.assertEqual([event["state"] for event in events][-2:], ["error", "stopped"])
        self.assertEqual(len(FakeInputListener.instances), 1)
        self.assertGreater(FakeInputListener.instances[0].stop_calls, 0)
        self.assertFalse(FakeInputListener.instances[0].is_alive())


class QuartzLifecycleTests(unittest.TestCase):
    def quartz(self):
        names = [
            "kCGEventKeyDown", "kCGEventKeyUp", "kCGEventFlagsChanged", "kCGEventLeftMouseDown",
            "kCGEventLeftMouseUp", "kCGEventRightMouseDown", "kCGEventRightMouseUp",
            "kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGEventMouseMoved",
            "kCGEventLeftMouseDragged", "kCGEventRightMouseDragged", "kCGEventOtherMouseDragged",
            "kCGEventScrollWheel", "kCGEventFlagMaskShift", "kCGEventFlagMaskControl",
            "kCGEventFlagMaskAlternate", "kCGEventFlagMaskCommand", "kCGHIDEventTap",
            "kCGSessionEventTap", "kCGAnnotatedSessionEventTap", "kCGHeadInsertEventTap",
            "kCGEventTapOptionDefault", "kCFRunLoopCommonModes", "kCFRunLoopDefaultMode",
        ]
        quartz = Mock()
        for index, name in enumerate(names):
            setattr(quartz, name, 1 << index)
        quartz.CGEventMaskBit.side_effect = lambda value: value
        quartz.CGDisplayBounds.return_value = SimpleNamespace(
            origin=SimpleNamespace(x=0, y=0), size=SimpleNamespace(width=800, height=600))
        return quartz

    def test_worker_runloop_and_tap_resources_are_released_on_stop(self):
        quartz = self.quartz()
        stop = threading.Event()
        client = mac_client.MacControllerClient(Mock(), 1, True, False)
        ready_threads = []
        quartz.CFRunLoopRunInMode.side_effect = lambda *args: stop.set()
        with patch.dict(sys.modules, {"Quartz": quartz}):
            worker = threading.Thread(target=mac_client.run_native_macos_event_tap,
                                      args=(client, stop, lambda: ready_threads.append(threading.get_ident())))
            worker.start()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(ready_threads, [worker.ident])
        quartz.CFRunLoopStop.assert_called_once_with(quartz.CFRunLoopGetCurrent.return_value)
        quartz.CGEventTapEnable.assert_any_call(quartz.CGEventTapCreate.return_value, False)
        quartz.CFRunLoopRemoveSource.assert_called_once()
        quartz.CFRunLoopSourceInvalidate.assert_called_once()
        quartz.CFMachPortInvalidate.assert_called_once()
        self.assertIsNone(client.warp_cursor)

    def test_tap_creation_failure_never_reports_ready(self):
        quartz = self.quartz()
        quartz.CGEventTapCreate.return_value = None
        ready = Mock()
        client = mac_client.MacControllerClient(Mock(), 1, True, False)
        with patch.dict(sys.modules, {"Quartz": quartz}):
            with self.assertRaisesRegex(RuntimeError, "Accessibility"):
                mac_client.run_native_macos_event_tap(client, threading.Event(), ready)
        ready.assert_not_called()
        quartz.CFRunLoopRunInMode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
