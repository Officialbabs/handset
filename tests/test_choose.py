"""Handset tests for which phone the window shows."""

import unittest

from handset.service import Phone
from handset.window import FRAME, choose, fitted, migrated, window_size
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
    def test_one_window_settings_move_over(self):
        old = {"phone": "P7", "size": 600, "maximized": True}
        self.assertEqual(migrated(old), {"windows": ["P7"], "sizes": {"P7": 600}, "maximized": {"P7": True}})

    def test_new_settings_stay(self):
        new = {"windows": ["P7", "IPH"], "sizes": {"P7": 600, "IPH": 640}, "maximized": {}}
        self.assertEqual(migrated(new), new)

    def test_nothing_yet(self):
        self.assertEqual(migrated({}), {"windows": [], "sizes": {}, "maximized": {}})


class SizeTest(unittest.TestCase):
    LAPTOP = (1366, 768)

    def test_upright_phone_fits_the_laptop(self):
        w, h = fitted(1080 / 2400, self.LAPTOP)
        self.assertEqual(h, 614)  # 80% of the screen's height
        self.assertAlmostEqual(w / h, 0.45, places=2)

    def test_turned_phone_fits_too(self):
        w, h = fitted(2400 / 1080, self.LAPTOP)
        self.assertLessEqual(w, 1366 * 0.8)
        self.assertLessEqual(h, 768 * 0.8)
        self.assertAlmostEqual(w / h, 2400 / 1080, places=2)

    def test_the_persons_size_kept_while_it_fits(self):
        self.assertEqual(fitted(0.45, self.LAPTOP, 500), (225, 500))
        self.assertEqual(fitted(0.45, self.LAPTOP, 2000)[1], 614)

    def test_the_bezel_above_and_the_chin_below(self):
        width, height = window_size(1080 / 2400, self.LAPTOP)
        self.assertEqual((width, height - FRAME), fitted(1080 / 2400, (1366, 768 - FRAME)))
        self.assertLessEqual(height, 768)

    def test_a_big_monitor(self):
        w, h = fitted(0.45, (3840, 2160))
        self.assertEqual(h, 1728)


if __name__ == "__main__":
    unittest.main()
