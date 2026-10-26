"""Scrolling where the phone scrolls as a mouse wheel does (window.py's _wheel_glide, _scroll_by,
_flicked): a wheel's notches glided there a frame at a time, a touchpad's movement passed on as it
comes, a flick thrown on and slowing, a press stopping it; a phone that can't (an iPhone) left to
its finger. A stand-in window and phone: no display, no phone."""

import math
import time
import unittest
from types import SimpleNamespace

from gi.repository import GLib

from handset import window
from handset.gestures import SCROLL_PER_NOTCH, Fit
from handset.window import Window


class FakeAndroid:
    shown = (1080, 2400)

    def __init__(self):
        self.scrolled: list[tuple[int, int, float, float]] = []

    def scroll(self, x, y, right, down):
        if right or down:
            self.scrolled.append((x, y, right, down))
        return True

    def total(self):
        return sum(s[2] for s in self.scrolled), sum(s[3] for s in self.scrolled)


def a_window(device):
    # The window 540x1200: the phone's screen at half its size, from (0, 0).
    shown = SimpleNamespace(device=device, pointer=(270.0, 600.0), wheel_left=[0.0, 0.0], wheel_glide=0,
                            thrown=None, thrown_at=0.0, throw_timer=0, pad_scrolled=False)
    shown._fit = lambda: Fit((540.0, 1200.0), device.shown)
    for name in ("_scroll_point", "_scroll_by", "_wheel_glide", "_wheel_glide_step", "_throw_step",
                 "_stop_throw", "_stop_gliding"):
        setattr(shown, name, getattr(Window, name).__get__(shown))
    return shown


def spin(until, seconds=5.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    return until()


class ScrollTest(unittest.TestCase):
    def test_a_wheels_notches_glide_the_page(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        self.assertTrue(shown._wheel_glide(0, 1))
        self.assertTrue(shown._wheel_glide(0, 1))  # a second notch adds to the way
        self.assertTrue(spin(lambda: not shown.wheel_glide))
        self.assertGreater(len(phone.scrolled), 5)  # in steps, not at once
        right, down = phone.total()
        self.assertAlmostEqual(down, 2 * SCROLL_PER_NOTCH * 2400, places=3)
        self.assertEqual(right, 0)
        self.assertEqual({s[:2] for s in phone.scrolled}, {(540, 1200)})  # where the pointer is

    def test_a_touchpads_movement_as_it_comes(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        shown.pointer = (-30.0, 50.0)  # on the bezel: the nearest of the screen
        self.assertTrue(shown._scroll_by(4, -10))
        self.assertEqual(phone.scrolled, [(0, 100, 8.0, -20.0)])  # the window's pixels, in the phone's

    def test_a_flick_thrown_on_slowing_and_stopped_by_a_press(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        shown.pad_scrolled = True
        Window._flicked(shown, None, 0.0, 1.5)  # 1.5 of the window's pixels a millisecond: 3000 of the phone's a second
        self.assertTrue(spin(lambda: not shown.throw_timer))
        _right, down = phone.total()
        # Thrown about as far as its speed lasts, until it's too slow to go on.
        self.assertGreater(down, 3000 * window.GLIDE_FADE_S * 0.9)
        self.assertLess(down, 3000 * window.GLIDE_FADE_S)
        steps = [s[3] for s in phone.scrolled]
        self.assertTrue(all(a >= b * 0.8 for a, b in zip(steps, steps[1:])))  # slowing
        # Thrown again, a press stops it at once.
        phone.scrolled.clear()
        shown.pad_scrolled = True
        Window._flicked(shown, None, 0.0, -1.5)
        shown._stop_gliding()
        spin(lambda: False, seconds=0.1)
        self.assertEqual(phone.scrolled, [])

    def test_no_throw_after_a_finger(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        Window._flicked(shown, None, 0.0, 1.5)  # the touchpad's scroll went as a finger: the phone throws it
        self.assertFalse(shown.throw_timer)

    def test_an_iphone_left_to_its_finger(self):
        iphone = SimpleNamespace(shown=(1170, 2532))
        shown = a_window(iphone)
        self.assertFalse(shown._wheel_glide(0, 1))
        self.assertFalse(shown._scroll_by(0, 10))


if __name__ == "__main__":
    unittest.main()
