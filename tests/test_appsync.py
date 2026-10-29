"""The connected phones' apps among the desk's apps, kept by the sync: with the stand-ins for Ada's
service and adb, in folders of the test's own."""

import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from gi.repository import GLib

from handset import android, apps, appsync
from tests.fakeservice import FakeService, phone

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "ADA_PHONES_SOCKET",
            "HANDSET_ADB", "FAKEADB_DIR", "HANDSET_LAUNCHER")
# A stand-in for `handset --sync-icons`: an icon for every package it's given, and a note of them.
WORKER = """#!/usr/bin/env python3
import os, sys
serial, folder = sys.argv[2], sys.argv[3]
asked = [line.strip() for line in sys.stdin if line.strip()]
with open(os.path.join(os.environ["FAKEADB_DIR"], "asked.txt"), "a") as file:
    file.write(" ".join(asked) + "\\n")
for package in asked:
    with open(os.path.join(folder, package + ".png"), "wb") as file:
        file.write(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                                 "0000000d49444154789c63f8cfc0f01f0005000201a5f1b6e80000000049454e44ae426082"))
    print(package, flush=True)
"""


def spin(until, seconds=15.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.01)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.get(key) for key in SETTINGS}
        for key, folder in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                            ("XDG_RUNTIME_DIR", "run"), ("FAKEADB_DIR", "adb")):
            os.environ[key] = os.path.join(self.tmp.name, folder)
            os.makedirs(os.environ[key])
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["ADA_PHONES_SOCKET"] = os.path.join(self.tmp.name, "phones.sock")
        os.environ["HANDSET_LAUNCHER"] = os.path.join(self.tmp.name, "handset")
        with open(os.environ["HANDSET_LAUNCHER"], "w") as file:
            file.write(WORKER)
        os.chmod(os.environ["HANDSET_LAUNCHER"], 0o755)
        os.makedirs(os.path.dirname(apps.dock_path()))
        with open(apps.dock_path(), "w") as file:
            file.write("[Dock]\nPinned=ada-files.desktop;\n")
        os.makedirs(os.path.join(os.environ["XDG_CONFIG_HOME"], "handset"))
        with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "handset", "settings.json"), "w") as file:
            file.write('{"phone_notifications": false}')  # the real desk's and Bluetooth's: not here
        self.version = android.scrcpy_server
        android.scrcpy_server = lambda: (__file__, "3.3.4")  # any file: the stand-in adb takes it
        self.service = FakeService(os.environ["ADA_PHONES_SOCKET"],
                                   [phone(id="FAKE7", name="Pixel 7", usb=True, status="connected")])

    def tearDown(self):
        self.service.close()
        android.scrcpy_server = self.version
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def names(self):
        return sorted(app.name for app in apps.entries())

    def test_a_connected_phones_apps_come_and_go(self):
        sync = appsync.AppSync()
        sync.start()
        spin(lambda: len(apps.entries()) == 3)
        self.assertEqual(self.names(), ["A Very Long Application Name Indeed", "Fake Chat", "Settings"])
        chat = apps.find("FAKE7", "org.example.chat")
        self.assertEqual(chat.title, "Fake Chat (Pixel 7)")
        apps.pin(chat, None)
        spin(lambda: not sync.busy)
        # The phone goes: its apps stay for now — away a night, not gone.
        self.service.push([phone(id="FAKE7", name="Pixel 7", usb=False, status="offline")])
        spin(lambda: not sync.online)
        self.assertEqual(len(apps.entries()), 3)
        # Gone for good a day later: its apps go too, but for the one in the dock.
        sync.last_seen["FAKE7"] -= appsync.ENTRY_RETENTION_S
        sync._drop_unconnected()
        self.assertEqual(self.names(), ["Fake Chat"])
        # And back.
        self.service.push([phone(id="FAKE7", name="Pixel 7", usb=True, status="connected")])
        spin(lambda: len(apps.entries()) == 3)
        spin(lambda: not sync.busy)  # nothing of this test's left running into the next
        sync.watch.stop()
        sync.stop()

    def test_entries_left_from_before_go_when_their_phone_isnt_here(self):
        apps.write_entry(apps.PhoneApp("GONE1", "Old phone", "android", "org.example.old", "Old"))
        apps.write_entry(apps.PhoneApp("GONE2", "Away phone", "android", "org.example.away", "Away"))
        folder = os.path.join(os.environ["XDG_CACHE_HOME"], "handset")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "last_seen.json"), "w") as file:
            file.write('{"GONE1": 1}')  # long gone; GONE2 it never saw, so that one counts from now
        sync = appsync.AppSync()
        sync.start()
        spin(lambda: len(apps.entries()) == 4 and not sync.busy)
        self.assertNotIn("Old", self.names())
        self.assertIn("Away", self.names())
        spin(lambda: not sync.busy)  # nothing of this test's left running into the next
        sync.watch.stop()
        sync.stop()

    def asked(self):
        path = os.path.join(os.environ["FAKEADB_DIR"], "asked.txt")
        if not os.path.exists(path):
            return []
        with open(path) as file:
            return [line.split() for line in file]

    def test_icons_are_kept_and_read_again_only_when_an_app_changes(self):
        sync = appsync.AppSync()
        sync.start()
        spin(lambda: len(self.asked()) == 1 and not sync.busy)
        self.assertEqual(sorted(self.asked()[0]), ["com.android.settings", "org.example.chat", "org.example.long"])
        chat = apps.find("FAKE7", "org.example.chat")
        icon = os.path.basename(apps.icon_files("FAKE7", "org.example.chat")[0])
        self.assertTrue(icon.startswith(chat.id + "-"))  # its own icon now, not the letter
        # Back after a while, with Fake Chat updated: only its icon is read again, and the
        # entries never left while the phone was away.
        with open(os.path.join(os.environ["FAKEADB_DIR"], "versions.json"), "w") as file:
            file.write('{"org.example.chat": 2}')
        self.service.push([phone(id="FAKE7", name="Pixel 7", usb=False, status="offline")])
        spin(lambda: not sync.online)
        self.assertEqual(len(apps.entries()), 3)
        self.service.push([phone(id="FAKE7", name="Pixel 7", usb=True, status="connected")])
        spin(lambda: len(self.asked()) == 2 and not sync.busy)
        self.assertEqual(self.asked()[1], ["org.example.chat"])
        spin(lambda: not sync.busy)  # nothing of this test's left running into the next
        sync.watch.stop()
        sync.stop()

    def test_listed_again_only_when_its_packages_changed(self):
        def listings():
            path = os.path.join(os.environ["FAKEADB_DIR"], "log.jsonl")
            with open(path) as file:
                return [line for line in file if "list_apps=true" in line]

        sync = appsync.AppSync()
        sync.start()
        spin(lambda: len(apps.entries()) == 3 and not sync.busy)
        self.assertEqual(len(listings()), 1)
        for change in (None, '{"org.example.chat": 2}'):  # due again: the same packages, then one updated
            if change:
                with open(os.path.join(os.environ["FAKEADB_DIR"], "versions.json"), "w") as file:
                    file.write(change)
            sync.listed["FAKE7"] -= appsync.RELIST_S
            sync._relist()
            spin(lambda: not sync.busy)
        self.assertEqual(len(listings()), 2)
        sync.watch.stop()
        sync.stop()

    def test_one_sync_at_a_time(self):
        held = appsync.take_lock()
        self.assertIsNotNone(held)
        self.assertTrue(appsync.running())
        self.assertIsNone(appsync.take_lock())
        held.close()
        self.assertFalse(appsync.running())


if __name__ == "__main__":
    unittest.main()


class IPhoneNotificationsTest(unittest.TestCase):
    """An iPhone's notifications follow whether it's connected (by cable or Wi-Fi): on at once,
    off only after it's been gone a while."""

    def test_on_at_once_off_after_a_while(self):
        from types import SimpleNamespace

        from handset import appsync

        sync = appsync.AppSync.__new__(appsync.AppSync)
        sync.away_timer = 0
        asked = []
        sync.ancs = SimpleNamespace(allowed=False, allow=lambda on: (asked.append(on), setattr(sync.ancs, "allowed", on)))
        saved = appsync.AWAY_FOR_S
        appsync.AWAY_FOR_S = 1
        try:
            sync._iphones_here(True)
            self.assertEqual(asked, [True])
            sync._iphones_here(False)
            self.assertEqual(asked, [True])  # not yet
            sync._iphones_here(True)  # back within the while: never off
            context = GLib.MainContext.default()
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                context.iteration(False)
                time.sleep(0.01)
            self.assertEqual(asked, [True, True])
            sync._iphones_here(False)
            end = time.monotonic() + 2.5  # whole seconds, on GLib's coarse clock
            while time.monotonic() < end:
                context.iteration(False)
                time.sleep(0.01)
            self.assertEqual(asked[-1], False)
        finally:
            appsync.AWAY_FOR_S = saved


class Folder:
    """A phone's folder here, as far as the sync sees it."""

    def __init__(self):
        self.stopped = threading.Event()

    def stop(self):
        self.stopped.set()


class FolderGraceTest(unittest.TestCase):
    """A phone's folder outlasts a moment out of reach, and goes when the phone stays gone."""

    def setUp(self):
        saved = appsync.FOLDER_STAYS_S, appsync.phone_files_wanted
        self.addCleanup(lambda: setattr(appsync, "FOLDER_STAYS_S", saved[0]))
        self.addCleanup(lambda: setattr(appsync, "phone_files_wanted", saved[1]))
        appsync.FOLDER_STAYS_S = 1
        self.switched_on = True
        appsync.phone_files_wanted = lambda: self.switched_on
        self.sync = appsync.AppSync.__new__(appsync.AppSync)
        self.sync.mounting, self.sync.leaving = set(), {}
        self.asked = []
        self.sync._mount = self.asked.append
        self.folder = Folder()
        self.sync.mounts, self.sync.mounted_at = {"P": self.folder}, {"P": "192.168.18.10"}

    def changed(self, online: bool, address: str = "192.168.18.10"):
        phones = [SimpleNamespace(id="P", online=online, problem=None, address=address)]
        self.sync.phones_now = phones
        self.sync._mount_phones(phones)

    def test_a_moment_out_of_reach_keeps_it(self):
        self.changed(False)
        self.changed(True)
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
        self.assertFalse(self.folder.stopped.is_set())
        self.assertIn("P", self.sync.mounts)
        self.assertEqual(self.asked, [])  # not mounted again

    def test_gone_for_good_takes_it_away(self):
        self.changed(False)
        self.assertFalse(self.folder.stopped.is_set())  # not at once
        spin(self.folder.stopped.is_set, seconds=5)
        self.assertNotIn("P", self.sync.mounts)

    def test_back_somewhere_else_it_follows(self):
        self.changed(False)
        self.changed(True, address="192.168.18.44")
        spin(lambda: self.folder.stopped.is_set() and self.asked, seconds=5)
        self.assertEqual([p.address for p in self.asked], ["192.168.18.44"])

    def test_switched_off_at_once(self):
        self.switched_on = False
        self.changed(True)
        spin(self.folder.stopped.is_set, seconds=2)
