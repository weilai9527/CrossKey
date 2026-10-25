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

    def test_heartbeat_timeout_must_exceed_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"heartbeat_interval":5,"heartbeat_timeout":5}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "heartbeat_timeout"):
                load_settings(path)

    def test_invalid_auto_switch_edge_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"auto_switch_edge":"diagonal"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "auto_switch_edge"):
                load_settings(path)

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings({"pairing_code": "123456"}, path)
            self.assertEqual(load_settings(path)["pairing_code"], "123456")

    def test_multi_monitor_settings_round_trip_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings({"edge_monitors": {"top": 1, "right": 2}}, path)
            self.assertEqual(load_settings(path)["edge_monitors"], {"top": 1, "right": 2})
            for mapping in ([], None, {"top": True}, {"right": "2"}, {"top": 0}, {"off": 1}):
                with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                    save_settings({"edge_monitors": mapping}, path)

    def test_discovery_announcement_validation(self) -> None:
        data = json.dumps({"app": "mac-control-windows", "tcp_port": 5050}).encode()
        self.assertEqual(parse_announcement(data, "192.168.1.8"), ("192.168.1.8", 5050))
        self.assertIsNone(parse_announcement(b'{"app":"other"}', "192.168.1.8"))


if __name__ == "__main__":
    unittest.main()
