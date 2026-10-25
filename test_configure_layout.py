import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from configure_layout import (main, parse_monitor_mappings, update_layout, update_monitor_layout,
                              validate_edge_margin, validate_position)
from settings import load_settings, save_settings


class ConfigureLayoutTests(unittest.TestCase):
    def test_updates_layout_without_losing_other_settings(self) -> None:
        config = {"windows_host": "192.168.1.25", "pairing_code": "123456"}

        updated = update_layout(config, "left", 4)

        self.assertEqual(updated["auto_switch_edge"], "left")
        self.assertEqual(updated["edge_margin"], 4)
        self.assertEqual(updated["windows_host"], "192.168.1.25")
        self.assertEqual(updated["pairing_code"], "123456")

    def test_position_validation(self) -> None:
        self.assertEqual(validate_position(" RIGHT "), "right")
        self.assertEqual(validate_position("top"), "top")
        with self.assertRaisesRegex(ValueError, "left, right, top, bottom, or off"):
            validate_position("diagonal")

    def test_edge_margin_validation(self) -> None:
        self.assertEqual(validate_edge_margin("3"), 3)
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            validate_edge_margin(0)

    def test_mapping_preserves_network_settings_and_can_be_disabled(self) -> None:
        original = {"windows_host": "192.168.0.104", "pairing_code": "123456"}
        updated = update_monitor_layout(original, parse_monitor_mappings(["top=1", "right=2"]), 3)
        self.assertEqual(updated["edge_monitors"], {"top": 1, "right": 2})
        self.assertEqual(updated["auto_switch_edge"], "off")
        self.assertEqual(updated["pairing_code"], original["pairing_code"])
        self.assertEqual(update_layout(updated, "off", 3)["edge_monitors"], {})
        self.assertEqual(update_layout(updated, "right", 3)["edge_monitors"], {})

    def test_malformed_mapping_is_rejected(self) -> None:
        for args in (["top=1", "top=2"], ["off=1"], ["right=0"], ["top=1.5"],
                     ["top"], ["right=two"], ["top=65536"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_monitor_mappings(args)

    def test_cli_saves_mapping_and_margin_only_edit_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings({"pairing_code": "123456"}, path)
            with patch("configure_layout.CONFIG_PATH", path):
                for argv in (["configure_layout.py", "--map", "top=1", "--map", "right=2"],
                             ["configure_layout.py", "--edge-margin", "4"]):
                    with patch("sys.argv", argv):
                        main()
            settings = load_settings(path)
            self.assertEqual(settings["edge_monitors"], {"top": 1, "right": 2})
            self.assertEqual(settings["edge_margin"], 4)

    def test_interactive_enter_keeps_existing_multi_monitor_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings({"edge_monitors": {"top": 1, "right": 2}}, path)
            with patch("configure_layout.CONFIG_PATH", path), \
                    patch("sys.argv", ["configure_layout.py"]), \
                    patch("builtins.input", side_effect=[""] * 6):
                main()
            self.assertEqual(load_settings(path)["edge_monitors"], {"top": 1, "right": 2})


if __name__ == "__main__":
    unittest.main()
