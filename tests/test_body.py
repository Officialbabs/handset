"""A phone's screen (body.py): an Android phone's corners and cutout as `dumpsys display` says them,
an iPhone's by its model, kept for next time, and laid out at a size: the screen in its middle at the
phone's shape, its rim round it, the cutout turning with it. No phone, no display."""

import os
import tempfile
import unittest
from unittest import mock

from handset import body as bodies

# As a Pixel 7 on Android 17 says it (the parts read), its punch hole a circle 68 pixels across.
PIXEL = ("roundedCorners RoundedCorners{[RoundedCorner{position=TopLeft, radius=47, center=Point(47, 47)}, "
         "RoundedCorner{position=TopRight, radius=47, center=Point(1033, 47)}]}, "
         "cutout DisplayCutout{insets=Rect(0, 136 - 0, 0) boundingRect={Bounds=[Rect(0, 0 - 0, 0), "
         "Rect(480, 0 - 625, 136), Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0)]} cutoutPathParserInfo={CutoutPathParserInfo"
         "{displayWidth=1080 displayHeight=2400 density={2.625} cutoutSpec={M 506,68 a 34,34 0 1 0 68,0 34,34 0 "
         "1 0 -68,0 Z @left} rotation={0}}}")


class AndroidTest(unittest.TestCase):
    def test_a_punch_hole(self):
        body = bodies.parse_android(PIXEL, (1080, 2400))
        self.assertEqual(body.corner, 47)
        self.assertEqual(body.cutout, bodies.Cutout("hole", 506, 34, 68, 68))

    def test_a_hole_from_the_middle_in_dp(self):
        text = "cutoutSpec={M -13,30 a 13,13 0 1 0 26,0 13,13 0 1 0 -26,0 Z @dp} density={3.0}"
        body = bodies.parse_android(text, (1080, 2400))
        self.assertEqual(body.cutout, bodies.Cutout("hole", 501, 51, 78, 78))

    def test_a_notch_by_its_box(self):
        text = "boundingRect={Bounds=[Rect(0, 0 - 0, 0), Rect(380, 0 - 700, 90), Rect(0, 0 - 0, 0)]} cutoutSpec={M 0,0 L -160,0 Z}"
        self.assertEqual(bodies.parse_android(text, (1080, 2400)).cutout, bodies.Cutout("notch", 380, 0, 320, 90))

    def test_saying_nothing(self):
        body = bodies.parse_android("", (1080, 2400))
        self.assertIsNone(body.cutout)
        self.assertGreater(body.corner, 0)


class IPhoneTest(unittest.TestCase):
    def test_by_model(self):
        twelve = bodies.iphone_body("iPhone 12", (1170, 2532))
        self.assertAlmostEqual(twelve.corner, 142, delta=1)
        self.assertEqual((twelve.cutout.kind, twelve.cutout.width, twelve.cutout.y), ("notch", 630, 0))
        self.assertAlmostEqual(twelve.cutout.x, (1170 - 630) / 2)
        island = bodies.iphone_body("iPhone 15 Pro", (1179, 2556)).cutout
        self.assertEqual(island.kind, "island")
        self.assertGreater(island.y, 0)  # it floats below the top
        se = bodies.iphone_body("iPhone SE (3rd generation)", (750, 1334))
        self.assertEqual((se.corner, se.cutout), (0, None))  # square, a Home button below it
        self.assertIsNone(bodies.iphone_body("iPhone 99", (1200, 2600)).cutout)  # unknown: none drawn


class KeptTest(unittest.TestCase):
    def test_kept_for_next_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
            body = bodies.parse_android(PIXEL, (1080, 2400))
            bodies.keep("PIXEL", body)
            self.assertEqual(bodies.kept("PIXEL"), body)
            self.assertEqual(bodies.kept("PIXEL", (1080, 2400)), body)
            self.assertIsNone(bodies.kept("PIXEL", (1440, 3120)))  # another screen: asked again
            self.assertIsNone(bodies.kept("OTHER"))
            with mock.patch("handset.android.shell", side_effect=AssertionError("asked")):
                self.assertEqual(bodies.android_body("PIXEL", "serial", (1080, 2400)), body)


class LayoutTest(unittest.TestCase):
    def test_the_screen_at_the_phones_shape_in_the_middle(self):
        body = bodies.parse_android(PIXEL, (1080, 2400))
        shape = bodies.layout(body, 0, 400, 800)
        x, y, w, h = shape.screen
        self.assertAlmostEqual(w / h, 1080 / 2400, places=3)
        ox, oy, ow, oh = shape.outer
        self.assertTrue(ow <= 400 and oh <= 800 and (abs(ow - 400) < 0.5 or abs(oh - 800) < 0.5))  # as large as fits
        glass = bodies.rim(body, shape.scale)
        self.assertEqual((x - ox, y - oy), (glass, glass))  # the rim alone round it, no bezel
        self.assertAlmostEqual(ox + ow - (x + w), glass, places=3)
        self.assertAlmostEqual(shape.screen_radius, 47 * shape.scale)
        self.assertAlmostEqual(shape.outer_radius, shape.screen_radius + glass)
        kind, cx, cy, cw, ch = shape.cutout
        self.assertEqual(kind, "hole")
        self.assertAlmostEqual(cx + cw / 2, x + w / 2, places=3)  # at the top, in the middle

    def test_turned_its_top_goes_left(self):
        body = bodies.iphone_body("iPhone 12", (1170, 2532))
        shape = bodies.layout(body, 90, 800, 400)
        x, y, w, h = shape.screen
        self.assertGreater(w, h)
        _kind, cx, cy, cw, ch = shape.cutout
        self.assertAlmostEqual(cx, x)  # hanging from the left edge
        self.assertGreater(ch, cw)
        self.assertAlmostEqual(cy + ch / 2, y + h / 2, places=3)
        self.assertEqual(bodies.layout(body, 270, 800, 400).cutout[1] + cw, x + w)  # the right, at 270

    def test_no_cutout_on_an_apps_own_screen(self):
        body = bodies.parse_android(PIXEL, (1080, 2400))
        self.assertIsNone(bodies.layout(body, 0, 400, 800, cutout=False).cutout)

    def test_a_hairline_of_rim_and_at_the_top_left(self):
        body = bodies.parse_android(PIXEL, (1080, 2400))
        shape = bodies.layout(body, 0, 500, 800, middle=False)
        self.assertEqual(shape.outer[:2], (0.0, 0.0))  # what stays put as the window comes round it
        self.assertEqual(shape.screen[0] - shape.outer[0], bodies.RIM)
        self.assertAlmostEqual(shape.outer[3], 800, places=3)  # as tall as fits, the room left at the right


if __name__ == "__main__":
    unittest.main()
