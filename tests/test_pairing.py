"""Setting phones up where Handset does it: an Android phone paired over Wi-Fi with its code
(through the stand-in adb), Apple's developer disk image fetched and checked, an iPhone's steps
through the stand-in helper (fakeios.py), and Start at Sign-In."""

import hashlib
import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import pairing, phones
from handset.pairing import (AndroidWifi, IPhoneStep, ddi_folder, fetch_ddi, paired, parse_address, wifi_address,
                             wifi_by_cable)

HERE = os.path.dirname(os.path.abspath(__file__))


def spin(until, seconds=8.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    return until()


class Temporary(unittest.TestCase):
    keys = ("HANDSET_ADB", "FAKEADB_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FAKEIOS_DIR")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in self.keys}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        for key in ("FAKEADB_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FAKEIOS_DIR"):
            os.environ[key] = self.tmp.name

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class WordsTest(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(parse_address("192.168.1.5:37123"), ("192.168.1.5", 37123))
        self.assertEqual(parse_address(" [fe80::1]:4000 "), ("fe80::1", 4000))
        self.assertIsNone(parse_address("192.168.1.5"))
        self.assertTrue(paired("Successfully paired to 192.168.1.5:41235 [guid=adb-X-Y]"))
        self.assertFalse(paired("Failed: Wrong password or connection was dropped."))

    def test_the_phones_wifi_address(self):
        loopback = "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
        # A Pixel 7 on Android 17: its Wi-Fi on wlan1, wlan0 down.
        self.assertEqual(wifi_address(loopback + "48: wlan1    inet 192.168.18.11/24 brd 192.168.18.255 scope global "
                                                 "dynamic wlan1\\       valid_lft 86106sec preferred_lft 86106sec"),
                         "192.168.18.11")
        self.assertEqual(wifi_address(loopback + "30: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global "
                                                 "wlan0"), "192.168.1.20")
        self.assertIsNone(wifi_address(loopback + "31: swlan0    inet 192.168.43.1/24 scope global swlan0"))  # hotspot
        self.assertIsNone(wifi_address(loopback))


class AndroidWifiTest(Temporary):
    def pair(self, code):
        steps, done = [], []
        wifi = AndroidWifi(steps.append, done.append)
        wifi.connecting["192.168.1.5"] = ("192.168.1.5", 37000)  # as Avahi finds it
        wifi.pair(code, "192.168.1.5:41235")
        self.assertTrue(spin(lambda: done, 15))
        return steps, done[0]

    def test_paired_and_connected(self):
        steps, problem = self.pair("123456")
        self.assertIsNone(problem)
        self.assertEqual(steps, ["Pairing…", "Paired. Connecting…"])
        with open(os.path.join(self.tmp.name, "log.jsonl")) as file:
            log = file.read()
        self.assertIn('["pair", "192.168.1.5:41235", "123456"]', log)
        self.assertIn('["connect", "192.168.1.5:37000"]', log)

    def test_a_wrong_code(self):
        _steps, problem = self.pair("654321")
        self.assertIn("didn't pair", problem)

    def test_not_a_code(self):
        done = []
        AndroidWifi(lambda words: None, done.append).pair("12", "192.168.1.5:41235")
        self.assertEqual(done, ["The code is the six digits the phone shows."])


class WifiByCableTest(Temporary):
    def test_turned_on_by_its_cable(self):
        with open(os.path.join(self.tmp.name, "wifi"), "w") as file:
            file.write("48: wlan1    inet 192.168.18.11/24 brd 192.168.18.255 scope global dynamic wlan1\n")
        self.assertEqual(wifi_by_cable("FAKE7"), ("192.168.18.11", 5555))
        with open(os.path.join(self.tmp.name, "log.jsonl")) as file:
            log = file.read()
        self.assertIn('["tcpip", "5555"]', log)
        self.assertIn('["connect", "192.168.18.11:5555"]', log)

    def test_not_on_wifi(self):
        self.assertIn("isn't on Wi-Fi", wifi_by_cable("FAKE7"))


class DiskImageTest(Temporary):
    def test_fetched_checked_and_found(self):
        source = os.path.join(self.tmp.name, "source")
        os.makedirs(source)
        sums = {}
        for name in pairing.DDI_FILES:
            with open(os.path.join(source, name), "wb") as file:
                file.write(name.encode())
            sums[name] = hashlib.sha256(name.encode()).hexdigest()
        saved = dict(pairing.DDI_SHA256)
        pairing.DDI_SHA256.update(sums)
        try:
            self.assertIsNone(ddi_folder())
            steps = []
            self.assertIsNone(fetch_ddi(steps.append, f"file://{source}/"))
            self.assertEqual(len(steps), len(pairing.DDI_FILES))
            self.assertEqual(ddi_folder(), os.path.join(self.tmp.name, "handset", "ddi"))
            self.assertIsNone(fetch_ddi(steps.append, f"file://{source}/"))  # kept: nothing fetched again
            self.assertEqual(len(steps), len(pairing.DDI_FILES))
            # A file that isn't what it should be is never kept.
            pairing.DDI_SHA256["Image.dmg"] = "0" * 64
            os.remove(os.path.join(self.tmp.name, "handset", "ddi", "Image.dmg"))
            self.assertIn("wasn't the file", fetch_ddi(steps.append, f"file://{source}/"))
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "handset", "ddi", "Image.dmg")))
        finally:
            pairing.DDI_SHA256.clear()
            pairing.DDI_SHA256.update(saved)


class IPhoneStepTest(Temporary):
    def run_step(self, step, extra=()):
        events = []
        IPhoneStep(os.path.join(HERE, "fakeios.py"), "UDID-1", step, events.append, list(extra)).start()
        self.assertTrue(spin(lambda: events and events[-1]["event"] in ("done", "error"), 10))
        return events

    def test_a_step_waits_for_the_person_then_is_done(self):
        events = self.run_step("trust")
        self.assertEqual([e["event"] for e in events], ["waiting", "done"])

    def test_a_step_refused(self):
        with open(os.path.join(self.tmp.name, "refuse"), "w") as file:
            file.write("developer-mode")
        self.assertEqual(self.run_step("developer-mode")[-1]["event"], "error")

    def test_wifi_writes_its_pairing(self):
        out = os.path.join(self.tmp.name, "pairings", "UDID-1.rppairing")
        os.makedirs(os.path.dirname(out))
        self.run_step("wifi", ["--out", out])
        self.assertTrue(os.path.exists(out))


class SignInTest(Temporary):
    def test_start_at_sign_in(self):
        self.assertFalse(phones.starts_at_sign_in())
        self.assertFalse(phones.start_at_sign_in(False))  # no entry: nothing to switch
        os.makedirs(os.path.dirname(phones.autostart_path()))
        with open(phones.autostart_path(), "w") as file:
            file.write("[Desktop Entry]\nType=Application\nName=Handset phone apps\nExec=handset --sync\n")
        self.assertTrue(phones.starts_at_sign_in())
        self.assertTrue(phones.start_at_sign_in(False))
        self.assertFalse(phones.starts_at_sign_in())
        with open(phones.autostart_path()) as file:
            self.assertIn("Exec=handset --sync", file.read())
        phones.start_at_sign_in(True)
        self.assertTrue(phones.starts_at_sign_in())


if __name__ == "__main__":
    unittest.main()
