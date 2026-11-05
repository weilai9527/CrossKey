import copy
import queue
import threading
import unittest

from launcher import Session, config_from_form
from settings import DEFAULTS


def drain(session):
    events = []
    while True:
        try:
            events.append(session.events.get_nowait())
        except queue.Empty:
            return events


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()

    def tearDown(self):
        self.session.stop()
        if self.session.thread is not None:
            self.session.thread.join(1)
            self.assertFalse(self.session.thread.is_alive())

    def test_stop_finishes_worker_and_next_start_uses_fresh_event(self):
        ready = threading.Event()
        stops = []

        def target(stop, publish):
            stops.append(stop)
            publish({"state": "connected", "host": "192.0.2.10", "port": 5050})
            ready.set()
            stop.wait(2)

        for _ in range(2):
            ready.clear()
            self.assertTrue(self.session.start(target))
            self.assertTrue(ready.wait(1))
            self.assertTrue(self.session.running)
            self.assertFalse(self.session.start(target))
            self.session.stop()
            self.session.thread.join(1)
            self.assertFalse(self.session.running)
            events = drain(self.session)
            self.assertEqual([event["state"] for event in events], ["connected", "stopped"])
            self.assertTrue(all(event["generation"] == self.session.generation for event in events))
        self.assertIsNot(stops[0], stops[1])
        self.assertTrue(all(stop.is_set() for stop in stops))

    def test_callback_from_old_run_cannot_overwrite_new_status(self):
        callbacks = []

        def first(stop, publish):
            callbacks.append(publish)

        self.session.start(first)
        self.session.thread.join(1)
        first_generation = self.session.generation
        self.session.start(lambda stop, publish: publish({"state": "connected"}))
        self.session.thread.join(1)
        callbacks[0]({"state": "error", "message": "late old connection"})
        events = drain(self.session)
        self.assertEqual([event["state"] for event in events], ["connected", "stopped"])
        self.assertTrue(all(event["generation"] > first_generation for event in events))

    def test_status_mailbox_is_bounded_and_keeps_latest_state(self):
        for number in range(1000):
            self.session.publish({"state": "connecting", "number": number}, 0)
        self.session.publish({"state": "error", "message": "latest"}, 0)
        self.assertEqual(self.session.events.qsize(), 64)
        self.assertEqual(drain(self.session)[-1]["message"], "latest")

    def test_exception_reports_error_and_terminal_state(self):
        def target(stop, publish):
            raise RuntimeError("startup failed")

        self.session.start(target)
        self.session.thread.join(1)
        events = drain(self.session)
        self.assertEqual([event["state"] for event in events], ["error", "stopped"])
        self.assertTrue(events[0]["fatal"])
        self.assertEqual(events[0]["message"], "startup failed")

    def test_stopped_message_does_not_allow_overlapping_core_threads(self):
        published = threading.Event()

        def target(stop, publish):
            publish({"state": "stopped"})
            published.set()
            stop.wait(2)

        self.session.start(target)
        self.assertTrue(published.wait(1))
        self.assertEqual(drain(self.session)[-1]["state"], "stopped")
        self.assertFalse(self.session.start(lambda *args: None))


class LauncherFormTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULTS)
        self.config.update(windows_host="192.0.2.10", pairing_code="123456",
                           edge_monitors={"right": 1}, mouse_scale=1.7,
                           heartbeat_timeout=30, custom_preference="keep")
        self.values = dict(host=" 192.0.2.20 ", port="5055", code="654321",
                           discover=False, clipboard=False, auto_start=True,
                           edges={"top": "2", "right": "关闭", "bottom": "", "left": ""})

    def test_mac_save_validates_and_preserves_unrelated_configuration(self):
        original = copy.deepcopy(self.config)
        updated = config_from_form(self.config, self.values, "mac")
        self.assertEqual(updated["windows_host"], "192.0.2.20")
        self.assertEqual(updated["port"], 5055)
        self.assertEqual(updated["edge_monitors"], {"top": 2})
        self.assertEqual(updated["auto_switch_edge"], "off")
        self.assertFalse(updated["sync_clipboard"])
        self.assertTrue(updated["launcher_auto_start"])
        self.assertEqual(updated["mouse_scale"], 1.7)
        self.assertEqual(updated["custom_preference"], "keep")
        self.assertEqual(self.config, original)

    def test_missing_manual_address_requires_discovery(self):
        self.values["host"] = ""
        with self.assertRaises(ValueError):
            config_from_form(self.config, self.values, "mac")
        self.values["discover"] = True
        self.assertEqual(config_from_form(self.config, self.values, "mac")["windows_host"], "")

    def test_invalid_port_code_or_monitor_is_rejected(self):
        for field, invalid in (("port", "0"), ("port", "65536"), ("code", "12345"),
                               ("code", "abcdef"), ("edges", {"top": "0"}),
                               ("edges", {"top": "33"}), ("edges", {"top": "two"})):
            with self.subTest(field=field, value=invalid):
                values = dict(self.values, **{field: invalid})
                with self.assertRaises(ValueError):
                    config_from_form(self.config, values, "mac")

    def test_windows_save_does_not_replace_pairing_or_mac_layout(self):
        values = {key: self.values[key] for key in ("port", "clipboard", "auto_start")}
        updated = config_from_form(self.config, values, "windows")
        self.assertEqual(updated["pairing_code"], self.config["pairing_code"])
        self.assertEqual(updated["edge_monitors"], self.config["edge_monitors"])
        self.assertEqual(updated["host"], self.config["host"])
        self.assertEqual(updated["port"], 5055)

    def test_editing_other_options_preserves_legacy_single_edge_layout(self):
        self.config.update(edge_monitors={}, auto_switch_edge="right")
        values = dict(self.values, edges={edge: "关闭" for edge in ("top", "right", "bottom", "left")},
                      edges_changed=False)
        updated = config_from_form(self.config, values, "mac")
        self.assertEqual(updated["auto_switch_edge"], "right")
        self.assertEqual(updated["edge_monitors"], {})
        self.assertFalse(updated["sync_clipboard"])

    def test_explicit_edge_edit_replaces_legacy_single_edge_layout(self):
        self.config.update(edge_monitors={}, auto_switch_edge="right")
        updated = config_from_form(self.config, dict(self.values, edges_changed=True), "mac")
        self.assertEqual(updated["auto_switch_edge"], "off")
        self.assertEqual(updated["edge_monitors"], {"top": 2})


if __name__ == "__main__":
    unittest.main()
