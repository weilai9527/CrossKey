import unittest
from unittest.mock import patch

from check_connection import resolve_target


class ConnectionTargetTests(unittest.TestCase):
    @patch("check_connection.discover_receiver", return_value=("192.168.1.20", 6060))
    def test_discovery_takes_priority_over_saved_ip(self, discover: object) -> None:
        target, source = resolve_target(
            {
                "auto_discover": True,
                "discovery_port": 5051,
                "discovery_timeout": 1,
                "windows_host": "192.168.53.178",
                "port": 5050,
            }
        )

        self.assertEqual(target, ("192.168.1.20", 6060))
        self.assertEqual(source, "auto-discovered")

    @patch("check_connection.discover_receiver", return_value=None)
    def test_saved_ip_is_only_a_fallback(self, discover: object) -> None:
        target, source = resolve_target(
            {
                "auto_discover": True,
                "discovery_port": 5051,
                "discovery_timeout": 1,
                "windows_host": "192.168.53.178",
                "port": 5050,
            }
        )

        self.assertEqual(target, ("192.168.53.178", 5050))
        self.assertEqual(source, "saved fallback")


if __name__ == "__main__":
    unittest.main()
