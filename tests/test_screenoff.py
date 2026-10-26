"""Phone Screen Off as a switch (screenoff.py): one per phone, shown alike in all its windows, and
never left on: when no window shows the phone, when it sleeps, when its link goes, when Handset
quits. Stand-in windows and a stand-in for the phone's session (android.ScreenOff)."""

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from gi.repository import GLib

from handset import android
from handset.screenoff import ScreenSwitch


class FakeScreenOff:
    made = []

    def __init__(self, serial, on_ended=None):
        self.serial = serial
        self.on_ended = on_ended
        self.started = self.stopped = False
        FakeScreenOff.made.append(self)

    def start(self):
        self.started = True

    def stop(self, light=True):
        self.stopped = True
        self.lit = light


class FakeWindow:
    def __init__(self, phone_id, platform="android", serial="FAKE7"):
        self.phone = SimpleNamespace(id=phone_id, name=f"Phone {phone_id}", platform=platform)
        self.serial = serial
        self.said = []
        self.shown = []

    def _serial(self):
        return self.serial

    def say(self, text):
        self.said.append(text)

    def screen_off_switched(self, on):
        self.shown.append(on)


def spin(until, seconds=2.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    return until()


class SwitchTest(unittest.TestCase):
    def setUp(self):
        FakeScreenOff.made = []
        patcher = mock.patch.object(android, "ScreenOff", FakeScreenOff)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pixel, self.app_window = FakeWindow("PIXEL"), FakeWindow("PIXEL")  # its own window and an app's
        self.iphone = FakeWindow("IPHONE", platform="ios", serial=None)
        self.app = SimpleNamespace(handset_windows=lambda: [self.pixel, self.app_window, self.iphone])
        self.switch = ScreenSwitch(self.app)

    def test_one_switch_per_phone(self):
        self.switch.switch(self.pixel, True)
        self.assertTrue(self.switch.is_on("PIXEL"))
        self.assertEqual(len(FakeScreenOff.made), 1)
        self.assertTrue(FakeScreenOff.made[0].started)
        self.assertEqual((self.pixel.shown, self.app_window.shown), ([True], [True]))  # shown alike
        self.switch.switch(self.app_window, True)  # already off: nothing more
        self.assertEqual(len(FakeScreenOff.made), 1)
        self.switch.switch(self.app_window, False)
        self.assertTrue(spin(lambda: FakeScreenOff.made[0].stopped))
        self.assertEqual(self.pixel.shown, [True, False])

    def test_not_for_an_iphone(self):
        self.switch.switch(self.iphone, True)
        self.assertEqual(FakeScreenOff.made, [])

    def test_never_left_off(self):
        for case in ("no window shows it", "it fell asleep", "Handset quits"):
            with self.subTest(case):
                FakeScreenOff.made = []
                self.switch.switch(self.pixel, True)
                if case == "no window shows it":
                    self.switch.release({"IPHONE"})
                elif case == "it fell asleep":
                    self.switch.asleep("PIXEL")
                else:
                    self.switch.stop()
                self.assertTrue(spin(lambda: FakeScreenOff.made[0].stopped))
                self.assertFalse(self.switch.is_on("PIXEL"))
                # A phone gone to sleep isn't lit again: its screen stays dark.
                self.assertEqual(FakeScreenOff.made[0].lit, case != "it fell asleep")
        self.switch.switch(self.pixel, True)
        self.switch.release({"PIXEL"})  # still shown: stays off
        self.assertTrue(self.switch.is_on("PIXEL"))

    def test_its_link_going(self):
        self.switch.switch(self.pixel, True)
        FakeScreenOff.made[0].on_ended("The phone's screen is back on: its link went.")
        self.assertFalse(self.switch.is_on("PIXEL"))
        self.assertEqual(self.pixel.shown[-1], False)
        self.assertIn("The phone's screen is back on: its link went.", self.pixel.said)

    def test_a_phone_not_connected(self):
        self.pixel.serial = None
        self.switch.switch(self.pixel, True)
        self.assertEqual(FakeScreenOff.made, [])
        self.assertEqual(self.pixel.shown, [False])
        self.assertTrue(self.pixel.said)


if __name__ == "__main__":
    unittest.main()
