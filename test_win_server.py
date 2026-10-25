import unittest
import socket
import threading
from unittest.mock import Mock, patch

from pynput import keyboard, mouse

import win_server
from auth import authenticate_client
from mac_client import MacControllerClient
from protocol import decode_event, encode_event
from screen_edges import ScreenBounds


class RemoteInputStateTests(unittest.TestCase):
    def setUp(self) -> None:
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()
        self.keyboard_controller = Mock()
        self.mouse_controller = Mock()
        self.controller_patches = (
            patch.object(win_server, "keyboard_controller", self.keyboard_controller),
            patch.object(win_server, "mouse_controller", self.mouse_controller),
        )
        for controller_patch in self.controller_patches:
            controller_patch.start()

    def tearDown(self) -> None:
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()
        for controller_patch in reversed(self.controller_patches):
            controller_patch.stop()

    def test_input_is_ignored_while_control_mode_is_disabled(self) -> None:
        win_server.handle_event({"type": "key_down", "key": "ctrl"})
        win_server.handle_event(
            {"type": "mouse_click", "button": "left", "pressed": True}
        )

        self.keyboard_controller.press.assert_not_called()
        self.mouse_controller.press.assert_not_called()

    def test_disabling_control_mode_releases_held_inputs(self) -> None:
        win_server.handle_event({"type": "control_mode", "enabled": True})
        win_server.handle_event({"type": "key_down", "key": "ctrl"})
        win_server.handle_event(
            {"type": "mouse_click", "button": "left", "pressed": True}
        )

        self.assertIn(keyboard.Key.ctrl, win_server.pressed_remote_keys)
        self.assertIn(mouse.Button.left, win_server.pressed_remote_buttons)

        win_server.handle_event({"type": "control_mode", "enabled": False})

        self.keyboard_controller.release.assert_called_once_with(keyboard.Key.ctrl)
        self.mouse_controller.release.assert_called_once_with(mouse.Button.left)
        self.assertEqual(win_server.pressed_remote_keys, set())
        self.assertEqual(win_server.pressed_remote_buttons, set())

    def test_release_all_is_idempotent(self) -> None:
        win_server.pressed_remote_keys.add("a")
        win_server.pressed_remote_buttons.add(mouse.Button.right)

        win_server.release_all_remote_inputs()
        win_server.release_all_remote_inputs()

        self.keyboard_controller.release.assert_called_once_with("a")
        self.mouse_controller.release.assert_called_once_with(mouse.Button.right)


class FakeMouse:
    def __init__(self) -> None:
        self.position = (960, 540)
        self.moves = []
        self.press = Mock()
        self.release = Mock()

    def move(self, dx: int, dy: int) -> None:
        self.moves.append((dx, dy))
        x, y = self.position
        self.position = (min(1919, max(0, x + dx)), min(1079, max(0, y + dy)))


class EdgeReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()
        self.mouse = FakeMouse()
        self.keyboard = Mock()
        self.sent = []
        for name, value in (("mouse_controller", self.mouse),
                            ("keyboard_controller", self.keyboard)):
            patcher = patch.object(win_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.receiver = win_server.EdgeReturnController(
            lambda event: self.sent.append(event) is None,
            lambda: ScreenBounds(0, 0, 1920, 1080),
        )

    def tearDown(self) -> None:
        win_server.control_mode_enabled = False
        win_server.pressed_remote_keys.clear()
        win_server.pressed_remote_buttons.clear()

    def activate(self, edge="right", position=0.25) -> None:
        self.receiver.handle({
            "type": "control_mode", "enabled": True, "session_id": "current",
            "edge": edge, "edge_margin": 2, "position": position,
        })

    def test_all_four_layouts_place_cursor_and_return_from_matching_edge(self) -> None:
        cases = (
            ("right", (10, 270), (-20, 0)),
            ("left", (1909, 270), (20, 0)),
            ("bottom", (480, 10), (0, -20)),
            ("top", (480, 1069), (0, 20)),
        )
        for edge, landing, (dx, dy) in cases:
            with self.subTest(edge=edge):
                self.sent.clear()
                self.activate(edge)
                self.assertEqual(self.mouse.position, landing)
                self.assertTrue(win_server.control_mode_enabled)
                self.receiver.handle({"type": "mouse_move", "dx": dx, "dy": dy})
                self.assertFalse(win_server.control_mode_enabled)
                self.assertEqual(len(self.sent), 1)
                self.assertEqual(self.sent[0]["edge"], edge)
                self.assertEqual(self.sent[0]["session_id"], "current")
                self.assertAlmostEqual(self.sent[0]["position"], 0.25, places=3)

    def test_return_releases_drag_and_modifiers_and_ignores_queued_input(self) -> None:
        self.activate()
        self.receiver.handle({"type": "key_down", "key": "ctrl"})
        self.receiver.handle({"type": "mouse_click", "button": "left", "pressed": True})
        self.receiver.handle({"type": "mouse_move", "dx": -20, "dy": 0})
        self.keyboard.release.assert_called_once_with(keyboard.Key.ctrl)
        self.mouse.release.assert_called_once_with(mouse.Button.left)
        self.receiver.handle({"type": "mouse_move", "dx": 100, "dy": 0})
        self.receiver.handle({"type": "key_down", "char": "x"})
        self.assertEqual(self.mouse.moves, [(-20, 0)])
        self.keyboard.press.assert_called_once_with(keyboard.Key.ctrl)
        self.assertEqual(len(self.sent), 1)
        self.activate()
        self.assertTrue(win_server.control_mode_enabled)

    def test_inward_and_parallel_movements_do_not_return(self) -> None:
        self.activate()
        self.mouse.position = (0, 500)
        for dx, dy in ((0, 1), (1, 0), (100, 0)):
            self.receiver.handle({"type": "mouse_move", "dx": dx, "dy": dy})
        self.assertTrue(win_server.control_mode_enabled)
        self.assertEqual(self.sent, [])

    def test_legacy_and_disabled_layout_clear_previous_return_state(self) -> None:
        self.activate()
        self.receiver.handle({"type": "control_mode", "enabled": False})
        self.receiver.handle({"type": "control_mode", "enabled": True})
        self.receiver.handle({"type": "mouse_move", "dx": -2000, "dy": 0})
        self.assertTrue(win_server.control_mode_enabled)
        self.assertEqual(self.sent, [])

    def test_f8_activation_supports_return_without_moving_cursor(self) -> None:
        self.receiver.handle({
            "type": "control_mode", "enabled": True, "edge": "right",
            "session_id": "current", "edge_margin": 2,
        })
        self.assertEqual(self.mouse.position, (960, 540))
        self.receiver.handle({"type": "mouse_move", "dx": -1000, "dy": 0})
        self.assertFalse(win_server.control_mode_enabled)

    def test_send_failure_disables_input_and_requests_connection_close(self) -> None:
        self.receiver.send = Mock(return_value=False)
        self.activate()
        self.assertFalse(self.receiver.handle({"type": "mouse_move", "dx": -20, "dy": 0}))
        self.assertFalse(win_server.control_mode_enabled)

    def test_display_query_failure_preserves_f8_control(self) -> None:
        self.receiver.get_bounds = Mock(side_effect=OSError("display unavailable"))
        self.activate()
        self.assertTrue(win_server.control_mode_enabled)
        self.assertIsNone(self.receiver.mac_edge)
        self.receiver.handle({"type": "control_mode", "enabled": False})
        self.assertFalse(win_server.control_mode_enabled)

    def test_authenticated_socket_round_trip_restores_mac_and_closes_cleanly(self) -> None:
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(2)
        connected = threading.Event()
        connected.set()
        errors = []

        def serve() -> None:
            try:
                win_server.handle_client(server_sock, ("local", 1), connected,
                                         False, True, 0.7, 262144, "123456", 2)
            except Exception as exc:
                errors.append(exc)

        sender = Mock()
        sender.send.side_effect = lambda event: client_sock.sendall(encode_event(event)) is None
        mac = MacControllerClient(sender, 1, True, False, "right", 2)
        mac.set_screen_horizontal_bounds(0, 1440)
        mac.set_screen_vertical_bounds(0, 900)
        mac.warp_cursor = Mock()
        with patch.object(win_server, "primary_screen_bounds", return_value=ScreenBounds(0, 0, 1920, 1080)):
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                authenticate_client(client_sock, "123456")
                self.assertTrue(mac.try_activate_from_edge(1439, 1, 450, 0))
                sender.send({"type": "key_down", "key": "ctrl"})
                sender.send({"type": "mouse_move", "dx": -20, "dy": 0})
                with client_sock.makefile("rb") as reader:
                    event = decode_event(reader.readline())
                self.assertEqual(event["type"], "return_control")
                mac.handle_remote_event(event)
                self.assertFalse(mac.is_enabled())
                self.assertFalse(win_server.control_mode_enabled)
                mac.warp_cursor.assert_called_once_with((1429, 450))
                self.keyboard.release.assert_called_once_with(keyboard.Key.ctrl)
            finally:
                client_sock.close()
                worker.join(3)
                server_sock.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(connected.is_set())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
