"""Opt-in desktop widget tests for macOS/Windows CI.

Run with CROSSKEY_GUI_TESTS=1 and an available desktop session. These tests create
only their own Tk windows. No real input listener, connection, clipboard action,
single-instance listener or user's config is used.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

import launcher
from launcher_support import BoundedLog, LauncherInstance
from screen_edges import ScreenBounds
import settings
from windows_monitors import WindowsMonitor


@unittest.skipUnless(os.environ.get("CROSSKEY_GUI_TESTS") == "1",
                     "Set CROSSKEY_GUI_TESTS=1 to create isolated test windows")
class LauncherWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.temp = tempfile.TemporaryDirectory(prefix="crosskey-ui-test-")
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.json"
        self.config = dict(settings.DEFAULTS, windows_host="192.168.0.100",
                           pairing_code="123456", auto_discover=False,
                           edge_monitors={"top": 2}, launcher_auto_start=False,
                           heartbeat_timeout=8.0, custom_preserved_value="keep-me")
        settings.save_settings(self.config, self.config_path)
        self.instance = Mock(spec=LauncherInstance)
        self.instance.poll_show.return_value = False
        self.log = BoundedLog()
        self.app = None
        self.root = tk.Tk()
        self.root.withdraw()
        fixtures = {
            1: WindowsMonitor(1, r"\\.\DISPLAY1", ScreenBounds(-1920, 0, 0, 1080)),
            2: WindowsMonitor(2, r"\\.\DISPLAY2", ScreenBounds(0, 0, 2560, 1440), True),
        }
        for mocked in (
            patch.object(launcher, "load_settings", side_effect=lambda: settings.load_settings(self.config_path)),
            patch.object(launcher, "save_settings", side_effect=lambda value: settings.save_settings(value, self.config_path)),
            patch.object(launcher, "local_ipv4_addresses", return_value=["172.20.10.8", "192.168.2.8"]),
            patch.object(launcher, "legacy_mac_processes", return_value=[]),
            patch("windows_monitors.list_monitors", return_value=fixtures),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.showerror"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.addCleanup(self.dispose_window)

    def dispose_window(self) -> None:
        if self.app is not None:
            self.app.session.stop()
            if self.app.session.thread is not None:
                self.app.session.thread.join(timeout=2)
            # Let any finite fake address lookup finish before removing patches.
            if self.app.ip_busy:
                try:
                    self.app.network_events.get(timeout=2)
                except queue.Empty:
                    pass
        try:
            for timer in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(timer)
            self.root.destroy()
        except self.tk.TclError:
            pass  # The explicit exit test has already destroyed its own root.

    def make_app(self, role: str = "mac") -> launcher.Launcher:
        self.app = launcher.Launcher(self.root, role, self.instance, self.log)
        self.drain_network()
        self.root.update_idletasks()
        return self.app

    def drain_network(self) -> None:
        if self.app.ip_busy:
            result = self.app.network_events.get(timeout=2)
            self.app.network_events.put_nowait(result)
        self.app.tick()

    @staticmethod
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from LauncherWindowTests.walk(child)

    def button(self, window, text: str):
        matches = [widget for widget in self.walk(window)
                   if isinstance(widget, self.ttk.Button) and str(widget.cget("text")) == text]
        self.assertEqual(len(matches), 1, f"Expected one button labeled {text}")
        return matches[0]

    def entry_after_label(self, window, text: str):
        for parent in self.walk(window):
            children = parent.winfo_children()
            for index, child in enumerate(children):
                if isinstance(child, self.tk.Label) and str(child.cget("text")) == text:
                    for candidate in children[index + 1:]:
                        if isinstance(candidate, self.ttk.Entry):
                            return candidate
        self.fail(f"Missing entry for {text}")

    def test_windows_displays_assigned_addresses_and_actual_two_monitor_geometry(self):
        app = self.make_app("windows")
        address = str(app.address.cget("text"))
        self.assertIn("172.20.10.8", address)
        self.assertIn("192.168.2.8", address)
        self.assertIn("5050", address)
        self.assertNotIn("0.0.0.0", address)
        self.assertEqual(str(app.heading.cget("text")), "准备接收")
        rectangles = [app.canvas.coords(item) for item in app.canvas.find_all()
                      if app.canvas.type(item) == "rectangle"]
        self.assertEqual(len(rectangles), 2)
        left, right = sorted(rectangles)
        self.assertLess(left[2], right[0])
        self.assertGreater(right[2] - right[0], left[2] - left[0])
        for x1, y1, x2, y2 in rectangles:
            self.assertGreaterEqual(x1, 0)
            self.assertGreaterEqual(y1, 0)
            self.assertGreater(x2, x1)
            self.assertGreater(y2, y1)
            self.assertLessEqual(x2, 410)
            self.assertLessEqual(y2, 158)
        labels = [app.canvas.itemcget(item, "text") for item in app.canvas.find_all()
                  if app.canvas.type(item) == "text"]
        self.assertIn("1", labels)
        self.assertIn("2", labels)

    def test_connection_state_shows_actual_target_instead_of_saved_fallback(self):
        app = self.make_app()
        app.session.publish({"state": "connected", "host": "172.20.10.8", "port": 5052},
                            app.session.generation)
        app.tick()
        self.assertEqual(str(app.heading.cget("text")), "已连接，可跨屏")
        self.assertIn("172.20.10.8", str(app.address.cget("text")))
        self.assertIn("5052", str(app.address.cget("text")))
        self.assertNotIn("192.168.0.100", str(app.address.cget("text")))
        app.session.publish({"state": "controlling"}, app.session.generation)
        app.tick()
        self.assertEqual(str(app.heading.cget("text")), "正在控制 Windows")
        app.session.publish({"state": "local"}, app.session.generation)
        app.tick()
        self.assertEqual(str(app.heading.cget("text")), "已返回 Mac")

    def test_saving_real_settings_widgets_preserves_unedited_config_and_layout(self):
        app = self.make_app()
        app.open_settings()
        port = self.entry_after_label(app.settings_window, "连接端口")
        port.delete(0, "end")
        port.insert(0, "5053")
        self.button(app.settings_window, "保存设置").invoke()
        self.assertIsNone(app.settings_window)
        saved = settings.load_settings(self.config_path)
        self.assertEqual(saved["port"], 5053)
        self.assertEqual(saved["edge_monitors"], {"top": 2})
        self.assertEqual(saved["heartbeat_timeout"], 8.0)
        self.assertEqual(saved["custom_preserved_value"], "keep-me")
        self.assertIn("5053", str(app.address.cget("text")))

    def test_unchanged_layout_widgets_preserve_legacy_single_edge_setting(self):
        config = settings.load_settings(self.config_path)
        config.update(edge_monitors={}, auto_switch_edge="left")
        settings.save_settings(config, self.config_path)
        app = self.make_app()
        app.open_settings()
        self.button(app.settings_window, "保存设置").invoke()
        saved = settings.load_settings(self.config_path)
        self.assertEqual(saved["auto_switch_edge"], "left")
        self.assertEqual(saved["edge_monitors"], {})

    def test_running_window_releases_views_and_duplicate_show_rebuilds_same_session(self):
        app = self.make_app()
        app.open_settings()
        app.open_details()
        old_body, old_settings, old_details = app.body, app.settings_window, app.details_window
        self.assertTrue(app.session.start(lambda stop, callback: stop.wait()))
        worker = app.session.thread
        app.phase = "connected"
        app.close_window()
        self.assertTrue(app.hidden)
        self.assertTrue(app.session.running)
        self.assertFalse(old_body.winfo_exists())
        self.assertFalse(old_settings.winfo_exists())
        self.assertFalse(old_details.winfo_exists())
        self.assertIsNone(app.settings_window)
        self.assertIsNone(app.details_text)
        self.instance.poll_show.return_value = True
        app.tick()
        self.instance.poll_show.return_value = False
        self.drain_network()
        self.assertFalse(app.hidden)
        self.assertTrue(app.body.winfo_exists())
        self.assertIsNot(app.body, old_body)
        self.assertIs(app.session.thread, worker)
        self.assertTrue(app.session.running)

    def test_log_view_is_bounded_redacts_pairing_and_updates_after_reopen(self):
        app = self.make_app()
        for number in range(250):
            self.log.write(f"sample line {number}\n")
        self.log.write("Pairing code: 123456\n")
        app.open_details()
        app.tick()
        content = app.details_text.get("1.0", "end-1c")
        self.assertNotIn("123456", content)
        self.assertIn("[hidden]", content)
        self.assertNotIn("sample line 0\n", content)
        self.assertIn("sample line 249", content)
        self.assertLessEqual(len(content.splitlines()), 200)
        callback = app.details_window.protocol("WM_DELETE_WINDOW")
        app.details_window.tk.call(callback)
        self.assertIsNone(app.details_text)
        self.log.write("after closing log\n")
        app.open_details()
        self.assertIn("after closing log", app.details_text.get("1.0", "end-1c"))

    def test_idle_close_exits_without_leaving_hidden_launcher(self):
        app = self.make_app()
        app.close_window()
        self.assertTrue(app.quitting)
        self.assertTrue(app.session.stop_event.is_set())
        self.assertFalse(app.hidden)
        app.tick()
        self.instance.close.assert_called_once()

    def test_explicit_exit_signals_fake_core_and_releases_instance_after_stop(self):
        app = self.make_app()
        self.assertTrue(app.session.start(lambda stop, callback: stop.wait()))
        self.button(app.body, "退出程序").invoke()
        self.assertTrue(app.session.stop_event.is_set())
        app.session.thread.join(timeout=2)
        self.assertFalse(app.session.running)
        app.tick()
        self.instance.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
