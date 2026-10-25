import unittest

from screen_edges import OPPOSITE_EDGE, ScreenBounds


class ScreenGeometryTests(unittest.TestCase):
    def test_segment_intersection_handles_negative_coordinates_and_large_moves(self) -> None:
        bounds = ScreenBounds(-1920, -1080, 0, 0)
        self.assertAlmostEqual(bounds.exit_fraction("bottom", -960, -11, 40, 100, 2),
                               (960 + 3.2) / 1919)
        self.assertIsNone(bounds.exit_fraction("bottom", -960, -11, 0, -10, 2))
        self.assertIsNone(bounds.exit_fraction("bottom", -960, -11, 0, 0, 2))
        # This segment exits through the right edge before reaching the bottom.
        self.assertIsNone(bounds.exit_fraction("bottom", -1, -100, 100, 100, 2))
        self.assertIsNone(bounds.exit_fraction("bottom", 100, 100, 0, 10, 2))

    def test_all_edges_require_outward_movement(self) -> None:
        bounds = ScreenBounds(0, 0, 1920, 1080)
        cases = (("left", 0, 500, -1, 0), ("right", 1919, 500, 1, 0),
                 ("top", 900, 0, 0, -1), ("bottom", 900, 1079, 0, 1))
        for edge, x, y, dx, dy in cases:
            with self.subTest(edge=edge):
                self.assertTrue(bounds.crossing(edge, x, y, dx, dy, 2))
                self.assertFalse(bounds.crossing(edge, x, y, -dx, -dy, 2))
                self.assertFalse(bounds.crossing(edge, x, y, 0, 0, 2))

    def test_different_resolutions_preserve_relative_edge_position(self) -> None:
        mac, windows = ScreenBounds(0, 0, 1440, 900), ScreenBounds(0, 0, 3840, 2160)
        for edge in OPPOSITE_EDGE:
            for fraction in (0, 0.25, 0.5, 0.75, 1):
                with self.subTest(edge=edge, fraction=fraction):
                    x, y = windows.landing(OPPOSITE_EDGE[edge], fraction, 2)
                    returned = windows.fraction(OPPOSITE_EDGE[edge], x, y)
                    mx, my = mac.landing(edge, returned, 2)
                    # Each display rounds to whole pixels; allow one Mac pixel.
                    self.assertAlmostEqual(mac.fraction(edge, mx, my), fraction, delta=1 / 899)
                    self.assertTrue(mac.contains(mx, my))

    def test_landing_is_outside_trigger_band_even_with_large_margin(self) -> None:
        bounds = ScreenBounds(-1440, -900, 0, 0)
        movements = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
        for edge, (dx, dy) in movements.items():
            with self.subTest(edge=edge):
                x, y = bounds.landing(edge, 0.5, 100)
                self.assertTrue(bounds.contains(x, y))
                self.assertFalse(bounds.crossing(edge, x, y, dx, dy, 100))

    def test_other_monitor_does_not_count_as_primary_edge(self) -> None:
        bounds = ScreenBounds(0, 0, 1920, 1080)
        self.assertFalse(bounds.crossing("left", 0, 1200, -1, 0, 2))
        self.assertFalse(bounds.crossing("top", -500, 0, 0, -1, 2))


if __name__ == "__main__":
    unittest.main()
