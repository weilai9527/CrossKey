import unittest

from configure_layout import update_layout, validate_edge_margin, validate_position


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


if __name__ == "__main__":
    unittest.main()
