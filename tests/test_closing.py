"""An iPhone app's window closing (window.py's _iphone_home): the iPhone back on its home screen,
through whichever window has its screen, never lit up by it when it's dark, and its screen ended
only once Home is done. Stand-in windows and iPhones: no display, no phone."""

import time
import unittest
from types import SimpleNamespace

from gi.repository import GLib

from handset import window
from handset.window import Window


class FakeIPhone:
    def __init__(self, awake=True):
        self.live_touch = True
        self.awake = awake
        self.keys: list[str] = []
        self.stopped = False

    def key(self, name):
        self.keys.append(name)

    def stop(self):
        self.stopped = True


class FakeApp:
    def __init__(self):
        self.windows = []
        self.given = []

    def handset_windows(self):
        return self.windows

    def screen_back(self, closing):
        self.given.append(closing)


def a_window(app, device, app_window=True, phone_id="IPHONE"):
    shown = SimpleNamespace(phone=SimpleNamespace(id=phone_id, platform="ios"), device=device,
                            phone_app=object() if app_window else None, gave_screen=False,
                            get_application=lambda: app)
    shown._stop_after_home = lambda device, app: Window._stop_after_home(shown, device, app)
    app.windows.append(shown)
    return shown


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)


class HomeTest(unittest.TestCase):
    def test_home_then_its_screen_ends(self):
        app, phone = FakeApp(), FakeIPhone()
        closing = a_window(app, phone)
        self.assertTrue(Window._iphone_home(closing))
        self.assertEqual(phone.keys, ["home"])
        self.assertIsNone(closing.device)  # not stopped with the window: a moment later
        self.assertFalse(phone.stopped)
        self.assertEqual(app.given, [])
        spin(window.HOME_FIRST_MS / 1000 + 0.3)
        self.assertTrue(phone.stopped)
        self.assertEqual(app.given, [closing])  # then the screen goes back to a window waiting

    def test_through_the_window_that_has_the_screen(self):
        app = FakeApp()
        phone = FakeIPhone()
        a_window(app, phone, app_window=False)  # the iPhone's own window has its screen now
        a_window(app, FakeIPhone(), phone_id="ANOTHER")
        closing = a_window(app, None)
        self.assertFalse(Window._iphone_home(closing))
        self.assertEqual(phone.keys, ["home"])
        self.assertFalse(phone.stopped)  # that window's screen goes on

    def minimizing(self, home_before, awake=True):
        app, phone = FakeApp(), FakeIPhone(awake=awake)
        shown = a_window(app, phone)
        shown.home_before, shown.sent_home, shown.minimized = home_before, False, False
        shown.minimize = lambda: setattr(shown, "minimized", True)
        shown.phone_app = SimpleNamespace(app="org.example.chat")
        Window.minimize_window(shown)
        self.assertTrue(shown.minimized)  # minimized whatever the iPhone does
        return shown, phone

    def test_minimized_over_the_home_screen_goes_home(self):
        shown, phone = self.minimizing(True)
        self.assertEqual(phone.keys, ["home"])
        self.assertTrue(shown.sent_home)
        # Back to the window: its app comes back.
        launched = []
        phone.launch = lambda app, front=False: launched.append((app, front))
        shown.is_active, shown.gave_screen = (lambda: True), False
        Window._activated(shown)
        self.assertEqual(launched, [("org.example.chat", True)])
        self.assertFalse(shown.sent_home)

    def test_minimized_over_another_app_stays(self):
        for home_before, awake in ((False, True), (None, True), (True, False)):
            with self.subTest(home_before=home_before, awake=awake):
                shown, phone = self.minimizing(home_before, awake)
                self.assertEqual(phone.keys, [])
                self.assertFalse(shown.sent_home)

    def test_a_dark_iphone_stays_dark(self):
        app, phone = FakeApp(), FakeIPhone(awake=False)
        closing = a_window(app, phone)
        self.assertFalse(Window._iphone_home(closing))
        self.assertEqual(phone.keys, [])
        self.assertIs(closing.device, phone)  # stopped with the window, as before


if __name__ == "__main__":
    unittest.main()
