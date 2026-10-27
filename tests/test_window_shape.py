"""The window as its phone (window.py's _shape, _keep_shape, _pill_toggled): an iPhone by its model,
turned as its screen shows; an Android phone as it said; an app's own screen without the cutout;
resized by hand, the window coming back round the phone; and its options pill opening in a row
where there's room, as a list where there isn't. Stand-in windows: no display, no phone."""

import unittest
from types import SimpleNamespace

from handset import body as bodies
from handset.window import CONTROLS, Window


class FakeBody:
    def __init__(self):
        self.set = None
        self.shape = None

    def set_body(self, body, rotation=0, cutout=True):
        self.set = (body, rotation, cutout)


def a_window(platform, model, shown, own=False, said=None):
    device = SimpleNamespace(shown=shown, link=SimpleNamespace(natural=(1080, 2400)), rotation=0)
    return SimpleNamespace(phone=SimpleNamespace(id="PHONE", platform=platform, model=model), device=device,
                           android_shape=said, body=FakeBody(), own_screen=lambda: own)


class ShapeTest(unittest.TestCase):
    def test_an_iphone_by_its_model(self):
        shown = a_window("ios", "iPhone 12", (1170, 2532))
        body, rotation = Window._shape(shown)
        self.assertEqual((body.cutout.kind, rotation), ("notch", 0))
        self.assertEqual(shown.body.set, (body, 0, True))
        turned = a_window("ios", "iPhone 12", (2532, 1170))
        self.assertEqual(Window._shape(turned)[1], 90)

    def test_an_android_phone_as_it_said(self):
        said = bodies.Body((1080, 2400), 47.0, bodies.Cutout("hole", 506, 34, 68, 68))
        shown = a_window("android", "Pixel 7", (1080, 2400), said=said)
        shown.device.rotation = 270
        self.assertEqual(Window._shape(shown), (said, 270))

    def test_an_apps_own_screen_has_no_cutout(self):
        said = bodies.Body((1080, 2400), 47.0, bodies.Cutout("hole", 506, 34, 68, 68))
        shown = a_window("android", "Pixel 7", (720, 1600), own=True, said=said)
        body, rotation = Window._shape(shown)
        self.assertEqual((body.screen, body.cutout, rotation), ((720, 1600), None, 0))
        self.assertAlmostEqual(body.corner, 47 * 720 / 1080)
        self.assertFalse(shown.body.set[2])


class KeepShapeTest(unittest.TestCase):
    def test_resized_by_hand_it_comes_back_round_the_phone(self):
        sized = []
        shown = SimpleNamespace(body=SimpleNamespace(shape=SimpleNamespace(outer=(50.0, 0.0, 300.4, 640.0))),
                                get_default_size=lambda: (400, 684), set_default_size=lambda w, h: sized.append((w, h)))
        Window._keep_shape(shown)
        self.assertEqual(sized, [(300, 640 + CONTROLS)])
        sized.clear()
        shown.get_default_size = lambda: (300, 640 + CONTROLS)  # round it already
        Window._keep_shape(shown)
        self.assertEqual(sized, [])


class FakePill:
    def __init__(self):
        self.active = True

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active

    def get_width(self):
        return 30

    def get_height(self):
        return 28


class OptionsTest(unittest.TestCase):
    def a_window(self, width):
        shown = SimpleNamespace(pill=FakePill(), revealed=[], menus=[], options_timer=0, get_width=lambda: width)
        shown.options = SimpleNamespace(measure=lambda orientation, size: (150, 160, -1, -1))
        shown.options_shown = SimpleNamespace(set_reveal_child=shown.revealed.append)
        shown._menu_at = lambda widget, x, y, window=False: shown.menus.append((widget, window))
        return shown

    def test_a_row_where_theres_room(self):
        shown = self.a_window(300)
        Window._pill_toggled(shown, shown.pill)
        self.assertEqual((shown.revealed, shown.menus), ([True], []))
        Window._close_options(shown)  # an option taken: closed again
        self.assertFalse(shown.pill.active)

    def test_a_list_over_the_phone_where_there_isnt(self):
        shown = self.a_window(190)  # under the row (160 + the pill's 30) and some room
        Window._pill_toggled(shown, shown.pill)
        self.assertEqual(shown.revealed, [])
        self.assertEqual(shown.menus, [(shown.pill, True)])  # with minimizing and closing in it
        self.assertFalse(shown.pill.active)


class Holder:
    """A strip or a floating spot the pill can be put in."""

    def __init__(self, child=None):
        self.child, self.visible, self.revealed = child, True, False

    def set_child(self, child):
        self.child = child

    def set_visible(self, visible):
        self.visible = visible

    def set_reveal_child(self, revealed):
        self.revealed = revealed


class FullScreenTest(unittest.TestCase):
    def test_on_black_the_pill_floating_and_back(self):
        full = [True]
        above, floating = Holder(), Holder()
        controls = SimpleNamespace(get_parent=lambda: above if above.child is controls else floating)
        above.child = controls
        bare = []
        shown = SimpleNamespace(is_fullscreen=lambda: full[0], body=SimpleNamespace(set_bare=bare.append),
                                fullscreen_button=SimpleNamespace(set_icon_name=lambda name: None,
                                                                  set_tooltip_text=lambda text: None),
                                controls=controls, above=above, floating=floating, float_timer=0,
                                _float_later=lambda: None)
        Window._fullscreen_changed(shown)
        self.assertEqual((bare, above.visible, floating.child, floating.revealed), ([True], False, controls, True))
        full[0] = False
        Window._fullscreen_changed(shown)
        self.assertEqual((bare[-1], above.visible, above.child, floating.child), (False, True, controls, None))


if __name__ == "__main__":
    unittest.main()
