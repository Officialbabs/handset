"""Handset tests for the Android side: what it reads from the phone, how it turns points, what it
asks uinput and `input` for, and the live video and touchscreen against a stand-in adb."""

import json
import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import android
from handset.android import (Android, AppScreen, Link, Touch, Video, display_size, down_events, even_size,
                             find_serial, gesture_command, list_apps, parse_app_list, start_app_message,
                             text_messages, touch_message,
                             key_command, parse_awake, parse_devices, parse_locked, parse_rotation, parse_wm_size,
                             shown_size,
                             stream_edge, text_command, to_natural, touch_device, up_events)
from handset.gestures import Gesture

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_ADB = os.path.join(HERE, "fakeadb.py")


def spin(until, seconds=8.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.005)


class ReadingTest(unittest.TestCase):
    def test_devices(self):
        text = ("List of devices attached\n1A2B3C4D5E6F7G\tdevice usb:1-1 product:panther\n"
                "192.168.18.20:5555\toffline\nemulator-5554\tunauthorized\n\n")
        self.assertEqual(parse_devices(text), ["1A2B3C4D5E6F7G"])

    def test_wm_size(self):
        self.assertEqual(parse_wm_size("Physical size: 1080x2400\n"), (1080, 2400))
        self.assertEqual(parse_wm_size("Physical size: 1080x2400\nOverride size: 720x1600\n"), (1080, 2400))
        self.assertIsNone(parse_wm_size("nope"))

    def test_rotation(self):
        line = "  Viewport INTERNAL: displayId=0, uniqueId=local:1, port=1, orientation={}, logicalFrame=[0, 0]"
        self.assertEqual(parse_rotation(line.format(0)), 0)
        self.assertEqual(parse_rotation(line.format(1)), 90)
        self.assertEqual(parse_rotation(line.format("ROTATION_270")), 270)
        self.assertIsNone(parse_rotation("Viewport EXTERNAL: orientation=1"))
        self.assertIsNone(parse_rotation(""))


class AwakeTest(unittest.TestCase):
    def test_wakefulness(self):
        self.assertTrue(parse_awake("  mWakefulness=Awake\n"))
        self.assertTrue(parse_awake("mWakefulness=Dreaming"))
        self.assertFalse(parse_awake("  mWakefulness=Dozing"))
        self.assertFalse(parse_awake("mWakefulness=Asleep"))
        self.assertIsNone(parse_awake("mWakefulnessChanging=false"))


class LockedTest(unittest.TestCase):
    def test_lock_screen(self):
        self.assertTrue(parse_locked("    isKeyguardShowing=true\n"))
        self.assertFalse(parse_locked("isKeyguardShowing=false"))
        self.assertIsNone(parse_locked("mWakefulness=Awake"))


class TurningTest(unittest.TestCase):
    NATURAL = (1080, 2400)

    def test_shown_size(self):
        self.assertEqual(shown_size(self.NATURAL, 0), (1080, 2400))
        self.assertEqual(shown_size(self.NATURAL, 90), (2400, 1080))

    def test_corners_land_on_the_upright_screen(self):
        # Turned 90°: the shown top-left corner is the upright screen's top-right.
        self.assertEqual(to_natural(0, 0, self.NATURAL, 90), (1079, 0))
        self.assertEqual(to_natural(2399, 1079, self.NATURAL, 90), (0, 2399))
        self.assertEqual(to_natural(0, 0, self.NATURAL, 270), (0, 2399))
        self.assertEqual(to_natural(0, 0, self.NATURAL, 180), (1079, 2399))
        self.assertEqual(to_natural(5, 7, self.NATURAL, 0), (5, 7))

    def test_every_shown_point_stays_on_the_screen(self):
        for rotation in (0, 90, 180, 270):
            w, h = shown_size(self.NATURAL, rotation)
            for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                nx, ny = to_natural(x, y, self.NATURAL, rotation)
                self.assertTrue(0 <= nx < 1080 and 0 <= ny < 2400, (rotation, x, y))

    def test_even_video_size(self):
        self.assertEqual(even_size(self.NATURAL, 0), (1080, 2400))  # the screen's own size
        self.assertEqual(even_size(self.NATURAL, 0, 1600), (720, 1600))
        self.assertEqual(even_size(self.NATURAL, 90, 1600), (1600, 720))
        self.assertEqual(even_size((1081, 1201), 0), (1080, 1200))


class EdgeTest(unittest.TestCase):
    def test_twice_the_monitor_and_no_more_than_the_phone(self):
        self.assertEqual(stream_edge(768, (1080, 2400)), 1536)  # a 1366×768 laptop
        self.assertEqual(stream_edge(1080, (1080, 2400)), 2160)  # a 1080p monitor
        self.assertEqual(stream_edge(1440, (1080, 2400)), 0)  # the phone's own size
        self.assertEqual(stream_edge(0, (1080, 2400)), 0)


class CommandsTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(key_command("back"), "input keyevent KEYCODE_BACK")
        self.assertEqual(key_command("recents"), "input keyevent KEYCODE_APP_SWITCH")
        self.assertEqual(key_command("notifications"), "cmd statusbar expand-notifications")
        self.assertIsNone(key_command("teleport"))

    def test_text(self):
        self.assertEqual(text_command("hi there"), ("input text 'hi%sthere'", 0))
        self.assertEqual(text_command("it's"), ("input text 'it'\\''s'", 0))
        self.assertEqual(text_command("café"), ("input text 'caf'", 1))
        self.assertEqual(text_command("😀"), (None, 1))

    def test_gestures(self):
        self.assertEqual(gesture_command(Gesture("tap", (5, 6))), "input tap 5 6")
        self.assertEqual(gesture_command(Gesture("press", (5, 6), ms=800)), "input swipe 5 6 5 6 800")
        self.assertEqual(gesture_command(Gesture("swipe", (5, 6), (7, 8), 250)), "input swipe 5 6 7 8 250")

    def test_touchscreen(self):
        device = touch_device((1080, 2400))
        self.assertEqual(device["command"], "register")
        self.assertEqual(device["bus"], "usb")
        # type before data, as uinput reads them; the property makes it a touchscreen.
        self.assertTrue(all(list(entry)[:2] == ["type", "data"] for entry in device["configuration"]))
        self.assertIn({"type": 110, "data": [1]}, device["configuration"])
        axes = {a["code"]: a["info"]["maximum"] for a in device["abs_info"]}
        self.assertEqual(axes["ABS_MT_POSITION_X"], 1079)
        self.assertEqual(axes["ABS_MT_POSITION_Y"], 2399)
        self.assertEqual(len(down_events(1, 2, 3)) % 3, 0)
        self.assertEqual(up_events()[-3:], ["EV_SYN", "SYN_REPORT", 0])


class FakeAdb(unittest.TestCase):
    """The stand-in adb, for tests that talk to it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HANDSET_ADB", "FAKEADB_DIR")}
        os.environ["HANDSET_ADB"] = FAKE_ADB
        os.environ["FAKEADB_DIR"] = self.tmp.name
        self.scrcpy = android.SCRCPY_SERVER
        android.SCRCPY_SERVER = os.path.join(self.tmp.name, "no-scrcpy")

    def tearDown(self):
        android.SCRCPY_SERVER = self.scrcpy
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def logged(self, name):
        path = os.path.join(self.tmp.name, name)
        if not os.path.exists(path):
            return []
        with open(path) as file:
            return [json.loads(line) for line in file if line.strip()]


class FakePhone(FakeAdb):
    def test_finding_the_phone(self):
        self.assertEqual(find_serial("FAKE7"), "FAKE7")
        self.assertIsNone(find_serial("SOMEONE-ELSE"))
        link = android.open_link("FAKE7", None, None)
        self.assertEqual(link, Link("FAKE7", (1080, 2400), 0, True))
        with open(os.path.join(self.tmp.name, "wakefulness"), "w") as file:
            file.write("Dozing")
        self.assertFalse(android.open_link("FAKE7", None, None).awake)
        self.assertIsInstance(android.open_link("SOMEONE-ELSE", None, None), str)

    def test_finding_it_by_cable_or_wifi_at_once(self):
        with open(os.path.join(self.tmp.name, "others"), "w") as file:
            file.write("192.168.18.20:5555 FAKE7 0\n"  # the same phone over Wi-Fi
                       "adb-PIXEL9-x1Yz._adb-tls-connect._tcp PIXEL9 0\n"
                       "slow-one SLOW 3\n"
                       "quick-one QUICK 0\n")
        self.assertEqual(find_serial("FAKE7", "192.168.18.20", 5555), "FAKE7")  # the cable first
        self.assertEqual(find_serial("PIXEL9"), "adb-PIXEL9-x1Yz._adb-tls-connect._tcp")
        began = time.monotonic()
        self.assertEqual(find_serial("QUICK"), "quick-one")
        self.assertLess(time.monotonic() - began, 2.5)  # not waiting on the slow one first

    def test_live_video(self):
        frames, ended = [], []
        video = Video("FAKE7", (1080, 2400), 0, frames.append, ended.append)
        video.start()
        try:
            spin(lambda: len(frames) >= 3 or ended)
        finally:
            video.stop()
        self.assertEqual(ended, [])
        self.assertEqual(video.kind, "screenrecord")
        self.assertEqual((frames[-1].get_width(), frames[-1].get_height()), (1080, 2400))
        asked = [" ".join(a) for a in self.logged("log.jsonl")]
        self.assertTrue(any("--time-limit 0" in a and "--size 1080x2400" in a for a in asked))

    def test_turning_restarts_screenrecord_in_the_new_shape(self):
        frames, ended = [], []
        video = Video("FAKE7", (1080, 2400), 0, frames.append, ended.append)
        video.start()
        try:
            spin(lambda: len(frames) >= 1)
            video.turned(90)
            spin(lambda: frames and frames[-1].get_width() == 2400)
        finally:
            video.stop()
        self.assertEqual(frames[-1].get_height(), 1080)

    def test_virtual_touchscreen(self):
        states = []
        touch = Touch("FAKE7", (1080, 2400), lambda: states.append("ready"), lambda: states.append("failed"))
        touch.start()
        try:
            spin(lambda: states)
            self.assertEqual(states, ["ready"])
            touch.down(10, 20)
            touch.move(30, 40)
            touch.up()
            spin(lambda: len(self.logged("uinput.jsonl")) >= 5)
        finally:
            touch.stop()
        commands = self.logged("uinput.jsonl")
        self.assertEqual([c["command"] for c in commands], ["register", "sync", "inject", "inject", "inject"])
        self.assertEqual(commands[2]["events"][:6], ["EV_ABS", "ABS_MT_SLOT", 0, "EV_ABS", "ABS_MT_TRACKING_ID", 1])

    def test_a_phone_without_uinput_falls_back_to_input(self):
        states = []
        os.environ["HANDSET_ADB"] = os.path.join(self.tmp.name, "missing-adb")
        touch = Touch("FAKE7", (1080, 2400), lambda: states.append("ready"), lambda: states.append("failed"))
        touch.start()
        spin(lambda: states)
        self.assertEqual(states, ["failed"])

    def test_the_phone_turned_touches_follow(self):
        frames, turned = [], []
        phone = Android(Link("FAKE7", (1080, 2400), 0), frames.append, lambda: turned.append(1),
                        lambda live: None, lambda why: None)
        android.ROTATION_POLL_S = 0.2
        try:
            phone.start()
            spin(lambda: phone.live_touch)
            with open(os.path.join(self.tmp.name, "rotation"), "w") as file:
                file.write("90")
            spin(lambda: turned)
            self.assertEqual(phone.shown, (2400, 1080))
            phone.press(0, 0)
            phone.release()
            spin(lambda: len(self.logged("uinput.jsonl")) >= 4)
        finally:
            android.ROTATION_POLL_S = 1.5
            phone.stop()
        down = self.logged("uinput.jsonl")[2]["events"]
        self.assertEqual(down[6:12], ["EV_ABS", "ABS_MT_POSITION_X", 1079, "EV_ABS", "ABS_MT_POSITION_Y", 0])

    def test_a_sleeping_phone_is_woken_and_said_so(self):
        with open(os.path.join(self.tmp.name, "wakefulness"), "w") as file:
            file.write("Dozing")
        changes = []
        phone = Android(Link("FAKE7", (1080, 2400), 0, False), lambda t: None, lambda: None,
                        lambda live: None, lambda why: None, changes.append)
        android.ROTATION_POLL_S = 0.2
        try:
            phone.start()
            spin(lambda: changes == [True])  # woken by Handset, and counted on at once
            # The wake key reaches the phone a moment later; only then does it go back to sleep.
            spin(lambda: ["shell", "input keyevent KEYCODE_WAKEUP"] in self.logged("log.jsonl"))
            with open(os.path.join(self.tmp.name, "wakefulness"), "w") as file:
                file.write("Asleep")
            spin(lambda: changes == [True, False])
        finally:
            android.ROTATION_POLL_S = 1.5
            phone.stop()
        self.assertFalse(phone.awake)
        self.assertIn(["shell", "input keyevent KEYCODE_WAKEUP"], self.logged("log.jsonl"))

    def test_keys_and_text_go_in_order(self):
        phone = Android(Link("FAKE7", (1080, 2400), 0), lambda t: None, lambda: None, lambda live: None,
                        lambda why: None)
        phone.type("hello")
        phone.key("enter")
        spin(lambda: sum(1 for a in self.logged("log.jsonl") if a[:1] == ["shell"]) >= 2)
        phone.stop()
        shells = [a[1] for a in self.logged("log.jsonl") if a[:1] == ["shell"] and "dumpsys" not in a[1]]
        self.assertEqual(shells, ["input text 'hello'", "input keyevent KEYCODE_ENTER"])


class AppMessagesTest(unittest.TestCase):
    def test_the_app_list(self):
        text = ("[server] INFO: Device: [Google] google Pixel 7 (Android 17)\n"
                "[server] INFO: List of apps:\n"
                " * Settings                       com.android.settings\n"
                " - WhatsApp                       com.whatsapp\n"
                " - An Application With A Long Name\n"
                "   org.example.long\n")
        self.assertEqual(parse_app_list(text), [("Settings", "com.android.settings"), ("WhatsApp", "com.whatsapp"),
                                                ("An Application With A Long Name", "org.example.long")])

    def test_messages(self):
        touch = touch_message(0, 10, 20, (1080, 2400))
        self.assertEqual(len(touch), 32)
        self.assertEqual(touch[:2], bytes([2, 0]))
        self.assertEqual(start_app_message("com.whatsapp"), bytes([16, 12]) + b"com.whatsapp")
        long = text_messages("é" * 200)  # 400 bytes: two messages, no character split
        self.assertEqual(long[:5], bytes([1, 0, 0, 1, 44]))
        self.assertEqual(len(long), 5 + 300 + 5 + 100)

    def test_its_screen_is_the_phones_upright(self):
        self.assertEqual(display_size((1080, 2400)), (1080, 2400))
        self.assertEqual(display_size((2400, 1080)), (1080, 2400))
        self.assertEqual(display_size((1179, 2556)), (1176, 2552))


class AppScreenTest(FakeAdb):
    """One app on a screen of its own, through the stand-in for scrcpy's server."""

    def setUp(self):
        super().setUp()
        self.version = android.scrcpy_version
        android.scrcpy_version = lambda: "3.3.4"

    def tearDown(self):
        android.scrcpy_version = self.version
        super().tearDown()

    def test_listing_the_apps(self):
        self.assertEqual(list_apps("FAKE7"), [("A Very Long Application Name Indeed", "org.example.long"),
                                              ("Fake Chat", "org.example.chat"), ("Settings", "com.android.settings")])

    def test_an_app_on_its_own_screen(self):
        frames, touch, ended = [], [], []
        screen = AppScreen(Link("FAKE7", (1080, 2400), 0), "org.example.chat", frames.append, lambda: None,
                           touch.append, ended.append)
        screen.start()
        try:
            spin(lambda: (touch and frames) or ended, seconds=20)
            self.assertEqual(ended, [])
            screen.press(100, 200)
            screen.drag(300, 400)
            screen.release()
            screen.key("back")
            screen.key("home")  # the phone's own: not on this screen
            screen.type("hi")
            screen._decoder_broken()  # a decoder started afresh: the phone's video does too
            spin(lambda: len(self.logged("control.jsonl")) >= 8)
        finally:
            screen.stop()
        sent = self.logged("control.jsonl")
        self.assertEqual(sent[0], {"type": "start_app", "package": "org.example.chat"})
        self.assertEqual([(m["action"], m["x"], m["y"], m["size"]) for m in sent if m["type"] == "touch"],
                         [(0, 100, 200, [1080, 2400]), (2, 300, 400, [1080, 2400]), (1, 300, 400, [1080, 2400])])
        self.assertEqual([(m["action"], m["code"]) for m in sent if m["type"] == "key"], [(0, 4), (1, 4)])
        self.assertEqual([m["text"] for m in sent if m["type"] == "text"], ["hi"])
        self.assertEqual(sent[-1], {"type": "reset_video"})
        self.assertEqual(ended, [])  # the app stays open
        started = next(a[1] for a in self.logged("log.jsonl") if a[:1] == ["shell"] and "Server" in a[1])
        self.assertIn("new_display=1080x2400", started)
        self.assertIn("control=true", started)

    def test_an_app_thats_gone(self):
        ended = []
        screen = AppScreen(Link("FAKE7", (1080, 2400), 0), "org.example.gone", lambda t: None, lambda: None,
                           lambda live: None, ended.append)
        screen.start()
        try:
            spin(lambda: ended, seconds=20)
        finally:
            screen.stop()
        self.assertEqual(ended, ["That app isn't on the phone any more."])


if __name__ == "__main__":
    unittest.main()
