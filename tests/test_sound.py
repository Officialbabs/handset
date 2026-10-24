"""Play Sound Here: which Bluetooth device is the phone, and the switch, with stand-in links."""

import os
import tempfile
import unittest

from handset import bluetooth, sound, window
from handset.service import Phone

PIXEL = Phone(id="FAKE7", name="Pixel 7", platform="android", online=True, usb=True, wifi=False,
              ada_allowed=True, ada_doing=None, ada_paused=False, problem=None)


class ChooseTest(unittest.TestCase):
    FOUND = [("/org/bluez/hci0/dev_A", "Test iPhone", "AA:AA"), ("/org/bluez/hci0/dev_B", "Pixel 7", "BB:BB")]

    def test_which_device_is_the_phone(self):
        self.assertEqual(bluetooth.choose(self.FOUND, "Anything", {"BB:BB"}), "/org/bluez/hci0/dev_B")
        self.assertEqual(bluetooth.choose(self.FOUND, "test iphone", set()), "/org/bluez/hci0/dev_A")
        self.assertIsNone(bluetooth.choose(self.FOUND, "Someone else's", set()))  # two, neither it
        self.assertEqual(bluetooth.choose(self.FOUND[:1], "Someone else's", set()), "/org/bluez/hci0/dev_A")
        self.assertIsNone(bluetooth.choose([], "Test iPhone", set()))


class FakeLink:
    def __init__(self, on_ended):
        self.on_ended = on_ended
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeWindow:
    def __init__(self, phone):
        self.phone = phone
        self.said = []
        self.switched = []

    def say(self, text):
        self.said.append(text)

    def sound_switched(self, on):
        self.switched.append(on)


class FakeApp:
    def __init__(self):
        self.settings = {}


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        self.app = FakeApp()
        self.switch = sound.SoundSwitch(self.app)
        self.links = []
        self.switch._link = lambda w: self.links.append(FakeLink(lambda why: self.switch._ended(w, w.phone.id, why))) \
            or self.links[-1]
        self.window = FakeWindow(PIXEL)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.saved
        self.tmp.cleanup()

    def test_on_remembered_and_off(self):
        self.switch.switch(self.window, True)
        self.assertTrue(self.links[0].started)
        self.assertEqual(window.load_settings()["sound_here"], ["FAKE7"])
        self.switch.resume(self.window)  # already playing: not twice
        self.assertEqual(len(self.links), 1)
        self.switch.switch(self.window, False)
        self.assertTrue(self.links[0].stopped)
        self.assertEqual(window.load_settings()["sound_here"], [])

    def test_it_comes_back_with_the_window_and_goes_with_it(self):
        self.app.settings["sound_here"] = ["FAKE7"]
        self.switch.resume(self.window)
        self.assertTrue(self.links[0].started)
        self.switch.release(set())  # its last window closed
        self.assertTrue(self.links[0].stopped)
        self.assertEqual(self.app.settings["sound_here"], ["FAKE7"])  # still wanted next time

    def test_a_link_that_cant_play_turns_the_switch_off(self):
        self.switch.switch(self.window, True)
        self.links[0].on_ended("The phone didn't let its sound be shared.")
        self.assertEqual(self.window.said, ["The phone didn't let its sound be shared."])
        self.assertEqual(self.window.switched, [False])
        self.assertFalse(self.switch.is_on("FAKE7"))
        self.assertTrue(self.links[0].stopped)


if __name__ == "__main__":
    unittest.main()
