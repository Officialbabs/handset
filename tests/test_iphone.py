"""Handset tests for the iPhone side: what it asks the helper for, against a stand-in helper."""

import json
import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import iphone
from handset.gestures import Gesture
from handset.iphone import SPAN, IPhone, Link, normalized, plainly, screen_crop

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_HELPER = os.path.join(HERE, "fakeios.py")


def spin(until, seconds=10.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.005)


class PointsTest(unittest.TestCase):
    def test_corners_and_middle(self):
        self.assertEqual(normalized(0, 0, (390, 844)), (0, 0))
        self.assertEqual(normalized(389, 843, (390, 844)), (SPAN, SPAN))
        x, y = normalized(194.5, 421.5, (390, 844))
        self.assertAlmostEqual(x, SPAN / 2, delta=1)
        self.assertAlmostEqual(y, SPAN / 2, delta=1)

    def test_never_off_the_glass(self):
        self.assertEqual(normalized(-20, 900, (390, 844)), (0, SPAN))


class WordsTest(unittest.TestCase):
    def test_an_unreachable_phone_said_plainly(self):
        for why in ("No route to host (os error 113)", "adapter closed", "The iPhone went to sleep or left Wi-Fi."):
            self.assertTrue(plainly(why).startswith("It went to sleep or left Wi-Fi"))
        self.assertEqual(plainly("The iPhone isn't on this Wi-Fi: unlock it and keep it near."),
                         "The iPhone isn't on this Wi-Fi: unlock it and keep it near.")


class CropTest(unittest.TestCase):
    def test_the_padding_goes(self):
        # What an iPhone 12 really sends: its 1170×2532 padded out to 1184×2576.
        self.assertEqual(screen_crop("iPhone 12", (1184, 2576)), (1170, 2532))
        self.assertEqual(screen_crop("iPhone 12", (2576, 1184)), (2532, 1170))  # turned

    def test_nothing_cut_when_unsure(self):
        self.assertIsNone(screen_crop("iPhone 99", (1184, 2576)))
        self.assertIsNone(screen_crop("iPhone 12", (720, 1560)))  # a smaller stream: not padding
        self.assertIsNone(screen_crop("", (1184, 2576)))


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (iphone.PAIRINGS, os.environ.get("HANDSET_IOS_HELPER"))
        iphone.PAIRINGS = self.tmp.name

    def tearDown(self):
        iphone.PAIRINGS = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("HANDSET_IOS_HELPER", None)
        else:
            os.environ["HANDSET_IOS_HELPER"] = self.saved[1]
        self.tmp.cleanup()

    def test_needs_the_pairing_ada_made(self):
        os.environ["HANDSET_IOS_HELPER"] = FAKE_HELPER
        self.assertIn("Set the iPhone up with Ada", iphone.open_link("UDID-1", None))
        with open(os.path.join(self.tmp.name, "UDID-1.rppairing"), "w") as file:
            file.write("<plist/>")
        link = iphone.open_link("UDID-1", "192.168.18.10", "iPhone 12")
        self.assertEqual(link, Link("UDID-1", os.path.join(self.tmp.name, "UDID-1.rppairing"),
                                    "192.168.18.10", FAKE_HELPER, "iPhone 12"))


class FakeHelper(unittest.TestCase):
    """The stand-in helper's picture is a ball on black; tests say what the window would see."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("FAKEIOS_DIR")
        os.environ["FAKEIOS_DIR"] = self.tmp.name
        self.frames, self.touch, self.ended, self.turned = [], [], [], []
        self.phone = IPhone(Link("UDID-1", "/p.rppairing", "192.168.18.10", FAKE_HELPER),
                            self.frames.append, lambda: self.turned.append(1), self.touch.append,
                            self.ended.append)

    def tearDown(self):
        self.phone.stop()
        if self.saved is None:
            os.environ.pop("FAKEIOS_DIR", None)
        else:
            os.environ["FAKEIOS_DIR"] = self.saved
        self.tmp.cleanup()

    def commands(self):
        path = os.path.join(self.tmp.name, "commands.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as file:
            return [json.loads(line) for line in file if line.strip()]

    def test_screen_and_its_size(self):
        self.phone.start()
        spin(lambda: len(self.frames) >= 2 and self.touch)
        self.assertEqual(self.ended, [])
        self.assertEqual(self.phone.shown, (390, 844))
        self.assertTrue(self.turned)  # the first picture gives the screen's real shape
        self.assertEqual(self.touch, [True])
        with open(os.path.join(self.tmp.name, "args.json")) as file:
            args = json.load(file)
        self.assertEqual(args[:5], ["stream", "--pairing", "/p.rppairing", "--udid", "UDID-1"])
        self.assertIn("192.168.18.10", args)

    def test_touches_buttons_and_typing(self):
        self.phone.start()
        spin(lambda: self.frames and self.phone.live_touch)
        self.phone.press(0, 0)
        self.phone.drag(389, 843)
        self.phone.release()
        self.phone.key("home")
        self.phone.key("enter")
        self.assertEqual(self.phone.type("hi é"), 1)
        spin(lambda: len(self.commands()) >= 6)
        self.assertEqual(self.commands()[:6], [
            {"op": "down", "x": 0, "y": 0}, {"op": "move", "x": SPAN, "y": SPAN}, {"op": "up"},
            {"op": "button", "name": "home"}, {"op": "key", "name": "enter"},
            {"op": "text", "text": "hi "}])

    def test_back_is_a_swipe_from_the_left_edge(self):
        self.phone.start()
        spin(lambda: self.frames and self.phone.live_touch)
        self.phone.key("back")
        spin(lambda: self.commands() and self.commands()[-1] == {"op": "up"})
        moves = self.commands()
        # A couple of pixels in from the edge, halfway down: iOS doesn't take a touch on the very
        # first column for the edge swipe.
        self.assertEqual(moves[0]["op"], "down")
        self.assertTrue(0 < moves[0]["x"] < SPAN // 100)
        self.assertEqual(moves[0]["y"], normalized(0, round(0.5 * 843), (390, 844))[1])
        self.assertGreater(moves[-2]["x"], SPAN // 2)

    def test_a_dark_screen_and_a_click_to_wake(self):
        changes = []
        self.phone.on_awake = changes.append
        self.phone.looks_dark = lambda: True  # as the window finds a black picture
        os.environ["FAKEIOS_DARK_AFTER"] = "1"
        saved = iphone.DARK_AFTER_S
        iphone.DARK_AFTER_S = 0.6
        try:
            self.phone.start()
            spin(lambda: changes == [False])
            self.assertFalse(self.phone.awake)
            self.phone.wake()
            spin(lambda: {"op": "button", "name": "home"} in self.commands())
        finally:
            iphone.DARK_AFTER_S = saved
            del os.environ["FAKEIOS_DARK_AFTER"]

    def test_a_still_lit_screen_keeps_taking_taps(self):
        changes = []
        self.phone.on_awake = changes.append
        self.phone.looks_dark = lambda: False  # frames stopped, but the picture is lit
        os.environ["FAKEIOS_DARK_AFTER"] = "1"
        saved = iphone.DARK_AFTER_S
        iphone.DARK_AFTER_S = 0.6
        try:
            self.phone.start()
            spin(lambda: self.frames)
            end = time.monotonic() + 3
            spin(lambda: time.monotonic() > end, seconds=5)
            self.assertTrue(self.phone.awake)
            self.assertEqual(changes, [])
        finally:
            iphone.DARK_AFTER_S = saved
            del os.environ["FAKEIOS_DARK_AFTER"]

    def test_a_tap_from_a_gesture(self):
        self.phone.start()
        spin(lambda: self.frames and self.phone.live_touch)
        self.phone.gesture(Gesture("tap", (195, 422)))
        spin(lambda: len(self.commands()) >= 2)
        self.assertEqual([c["op"] for c in self.commands()], ["down", "up"])


if __name__ == "__main__":
    unittest.main()
