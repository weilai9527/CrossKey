import unittest
from unittest.mock import Mock, patch

from pynput import keyboard, mouse

import win_server


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


if __name__ == "__main__":
    unittest.main()
