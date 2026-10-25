import socket
import threading
import unittest
from unittest.mock import Mock, patch

from auth import authenticate_client
from mac_client import EventSender, MacControllerClient, parse_args
from protocol import decode_event, encode_event
from screen_edges import ScreenBounds
from settings import DEFAULTS
from windows_monitors import WindowsMonitor
import win_server


MONITORS = {
    1: WindowsMonitor(1, r"\\.\DISPLAY1", ScreenBounds(-1920, -1080, 0, 0)),
    2: WindowsMonitor(2, r"\\.\DISPLAY2", ScreenBounds(0, 0, 2560, 1440), True),
}
MAPPING = {"top": 1, "right": 2}


def make_mac(sender):
    mac = MacControllerClient(sender, 1, True, False, "off", 2, MAPPING)
    mac.set_screen_horizontal_bounds(0, 1440)
    mac.set_screen_vertical_bounds(0, 900)
    mac.warp_cursor = Mock()
    return mac


class MacMonitorMappingTests(unittest.TestCase):
    def test_each_entry_carries_selected_edge_and_complete_mapping(self) -> None:
        for edge, values in (("top", (720, 0, 1, -2)), ("right", (1439, 2, 450, 0))):
            with self.subTest(edge=edge):
                sender = Mock()
                mac = make_mac(sender)
                self.assertTrue(mac.try_activate_from_edge(*values))
                event = sender.send.call_args.args[0]
                self.assertEqual(event["edge"], edge)
                self.assertEqual(event["edge_monitors"], MAPPING)

    def test_corner_prefers_dominant_direction(self) -> None:
        for dx, dy, edge in ((4, -1, "right"), (1, -4, "top"), (2, -2, "top")):
            mac = make_mac(Mock())
            self.assertTrue(mac.try_activate_from_edge(1439, dx, 1, dy))
            self.assertEqual(mac.active_edge, edge)

    def test_return_from_other_mapped_screen_and_stale_rejection(self) -> None:
        mac = make_mac(Mock())
        mac.try_activate_from_edge(720, 0, 1, -2)
        mac.handle_remote_event({"type": "control_rejected", "session_id": "old", "reason": "missing"})
        self.assertTrue(mac.is_enabled())
        mac.handle_remote_event({"type": "return_control", "session_id": mac.session_id,
                                 "edge": "bottom", "position": 0.25})
        self.assertTrue(mac.is_enabled())
        mac.handle_remote_event({"type": "return_control", "session_id": mac.session_id,
                                 "edge": "right", "position": 0.25})
        self.assertFalse(mac.is_enabled())
        mac.warp_cursor.assert_called_once_with((1429, 225))

    def test_rejected_layout_restores_mac_without_warp(self) -> None:
        mac = make_mac(Mock())
        mac.toggle_control_mode()
        mac.handle_remote_event({"type": "control_rejected", "session_id": mac.session_id,
                                 "reason": "Monitor 2 unavailable"})
        self.assertFalse(mac.is_enabled())
        mac.warp_cursor.assert_not_called()

    def test_reconnect_preserves_route_and_does_not_capture_if_send_fails(self) -> None:
        sender = Mock()
        mac = make_mac(sender)
        mac.try_activate_from_edge(1439, 2, 450, 0)
        session = mac.session_id
        mac.recover_remote_state_after_reconnect()
        event = next(c.args[0] for c in reversed(sender.send.call_args_list)
                     if c.args[0]["type"] == "control_mode")
        self.assertEqual(event["edge_monitors"], MAPPING)
        self.assertEqual(event["edge"], "right")
        self.assertNotEqual(session, event["session_id"])
        sender.send.return_value = False
        mac.recover_remote_state_after_reconnect()
        self.assertFalse(mac.is_enabled())

    def test_single_edge_cli_override_disables_saved_mapping(self) -> None:
        config = dict(DEFAULTS, pairing_code="123456", edge_monitors=MAPPING)
        for argv, expected in ((["mac_client.py"], MAPPING),
                               (["mac_client.py", "--auto-switch-edge", "off"], {})):
            with patch("mac_client.load_config", return_value=config), patch("sys.argv", argv):
                self.assertEqual(parse_args().edge_monitors, expected)

    def test_sender_refuses_multi_mapping_on_old_receiver(self) -> None:
        with patch("mac_client.threading.Thread"):
            sender = EventSender(lambda: None)
        sender.sock = Mock()
        mac = make_mac(sender)
        self.assertFalse(mac.try_activate_from_edge(1439, 2, 450, 0))
        self.assertFalse(mac.is_enabled())
        self.assertTrue(sender.send_queue.empty())
        sender.capabilities = {"monitor_mapping_v1"}
        self.assertTrue(mac.try_activate_from_edge(1439, 2, 450, 0))
        sender.close()


class WindowsMonitorMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()
        self.mouse = Mock(position=(100, 100))
        self.keyboard = Mock()
        for name, value in (("mouse_controller", self.mouse), ("keyboard_controller", self.keyboard)):
            patcher = patch.object(win_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        self.receiver = win_server.EdgeReturnController(
            lambda event: self.events.append(event) is None, get_monitors=lambda: MONITORS
        )

    def tearDown(self) -> None:
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()

    def activate(self, edge="top", mappings=None):
        return self.receiver.handle({"type": "control_mode", "enabled": True, "session_id": "current",
                                     "edge": edge, "position": 0.5, "edge_margin": 2,
                                     "edge_monitors": MAPPING if mappings is None else mappings})

    def test_target_screen_landings_include_negative_origin(self) -> None:
        self.activate("top")
        self.assertEqual(self.mouse.position, (-960, -11))
        self.activate("right")
        self.assertEqual(self.mouse.position, (10, 720))

    def test_missing_monitor_rejects_without_falling_back_to_primary(self) -> None:
        self.assertTrue(self.activate("right", {"top": 1, "right": 9}))
        self.assertFalse(win_server.control_mode_enabled)
        self.assertEqual(self.mouse.position, (100, 100))
        self.assertEqual(self.events[0]["type"], "control_rejected")
        self.assertIn("9", self.events[0]["reason"])
        self.receiver.handle({"type": "key_down", "char": "x"})
        self.keyboard.press.assert_not_called()

    def test_refresh_inventory_on_each_activation(self) -> None:
        self.receiver.get_monitors = Mock(side_effect=[MONITORS, {1: MONITORS[1]}])
        self.activate()
        self.activate()
        self.assertFalse(win_server.control_mode_enabled)
        self.assertEqual(self.events[-1]["type"], "control_rejected")

    def test_return_follows_current_monitor_and_stops_queued_input(self) -> None:
        self.activate("top")
        self.receiver.handle({"type": "key_down", "key": "ctrl"})
        self.receiver.handle({"type": "mouse_click", "button": "left", "pressed": True})
        # The user has moved from display 1 onto display 2 inside Windows.
        self.mouse.position = (3, 360)
        self.receiver.handle({"type": "mouse_move", "dx": -20, "dy": 0})
        self.assertFalse(win_server.control_mode_enabled)
        self.assertEqual(self.events[-1]["edge"], "right")
        self.assertAlmostEqual(self.events[-1]["position"], 0.25, places=3)
        self.mouse.move.assert_not_called()
        self.keyboard.release.assert_called_once()
        self.mouse.release.assert_called_once()
        self.receiver.handle({"type": "mouse_move", "dx": 20, "dy": 0})
        self.mouse.move.assert_not_called()

    def test_shared_monitor_boundary_is_intercepted_before_cursor_crosses(self) -> None:
        monitors = dict(MONITORS)
        monitors[1] = WindowsMonitor(1, r"\\.\DISPLAY1", ScreenBounds(-1920, 0, 0, 1080))
        self.receiver.get_monitors = lambda: monitors
        self.activate("right")
        self.mouse.position = (10, 200)
        self.receiver.handle({"type": "mouse_move", "dx": -100, "dy": 30})
        self.assertEqual(self.events[-1]["edge"], "right")
        self.mouse.move.assert_not_called()

    def test_inward_parallel_and_unmapped_monitor_do_not_return(self) -> None:
        self.activate("right")
        self.mouse.position = (0, 300)
        for dx, dy in ((1, 0), (0, 2)):
            self.receiver.handle({"type": "mouse_move", "dx": dx, "dy": dy})
        self.mouse.position = (5000, 5000)
        self.receiver.handle({"type": "mouse_move", "dx": -10, "dy": 0})
        self.assertTrue(win_server.control_mode_enabled)
        self.assertEqual(self.events, [])
        self.assertEqual(self.mouse.move.call_count, 3)

    def test_authenticated_socket_supports_two_entry_exit_cycles(self) -> None:
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(2)
        connected = threading.Event()
        connected.set()
        errors = []

        def serve():
            try:
                win_server.handle_client(server_sock, ("local", 1), connected,
                                         False, True, 0.7, 262144, "123456", 2)
            except Exception as exc:
                errors.append(exc)

        sender = Mock()
        sender.send.side_effect = lambda event: client_sock.sendall(encode_event(event)) is None
        mac = make_mac(sender)
        with patch.object(win_server, "list_monitors", return_value=MONITORS):
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                auth = authenticate_client(client_sock, "123456")
                self.assertIn("monitor_mapping_v1", auth["capabilities"])
                with client_sock.makefile("rb") as reader:
                    for edge, entry, movement, landing in (
                        ("top", (720, 0, 1, -2), (0, 20), (-960, -11)),
                        ("right", (1439, 2, 450, 0), (-20, 0), (10, 720)),
                    ):
                        mac.edge_cooldown_until = 0
                        self.assertTrue(mac.try_activate_from_edge(*entry))
                        sender.send({"type": "mouse_move", "dx": movement[0], "dy": movement[1]})
                        event = decode_event(reader.readline())
                        self.assertEqual(event["edge"], edge)
                        self.assertEqual(self.mouse.position, landing)
                        mac.handle_remote_event(event)
                        self.assertFalse(mac.is_enabled())
                        self.assertFalse(win_server.control_mode_enabled)
                self.assertEqual(mac.warp_cursor.call_count, 2)
            finally:
                client_sock.close()
                worker.join(3)
                server_sock.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(connected.is_set())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
