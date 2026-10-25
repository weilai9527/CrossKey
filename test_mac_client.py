import unittest
from unittest.mock import Mock, patch

from mac_client import MacControllerClient


class RecordingSender:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def send(self, event: dict[str, object]) -> bool:
        self.events.append(event)
        return True


class EdgeSwitchTests(unittest.TestCase):
    def make_client(self, edge: str) -> tuple[MacControllerClient, RecordingSender]:
        sender = RecordingSender()
        client = MacControllerClient(sender, 1.0, True, False, edge, 2)  # type: ignore[arg-type]
        client.set_screen_horizontal_bounds(0, 1440)
        client.set_screen_vertical_bounds(0, 900)
        return client, sender

    def test_right_edge_activates_only_while_moving_outward(self) -> None:
        client, sender = self.make_client("right")

        self.assertFalse(client.try_activate_from_edge(1439, -1))
        self.assertTrue(client.try_activate_from_edge(1439, 1))

        self.assertTrue(client.is_enabled())
        self.assertEqual(sender.events, [{
            "type": "control_mode", "enabled": True, "session_id": client.session_id,
            "edge": "right", "edge_margin": 2, "position": 0.5,
        }])

    def test_left_edge_activation(self) -> None:
        client, _sender = self.make_client("left")

        self.assertTrue(client.try_activate_from_edge(1, -2))
        self.assertTrue(client.is_enabled())

    def test_edge_switch_can_be_disabled(self) -> None:
        client, sender = self.make_client("off")

        self.assertFalse(client.try_activate_from_edge(1439, 1))
        self.assertFalse(client.is_enabled())
        self.assertEqual(sender.events, [])

    def test_bottom_edge_activation(self) -> None:
        client, _sender = self.make_client("bottom")

        self.assertTrue(client.try_activate_from_edge(700, 0, 899, 2))
        self.assertTrue(client.is_enabled())

    def test_top_edge_activation(self) -> None:
        client, sender = self.make_client("top")
        self.assertTrue(client.try_activate_from_edge(720, 0, 1, -2))
        self.assertAlmostEqual(sender.events[0]["position"], 720 / 1439)

    def test_events_outside_primary_display_do_not_activate(self) -> None:
        client, _ = self.make_client("right")
        self.assertFalse(client.try_activate_from_edge(1450, 5, 300, 0))
        self.assertFalse(client.try_activate_from_edge(1439, 5, 950, 0))

    def test_failed_activation_does_not_capture_local_input(self) -> None:
        client, sender = self.make_client("right")
        sender.send = Mock(return_value=False)
        self.assertFalse(client.try_activate_from_edge(1439, 1))
        self.assertFalse(client.is_enabled())

    def test_return_warps_to_mac_and_prevents_immediate_reentry(self) -> None:
        client, sender = self.make_client("right")
        client.warp_cursor = Mock()
        with patch("mac_client.time.monotonic", return_value=10):
            client.try_activate_from_edge(1439, 1)
            client.pressed_keys.add("ctrl")
            client.handle_remote_event({
                "type": "return_control", "session_id": client.session_id,
                "edge": "right", "position": 0.25,
            })
            self.assertFalse(client.is_enabled())
            self.assertFalse(client.pressed_keys)
            client.warp_cursor.assert_called_once_with((1429, 225))
            self.assertFalse(client.try_activate_from_edge(1439, 1))
        with patch("mac_client.time.monotonic", return_value=10.3):
            self.assertTrue(client.try_activate_from_edge(1439, 1))
        self.assertEqual(len(sender.events), 2)

    def test_delayed_or_duplicate_return_cannot_toggle_a_new_session(self) -> None:
        client, _ = self.make_client("right")
        client.toggle_control_mode()
        old_id = client.session_id
        client.toggle_control_mode()
        client.toggle_control_mode()
        for session_id, edge in ((old_id, "right"), (client.session_id, "left")):
            client.handle_remote_event({
                "type": "return_control", "session_id": session_id,
                "edge": edge, "position": 0.5,
            })
            self.assertTrue(client.is_enabled())
        event = {"type": "return_control", "session_id": client.session_id,
                 "edge": "right", "position": 0.5}
        client.handle_remote_event(event)
        client.handle_remote_event(event)
        self.assertFalse(client.is_enabled())

    def test_reconnect_resends_layout_with_new_session(self) -> None:
        client, sender = self.make_client("bottom")
        client.toggle_control_mode()
        old_id = client.session_id
        client.recover_remote_state_after_reconnect()
        self.assertNotEqual(client.session_id, old_id)
        self.assertEqual(sender.events[1]["edge"], "bottom")
        self.assertNotIn("position", sender.events[1])

    def test_warp_failure_still_restores_local_input(self) -> None:
        client, _ = self.make_client("right")
        client.warp_cursor = Mock(side_effect=RuntimeError("warp failed"))
        client.toggle_control_mode()
        client.handle_remote_event({
            "type": "return_control", "session_id": client.session_id,
            "edge": "right", "position": 0.5,
        })
        self.assertFalse(client.is_enabled())


if __name__ == "__main__":
    unittest.main()
