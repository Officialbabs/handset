"""Handset tests for which phone the window shows."""

import unittest

from handset.service import Phone
from handset import body as bodies
from handset.window import CONTROLS, SIZES_SINCE, choose, migrated, window_size
from tests.fakeservice import phone


def phones(*entries):
    return [Phone.from_state(e) for e in entries]


class ChooseTest(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(choose([], None))

    def test_asked_for_by_id_or_name(self):
        both = phones(phone(), phone(id="IPH", name="Test iPhone", platform="ios"))
        self.assertEqual(choose(both, "IPH").name, "Test iPhone")
        self.assertEqual(choose(both, "Test iPhone").id, "IPH")

    def test_a_connected_android_first(self):
        listed = phones(phone(id="IPH", name="Test iPhone", platform="ios", wifi=True),
                        phone(id="OLD", name="Old Pixel"), phone(id="P7", name="Pixel 7", usb=True))
        self.assertEqual(choose(listed, None).id, "P7")
        self.assertEqual(choose(listed, "gone").id, "P7")

    def test_then_any_android_then_anything(self):
        self.assertEqual(choose(phones(phone(id="IPH", platform="ios"), phone(id="P7")), None).id, "P7")
        self.assertEqual(choose(phones(phone(id="IPH", platform="ios")), None).id, "IPH")


class TwoWindowsTest(unittest.TestCase):
    PIXEL, IPHONE = phone(id="P7", name="Pixel 7", usb=True), phone(id="IPH", name="Test iPhone", platform="ios")

    def test_a_second_window_takes_the_other_phone(self):
        both = phones(self.PIXEL, self.IPHONE)
        self.assertEqual(choose(both, None).id, "P7")
        self.assertEqual(choose(both, None, taken={"P7"}).id, "IPH")

    def test_asked_for_is_asked_for(self):
        self.assertEqual(choose(phones(self.PIXEL, self.IPHONE), "P7", taken={"P7"}).id, "P7")

    def test_every_phone_taken(self):
        both = phones(self.PIXEL, self.IPHONE)
        self.assertEqual(choose(both, None, taken={"P7", "IPH"}).id, "P7")
        self.assertIsNone(choose(both, None, taken={"P7", "IPH"}, free_only=True))


class SettingsTest(unittest.TestCase):
    def test_old_sizes_go(self):
        # Kept when the default was smaller: windows open at the new one.
        old = {"phone": "P7", "size": 600, "maximized": True, "windows": ["P7"]}
        self.assertEqual(migrated(old), {"sizes": {}, "maximized": {}, "sizes_since": SIZES_SINCE})

    def test_new_settings_stay(self):
        new = {"sizes": {"P7": 600, "apps": 640}, "maximized": {}, "sizes_since": SIZES_SINCE}
        self.assertEqual(migrated(dict(new, windows=["P7"])), new)  # which had windows: not kept

    def test_nothing_yet(self):
        self.assertEqual(migrated({}), {"sizes": {}, "maximized": {}, "sizes_since": SIZES_SINCE})


class SizeTest(unittest.TestCase):
    LAPTOP = (1366, 768)
    PIXEL = bodies.parse_android("RoundedCorner{position=TopLeft, radius=47, center=Point(47, 47)}", (1080, 2400))

    def screen(self, size, rotation=0):
        """The screen's size as the window would draw it, at that window size."""
        shape = bodies.layout(self.PIXEL, rotation, size[0], size[1] - CONTROLS)
        return shape.screen[2], shape.screen[3]

    def test_upright_phone_fits_the_laptop(self):
        w, h = window_size(self.PIXEL, 0, self.LAPTOP)
        self.assertLessEqual(h, 768 * 0.9)
        self.assertGreater(h, 768 * 0.9 - 4)  # as large as fits
        sw, sh = self.screen((w, h))
        self.assertAlmostEqual(sw / sh, 0.45, places=2)

    def test_turned_phone_fits_too(self):
        w, h = window_size(self.PIXEL, 90, self.LAPTOP)
        self.assertLessEqual(w, 1366 * 0.9)
        self.assertLessEqual(h, 768 * 0.9)
        sw, sh = self.screen((w, h), 90)
        self.assertAlmostEqual(sw / sh, 2400 / 1080, places=2)

    def test_the_persons_size_kept_while_it_fits(self):
        self.assertAlmostEqual(max(self.screen(window_size(self.PIXEL, 0, self.LAPTOP, 500))), 500, delta=2)
        self.assertLessEqual(window_size(self.PIXEL, 0, self.LAPTOP, 2000)[1], 768 * 0.9)

    def test_the_rim_round_its_screen_and_its_options_above(self):
        w, h = window_size(self.PIXEL, 0, self.LAPTOP, 500)
        sw, sh = self.screen((w, h))
        self.assertAlmostEqual(w - sw, 2 * bodies.RIM, delta=1)  # a hairline of rim either side
        self.assertGreater(h - sh, CONTROLS)

    def test_a_big_monitor(self):
        self.assertLessEqual(window_size(self.PIXEL, 0, (3840, 2160))[1], 1944)


if __name__ == "__main__":
    unittest.main()
