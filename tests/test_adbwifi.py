"""Android phones connected again over Wi-Fi (adbwifi): where each was found remembered; a phone
Handset uses that isn't connected, connected where it says its adb listens, or now and then where
it was; at once when it's wanted; adb's server kept apart from Handset. A stand-in browser and the
stand-in adb: no network, no phone."""

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from handset import adbwifi, android
from handset.adbwifi import WifiReconnect, serial_of
from handset.appsync import AppSync
from handset.service import CABLE_ONLY, DISCONNECTED

HERE = os.path.dirname(__file__)


class FakeBrowser:
    def __init__(self):
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class NameTest(unittest.TestCase):
    def test_serial_of(self):
        self.assertEqual(serial_of("adb-1A2B3C4D5E6F7G-x8Kq2a"), "1A2B3C4D5E6F7G")
        self.assertEqual(serial_of("adb-FAKE-7-x8Kq2a"), "FAKE-7")
        for name in ("adb-x8Kq2a", "something else", "adb-FAKE7-"):
            self.assertIsNone(serial_of(name), name)


class ReconnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HANDSET_ADB", "FAKEADB_DIR", "XDG_CACHE_HOME")}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["FAKEADB_DIR"] = self.tmp.name
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")
        self.wanted: set[str] = set()
        self.reconnect = WifiReconnect(lambda: self.wanted, FakeBrowser())

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def asked(self, wait=0.0):
        """The adb commands asked so far (waiting up to `wait` seconds for one)."""
        end = time.monotonic() + wait
        while True:
            try:
                with open(os.path.join(self.tmp.name, "log.jsonl")) as file:
                    return [json.loads(line) for line in file]
            except OSError:
                if time.monotonic() >= end:
                    return []
                time.sleep(0.02)

    def test_a_wanted_phone_is_connected_where_it_listens(self):
        self.wanted = {"FAKE7"}
        self.reconnect._found("adb-FAKE7-x8Kq2a", "192.0.2.7", 5555)
        self.assertEqual(self.asked(wait=5), [["connect", "192.0.2.7:5555"]])

    def test_only_the_wanted_and_seen(self):
        self.reconnect._found("adb-FAKE7-x8Kq2a", "192.0.2.7", 5555)  # connected already
        self.wanted = {"OTHER"}  # not on the network
        self.reconnect.check()
        time.sleep(0.3)
        self.assertEqual(self.asked(), [])

    def test_not_again_too_soon(self):
        self.wanted = {"FAKE7"}
        self.reconnect._found("adb-FAKE7-x8Kq2a", "192.0.2.7", 5555)
        self.reconnect.check()
        self.reconnect.check()
        self.assertEqual(len(self.asked(wait=5)), 1)
        time.sleep(0.3)
        self.assertEqual(len(self.asked()), 1)
        # Long enough after, it's tried again.
        self.reconnect.tried["FAKE7"] -= adbwifi.AGAIN_S
        self.reconnect.check()
        end = time.monotonic() + 5
        while len(self.asked()) < 2 and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(len(self.asked()), 2)

    def test_quiet_on_the_network_asked_rarely_where_it_was(self):
        self.reconnect._found("adb-FAKE7-x8Kq2a", "192.0.2.7", 5555)
        self.reconnect._lost("adb-FAKE7-x8Kq2a")  # asleep, say
        self.wanted = {"FAKE7"}
        self.reconnect.check()
        self.assertEqual(self.asked(wait=5), [["connect", "192.0.2.7:5555"]])
        self.reconnect.tried["FAKE7"] -= adbwifi.AGAIN_S  # long enough for one that says where it is
        self.reconnect.check()
        time.sleep(0.3)
        self.assertEqual(len(self.asked()), 1)
        self.reconnect.now()  # but its files searched: asked at once
        end = time.monotonic() + 5
        while len(self.asked()) < 2 and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(len(self.asked()), 2)

    def test_remembered_where_found_and_connected_when_wanted(self):
        with open(os.path.join(self.tmp.name, "others"), "w") as file:
            file.write("192.0.2.9:5555 WIFI9 0\n")  # a phone adb has only over Wi-Fi
        self.assertEqual(android.find_serial("WIFI9"), "192.0.2.9:5555")
        self.assertEqual(adbwifi.remembered("WIFI9"), ("192.0.2.9", 5555))
        self.assertTrue(adbwifi.connect_now("WIFI9"))
        self.assertIn(["connect", "192.0.2.9:5555"], self.asked())
        self.assertFalse(adbwifi.connect_now("NEVER-ON-WIFI"))

    def test_started_and_stopped(self):
        self.reconnect.start()
        self.assertTrue(self.reconnect.browser.started)
        self.assertTrue(self.reconnect.timer)
        self.reconnect.stop()
        self.assertTrue(self.reconnect.browser.stopped)
        self.assertEqual(self.reconnect.timer, 0)


class ServerTest(unittest.TestCase):
    def test_adbs_server_started_in_a_scope_of_its_own(self):
        ran = []
        with mock.patch.dict(os.environ, {"HANDSET_ADB": ""}), \
                mock.patch.object(android.socket, "create_connection", side_effect=OSError), \
                mock.patch.object(android.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(android.subprocess, "run", side_effect=lambda command, **_: ran.append(command)):
            android._server_checked = -android.SERVER_CHECK_S
            android.keep_server("/usr/bin/adb")
            android.keep_server("/usr/bin/adb")  # not asked again so soon
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0][:3], ["/usr/bin/systemd-run", "--user", "--scope"])
        self.assertEqual(ran[0][-2:], ["/usr/bin/adb", "start-server"])

    def test_not_for_the_stand_in(self):
        with mock.patch.dict(os.environ, {"HANDSET_ADB": "/x/fakeadb.py"}), \
                mock.patch.object(android.subprocess, "run") as run:
            android._server_checked = -android.SERVER_CHECK_S
            android.keep_server("/x/fakeadb.py")
        run.assert_not_called()


class DialTest(unittest.TestCase):
    def test_a_window_connects_a_phone_away_where_it_was(self):
        from gi.repository import GLib

        from handset.window import Window

        shown = SimpleNamespace(dialling=None, dialled_at=-60.0, phone=None)
        shown._dialled = lambda phone_id, connected: Window._dialled(shown, phone_id, connected)
        pixel = SimpleNamespace(id="PIXEL", name="Pixel", online=False)
        never = SimpleNamespace(id="NEVER", name="Other", online=False)
        place = {"PIXEL": ("192.0.2.7", 5555)}
        with mock.patch.object(adbwifi, "remembered", place.get), \
                mock.patch.object(adbwifi, "connect_now", return_value=True) as connected:
            self.assertTrue(Window._dial(shown, pixel))
            self.assertTrue(Window._dial(shown, pixel))  # still connecting: once
            end = time.monotonic() + 5
            while shown.dialling and time.monotonic() < end:
                GLib.MainContext.default().iteration(False)
                time.sleep(0.01)
            connected.assert_called_once_with("PIXEL")
            self.assertFalse(Window._dial(shown, pixel))  # not again so soon
            shown.dialled_at = -60.0
            self.assertFalse(Window._dial(shown, never))  # never found on Wi-Fi: said as before


class WhichTest(unittest.TestCase):
    def test_android_phones_not_connected_nor_kept_off(self):
        def phone(id, platform="android", online=False, problem=None):
            return SimpleNamespace(id=id, platform=platform, online=online, problem=problem)

        sync = SimpleNamespace(phones_now=[
            phone("DROPPED"), phone("ON", online=True), phone("IPHONE", "ios"),
            phone("SWITCHED", problem=DISCONNECTED), phone("CABLED", problem=CABLE_ONLY),
            phone("ASLEEP", problem="Unlock it"),
        ])
        self.assertEqual(AppSync.to_reconnect(sync), {"DROPPED", "ASLEEP"})


if __name__ == "__main__":
    unittest.main()
