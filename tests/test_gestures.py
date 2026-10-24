"""Handset tests for the window-to-phone mapping and the mouse's gestures."""

import unittest

from handset.gestures import (EDGE_ZONE, LONG_PRESS_MS, LONGEST_SWIPE_MS, SHORTEST_SWIPE_MS, Fit, Gesture, classify,
                              inside, safe_box, scroll, to_edge)

# A Pixel 7's screen (1080×2400) in a 400×600 window: 0.25 scale, 150 px bars at the sides.
PIXEL = Fit((400, 600), (1080, 2400))


class FitTest(unittest.TestCase):
    def test_scale_and_bars(self):
        self.assertEqual(PIXEL.scale, 0.25)
        self.assertEqual(PIXEL.origin, (65.0, 0.0))

    def test_points_map_both_ways(self):
        self.assertEqual(PIXEL.to_screen(65, 0), (0, 0))
        self.assertEqual(PIXEL.to_screen(200, 300), (540, 1200))
        self.assertEqual(PIXEL.to_widget(540, 1200), (200.0, 300.0))

    def test_off_the_screen(self):
        self.assertIsNone(PIXEL.to_screen(10, 300))  # the bar beside it
        self.assertIsNone(PIXEL.to_screen(336, 300))
        self.assertIsNone(PIXEL.to_screen(200, 601))

    def test_clamp_keeps_a_drag_on_the_screen(self):
        self.assertEqual(PIXEL.clamp(-50, 900), (0, 2399))
        self.assertEqual(PIXEL.clamp(1000, -5), (1079, 0))

    def test_no_screen_yet(self):
        fit = Fit((400, 600), (0, 0))
        self.assertEqual(fit.scale, 0.0)
        self.assertIsNone(fit.to_screen(200, 300))

    def test_landscape_bars_above_and_below(self):
        fit = Fit((800, 600), (2400, 1080))
        self.assertAlmostEqual(fit.scale, 1 / 3)
        self.assertEqual(fit.origin, (0.0, 120.0))
        self.assertEqual(fit.to_screen(400, 300), (1200, 540))


class EdgeTest(unittest.TestCase):
    """PIXEL's screen runs from x 65 to 335 and y 0 to 600 in the window."""

    def test_near_the_bottom_is_the_bottom_edge(self):
        self.assertEqual(to_edge(PIXEL, 200, 600 - EDGE_ZONE + 1), (540, 2399))  # the home bar
        self.assertEqual(to_edge(PIXEL, 200, 600 - EDGE_ZONE - 5)[1], int((600 - EDGE_ZONE - 5) * 4))

    def test_near_the_sides_and_top(self):
        self.assertEqual(to_edge(PIXEL, 65 + EDGE_ZONE - 1, 300), (0, 1200))  # back, from the left
        self.assertEqual(to_edge(PIXEL, 335 - 2, 300), (1079, 1200))  # back, from the right
        self.assertEqual(to_edge(PIXEL, 200, 3), (540, 0))  # the notifications

    def test_the_middle_stays_where_it_is(self):
        self.assertEqual(to_edge(PIXEL, 200, 300), (540, 1200))
        self.assertIsNone(to_edge(PIXEL, 10, 300))  # off the screen


class ClassifyTest(unittest.TestCase):
    def test_tap(self):
        self.assertEqual(classify(PIXEL, (200, 300), (202, 301), 90, 3), Gesture("tap", (540, 1200)))

    def test_long_press(self):
        gesture = classify(PIXEL, (200, 300), (200, 300), 800, 0)
        self.assertEqual(gesture, Gesture("press", (540, 1200), ms=800))
        self.assertEqual(classify(PIXEL, (200, 300), (200, 300), LONG_PRESS_MS - 1, 0).kind, "tap")

    def test_swipe_keeps_its_speed_within_bounds(self):
        self.assertEqual(classify(PIXEL, (200, 500), (200, 100), 250, 400),
                         Gesture("swipe", (540, 2000), (540, 400), 250))
        self.assertEqual(classify(PIXEL, (200, 500), (200, 100), 10, 400).ms, SHORTEST_SWIPE_MS)
        self.assertEqual(classify(PIXEL, (200, 500), (200, 100), 9000, 400).ms, LONGEST_SWIPE_MS)

    def test_a_drag_back_to_its_start_is_still_a_drag(self):
        # Out and back: it moved, so it isn't a tap.
        self.assertEqual(classify(PIXEL, (200, 300), (200, 300), 400, 120).kind, "swipe")

    def test_starting_off_the_screen_is_nothing(self):
        self.assertIsNone(classify(PIXEL, (10, 300), (200, 300), 90, 190))


class ScrollTest(unittest.TestCase):
    def test_down_the_page_is_a_finger_going_up(self):
        gesture = scroll(PIXEL, (200, 300), 1)
        self.assertEqual(gesture.kind, "swipe")
        self.assertEqual(gesture.start, (540, 1200))
        self.assertEqual(gesture.end, (540, 1200 - int(0.12 * 2400)))

    def test_never_from_or_past_the_edges(self):
        # Near the bottom, where a swipe up would go home: it starts higher.
        gesture = scroll(PIXEL, (200, 599), 1)
        self.assertLessEqual(gesture.start[1], int(2400 * 0.92))
        gesture = scroll(PIXEL, (200, 300), -50)
        self.assertEqual(gesture.end[1], int(2400 * 0.92))

    def test_sideways(self):
        gesture = scroll(PIXEL, (200, 300), 0, 1)  # to the right: the finger goes left
        self.assertEqual(gesture.start, (540, 1200))
        self.assertEqual(gesture.end, (int(540 - 0.12 * 1080), 1200))
        both = scroll(PIXEL, (200, 300), 1, -1)
        self.assertLess(both.end[1], 1200)
        self.assertGreater(both.end[0], 540)

    def test_sideways_never_from_the_side_edges(self):
        # Android goes back on a swipe in from either side: a scroll there starts further in.
        left, _top, right, _bottom = safe_box((1080, 2400))
        gesture = scroll(PIXEL, (66, 300), 0, -1)
        self.assertGreaterEqual(gesture.start[0], left)
        gesture = scroll(PIXEL, (200, 300), 0, -50)
        self.assertEqual(gesture.end[0], right)
        self.assertEqual(inside((1080, 2400), -5, 5000), (left, int(2400 * 0.92)))

    def test_nothing_to_do(self):
        self.assertIsNone(scroll(PIXEL, (200, 300), 0))
        self.assertIsNone(scroll(PIXEL, (10, 300), 1))


if __name__ == "__main__":
    unittest.main()
