"""Scrolling where the phone scrolls as a mouse wheel does (window.py's _wheel_glide, _scroll_by,
_flicked): a wheel's notches glided there a frame at a time, a touchpad's movement passed on as it
comes, a flick thrown on and slowing, a press stopping it; a phone that can't (an iPhone) left to
its finger. A stand-in window and phone: no display, no phone."""

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


class FakeScreen:
    """The screen's frame clock, stood in for: frames come when the test says."""

    def __init__(self):
        self.ticking = None

    def add_tick_callback(self, callback):
        self.ticking = callback
        return 1


def a_window(device):
    # The window 540x1200: the phone's screen at half its size, from (0, 0).
    shown = SimpleNamespace(device=device, pointer=(270.0, 600.0), wheel_left=[0.0, 0.0], thrown=None,
                            motion_tick=0, motion_at=None, pad_scrolled=False, pad_history=[], screen=FakeScreen())
    shown._fit = lambda: Fit((540.0, 1200.0), device.shown)
    for name in ("_scroll_point", "_scroll_by", "_wheel_glide", "_wheel_glide_step", "_throw_step", "_moving",
                 "_motion_frame", "_stop_throw", "_stop_gliding"):
        setattr(shown, name, getattr(Window, name).__get__(shown))
    return shown


def frames(shown, rate=60, most=600):
    """The display's frames, `rate` a second, until the page stops; how many it took."""
    clock = SimpleNamespace(frame_time=0, get_frame_time=lambda: clock.frame_time)
    for count in range(most):
        clock.frame_time += round(1e6 / rate)
        if shown.screen.ticking(None, clock) == GLib.SOURCE_REMOVE:
            shown.screen.ticking = None
            return count + 1
    return most


def a_flick(shown, speed, lasting=100, every=10):
    """A touchpad's scroll at `speed` (the window's pixels a second) for `lasting` milliseconds,
    an event `every` so often, then the fingers lift: GTK's word that they did."""
    shown.pad_scrolled = True
    for at in range(0, lasting + 1, every):
        shown.pad_history.append((1000 + at, 0.0, speed * every / 1000))
    Window._flicked(shown, SimpleNamespace(get_current_event_time=lambda: 1000 + lasting + 5), 0.0, 99.0)


class ScrollTest(unittest.TestCase):
    def test_a_wheels_notches_glide_the_page(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        self.assertTrue(shown._wheel_glide(0, 1))
        self.assertTrue(shown._wheel_glide(0, 1))  # a second notch adds to the way
        took = frames(shown)
        self.assertGreater(len(phone.scrolled), 5)  # in steps, not at once
        self.assertLess(took, 40)  # and done well within a second
        right, down = phone.total()
        self.assertAlmostEqual(down, 2 * SCROLL_PER_NOTCH * 2400, places=3)
        self.assertEqual(right, 0)
        self.assertEqual({s[:2] for s in phone.scrolled}, {(540, 1200)})  # where the pointer is

    def test_the_same_glide_at_any_frame_rate(self):
        glides = []
        for rate in (60, 144):
            phone = FakeAndroid()
            shown = a_window(phone)
            shown._wheel_glide(0, 1)
            took = frames(shown, rate)
            glides.append(took / rate)
        self.assertAlmostEqual(glides[0], glides[1], delta=0.05)  # as long, whatever the display

    def test_a_touchpads_movement_as_it_comes(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        shown.pointer = (-30.0, 50.0)  # off the screen: the nearest of it
        self.assertTrue(shown._scroll_by(4, -10))
        self.assertEqual(phone.scrolled, [(0, 100, 8.0, -20.0)])  # the window's pixels, in the phone's

    def test_a_flick_measured_here_thrown_on_slowing(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        a_flick(shown, 1500)  # 1500 of the window's pixels a second: 3000 of the phone's
        self.assertAlmostEqual(shown.thrown[1], 3000, delta=1)  # not GTK's 99, whatever its unit
        frames(shown)
        _right, down = phone.total()
        # Thrown about as far as its speed lasts, until it's too slow to go on.
        self.assertGreater(down, 3000 * window.GLIDE_FADE_S * 0.9)
        self.assertLess(down, 3000 * window.GLIDE_FADE_S)
        steps = [s[3] for s in phone.scrolled]
        self.assertTrue(all(a > b for a, b in zip(steps, steps[1:])))  # slowing, frame by frame

    def test_never_thrown_too_fast_and_stopped_by_a_press(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        a_flick(shown, 1e6)
        self.assertEqual(shown.thrown[1], window.THROW_FASTEST * 2)
        shown._stop_gliding()  # a press
        frames(shown)
        self.assertEqual(phone.scrolled, [])

    def test_no_throw_once_the_fingers_had_stopped(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        shown.pad_scrolled = True
        shown.pad_history = [(1000, 0.0, 20.0), (1010, 0.0, 20.0)]
        Window._flicked(shown, SimpleNamespace(get_current_event_time=lambda: 1400), 0.0, 99.0)  # lifted later
        self.assertIsNone(shown.thrown)

    def test_no_throw_after_a_finger(self):
        phone = FakeAndroid()
        shown = a_window(phone)
        shown.pad_history = [(1000, 0.0, 20.0), (1010, 0.0, 20.0)]
        Window._flicked(shown, SimpleNamespace(get_current_event_time=lambda: 1015), 0.0, 1.5)
        self.assertIsNone(shown.thrown)  # its scroll went as a finger: the phone throws it itself

    def test_an_iphone_left_to_its_finger(self):
        iphone = SimpleNamespace(shown=(1170, 2532))
        shown = a_window(iphone)
        self.assertFalse(shown._wheel_glide(0, 1))
        self.assertFalse(shown._scroll_by(0, 10))


class SpeedTest(unittest.TestCase):
    def test_over_the_last_moments(self):
        history = [(900, 0.0, 500.0)] + [(1000 + 10 * n, 2.0, -10.0) for n in range(11)]  # an old move left out
        self.assertEqual(window.pad_speed(history, 1105), (200.0, -1000.0))
        self.assertIsNone(window.pad_speed([(1000, 0.0, 5.0)], 1005))


if __name__ == "__main__":
    unittest.main()
