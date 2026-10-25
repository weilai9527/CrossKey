import unittest

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
        self.assertEqual(sender.events, [{"type": "control_mode", "enabled": True}])

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


if __name__ == "__main__":
    unittest.main()
