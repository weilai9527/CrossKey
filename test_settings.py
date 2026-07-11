import json
import tempfile
import unittest
from pathlib import Path

from discovery import parse_announcement
from settings import load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_merged_with_saved_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"port":6060}', encoding="utf-8")

            settings = load_settings(path)

            self.assertEqual(settings["port"], 6060)
            self.assertTrue(settings["auto_discover"])

    def test_invalid_port_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"port":70000}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "port"):
                load_settings(path)

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings({"pairing_code": "123456"}, path)
            self.assertEqual(load_settings(path)["pairing_code"], "123456")

    def test_discovery_announcement_validation(self) -> None:
        data = json.dumps({"app": "mac-control-windows", "tcp_port": 5050}).encode()
        self.assertEqual(parse_announcement(data, "192.168.1.8"), ("192.168.1.8", 5050))
        self.assertIsNone(parse_announcement(b'{"app":"other"}', "192.168.1.8"))


if __name__ == "__main__":
    unittest.main()
