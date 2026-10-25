"""Handset tests for the iPhone side: what it asks the helper for, against a stand-in helper."""

import json
import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import iphone
from handset.gestures import Gesture
from handset.iphone import SPAN, ClipboardLink, FilesSession, IPhone, Link, list_apps, normalized, plainly, screen_crop

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
        for why in ("No route to host (os error 113)", "adapter closed",
                    "The iPhone went to sleep or left Wi-Fi: it didn't answer.",
                    "The iPhone's link by cable went: it was unplugged, went to sleep or left Wi-Fi."):
            self.assertTrue(plainly(why).startswith("It isn't plugged in, and it's asleep or off Wi-Fi"))
        for why in ("The iPhone isn't plugged in or on this Wi-Fi: plug it in, or unlock it and keep it near.",
                    "The iPhone is plugged in but its cable link didn't open (PasswordProtected): unlock it."):
            self.assertEqual(plainly(why), why)

    def test_the_phones_own_words_from_its_errors(self):
        call = ('device returned an error: Dictionary({"code": Integer(9022), "userInfo": Dictionary({'
                '"NSLocalizedDescription": String("A phone or VoIP call is currently in progress on the device.")}), '
                '"domain": String("com.apple.dt.CoreDeviceError")})')
        self.assertEqual(plainly(call), "It's on a call: its screen shows here again when the call ends.")
        other = ('device returned an error: Dictionary({"code": Integer(1), "userInfo": Dictionary({'
                 '"NSLocalizedDescription": String("The \\"display\\" is busy.")})})')
        self.assertEqual(plainly(other), 'The \\"display\\" is busy.')


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

    def test_an_app_opened_once_it_takes_input(self):
        launched = []
        self.phone.on_launched = lambda ok, why: launched.append((ok, why))
        self.phone.launch("org.example.chat")  # before it takes input: kept for then
        self.phone.start()
        spin(lambda: launched)
        self.phone.launch("org.example.gone")
        spin(lambda: len(launched) >= 2)
        self.assertEqual(launched, [(True, ""), (False, "Unlock the iPhone to open it.")])
        self.assertEqual([c["bundle"] for c in self.commands() if c["op"] == "launch"],
                         ["org.example.chat", "org.example.gone"])

    def test_its_clipboard_both_ways(self):
        heard = []
        clip = ClipboardLink(Link("UDID-1", "/p.rppairing", "192.168.18.10", FAKE_HELPER), heard.append)
        clip.start()
        try:
            spin(lambda: clip.process is not None and os.path.exists(os.path.join(self.tmp.name, "commands.jsonl")))
            with open(os.path.join(self.tmp.name, "phone_clipboard"), "w") as file:
                file.write("copied on the iPhone")
            spin(lambda: heard)
            clip.set("copied on the computer")
            spin(lambda: any(c.get("op") == "set" for c in self.commands()))
            time.sleep(0.4)
            self.assertEqual(heard, ["copied on the iPhone"])  # its own isn't heard back
            self.assertEqual([c["text"] for c in self.commands() if c.get("op") == "set"], ["copied on the computer"])
        finally:
            clip.stop()

    def test_its_files(self):
        media = os.path.join(self.tmp.name, "media")
        os.makedirs(os.path.join(media, "DCIM", "100APPLE"))
        os.makedirs(os.path.join(media, "Downloads"))
        with open(os.path.join(media, "Downloads", "notes.txt"), "w") as file:
            file.write("from the iPhone")
        session = FilesSession(Link("UDID-1", "/p.rppairing", "192.168.18.10", FAKE_HELPER))
        try:
            self.assertEqual([(f.name, f.folder) for f in session.list_folder("/")], [("DCIM", True), ("Downloads", True)])
            here = os.path.join(self.tmp.name, "notes.txt")
            with open(here, "w") as file:
                file.write("from the computer")
            sent, trouble = session.send_files([here], "/Downloads")
            self.assertEqual((sent, trouble), (["/Downloads/notes (2).txt"], None))  # not over the phone's own
            got = session.fetch_file("/Downloads/notes.txt", os.path.join(self.tmp.name, "back"))
            with open(got) as file:
                self.assertEqual(file.read(), "from the iPhone")
            self.assertIsInstance(session.list_folder("/Nowhere"), str)
            with open(os.path.join(media, "DCIM", "100APPLE", "IMG_0042.HEIC"), "w") as file:
                file.write("a photo")
            found = session.find_files("/", "img_00")
            self.assertEqual([(f.name, f.path) for f in found], [("IMG_0042.HEIC", "/DCIM/100APPLE/IMG_0042.HEIC")])
        finally:
            session.stop()

    def test_apps_that_share_their_files(self):
        from handset import apps
        from handset.apps import PhoneApp

        saved = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "XDG_CACHE_HOME")}
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp.name, "data")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")
        try:
            for bundle, name in (("org.videolan.vlc-ios", "VLC"), ("org.localsend.localsendApp", "LocalSend"),
                                 ("com.apple.calculator", "Calculator")):
                apps.write_entry(PhoneApp("UDID-1", "Test iPhone", "ios", bundle, name))
            os.makedirs(os.path.join(self.tmp.name, "apps", "org.videolan.vlc-ios"))
            os.makedirs(os.path.join(self.tmp.name, "apps", "org.localsend.localsendApp"))
            os.makedirs(os.path.join(self.tmp.name, "media", "DCIM"))
            session = FilesSession(Link("UDID-1", "/p.rppairing", "192.168.18.10", FAKE_HELPER))
            try:
                self.assertEqual([f.name for f in session.list_folder("/")], ["On My iPhone", "DCIM"])
                self.assertEqual([(f.name, f.path) for f in session.list_folder("/On My iPhone")],
                                 [("LocalSend", "/On My iPhone/org.localsend.localsendApp"),
                                  ("VLC", "/On My iPhone/org.videolan.vlc-ios")])
                self.assertEqual(session.display("/On My iPhone/org.videolan.vlc-ios/Films"),
                                 ["On My iPhone", "VLC", "Films"])
                # Videos into VLC, the rest into LocalSend: where the Files app shows them.
                self.assertEqual(session.drop_folder(["/x/clip.mp4"]),
                                 ("/On My iPhone/org.videolan.vlc-ios", "Files › On My iPhone › VLC"))
                self.assertEqual(session.drop_folder(["/x/notes.pdf"])[0], "/On My iPhone/org.localsend.localsendApp")
                here = os.path.join(self.tmp.name, "clip.mp4")
                with open(here, "w") as file:
                    file.write("a video")
                sent, trouble = session.send_files([here], "/On My iPhone/org.videolan.vlc-ios")
                self.assertEqual((sent, trouble), (["/On My iPhone/org.videolan.vlc-ios/clip.mp4"], None))
                self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "apps", "org.videolan.vlc-ios", "clip.mp4")))
            finally:
                session.stop()
            # Photos and videos into Photos, through the shortcut; the rest into LocalSend.
            photo = os.path.join(self.tmp.name, "beach.jpg")
            with open(photo, "w") as file:
                file.write("a photo")
            session = FilesSession(Link("UDID-1", "/p.rppairing", "192.168.18.10", FAKE_HELPER))
            session.PHOTOS_WAIT_S = 3
            try:
                sent, where, trouble = session.deliver([photo])  # no shortcut yet: it waits in Files
                self.assertEqual((where, trouble), ("set up", None))
                waiting = os.path.join(self.tmp.name, "apps", "org.localsend.localsendApp", "Handset")
                self.assertEqual(os.listdir(waiting), ["beach.jpg"])
                open(os.path.join(self.tmp.name, "shortcut"), "w").close()
                sent, where, trouble = session.deliver([photo])
                self.assertEqual(where, "Photos › Albums › Handset")
                self.assertEqual(os.listdir(waiting), [])  # in Photos: the copies gone
                self.assertIn("IMG_beach.jpg", os.listdir(os.path.join(self.tmp.name, "media", "DCIM", "100APPLE")))
            finally:
                session.stop()
            # Asked once a day: the next session knows without asking.
            again = FilesSession(Link("UDID-1", "/p.rppairing", "192.168.18.10", "/nonexistent"))
            self.assertEqual(sorted(again.sharing().values()), ["LocalSend", "VLC"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_its_apps_and_their_icons(self):
        icons = os.path.join(self.tmp.name, "icons")
        apps = list_apps(Link("UDID-1", "/p.rppairing", "192.168.18.10", FAKE_HELPER), icons)
        self.assertEqual(apps, [("Fake Chat", "org.example.chat", os.path.join(icons, "org.example.chat.png")),
                                ("Maps", "com.apple.Maps", None)])
        self.assertTrue(os.path.isfile(apps[0][2]))

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

    def test_recents_is_home_pressed_twice(self):
        self.phone.start()
        spin(lambda: self.frames and self.phone.live_touch)
        self.phone.key("recents")
        spin(lambda: len(self.commands()) >= 2)
        self.assertEqual(self.commands(), [{"op": "button", "name": "home"}] * 2)

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
        os.environ["FAKEIOS_DARK_AFTER"] = "6"  # time for a first picture, however busy the machine
        saved = iphone.DARK_AFTER_S
        iphone.DARK_AFTER_S = 0.6
        try:
            self.phone.start()
            spin(lambda: changes == [False], seconds=20)
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
        os.environ["FAKEIOS_DARK_AFTER"] = "6"  # time for a first picture, however busy the machine
        saved = iphone.DARK_AFTER_S
        iphone.DARK_AFTER_S = 0.6
        try:
            self.phone.start()
            spin(lambda: self.frames, seconds=20)
            end = time.monotonic() + 7  # past the pictures stopping, and then some
            spin(lambda: time.monotonic() > end, seconds=9)
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
