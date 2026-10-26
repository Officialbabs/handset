"""The phones' files in the desktop's search (phonesearch.py): an Android phone's storage listed in
one go (through the stand-in adb, fakeadb.py), an iPhone's walked (the stand-in helper,
fakeios.py), the names searched as the launcher asks, and a result opened from the phone's folder."""

import os
import tempfile
import time
import unittest
from unittest import mock

from gi.repository import GLib

from handset import phonesearch
from handset.iphone import FilesSession, Link
from handset.phonesearch import Found, PhoneSearch, android_files, iphone_files, parse_find, score

HERE = os.path.dirname(os.path.abspath(__file__))


def spin(until, seconds=8.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    return until()


class ReadingTest(unittest.TestCase):
    def test_androids_listing(self):
        listed = parse_find("d\t0\t1783059702.5\t.\n"
                            "d\t0\t1783059702.5\t./Music\n"
                            "f\t4356069\t1732107822.0\t./Music/Shape of You.opus\n"
                            "f\t12\t1732107822.0\t./Download/a\tname with a tab.txt\n"
                            "not a line\n")
        self.assertEqual(listed, [Found("/Music", True, 0, 1783059702.5),
                                  Found("/Music/Shape of You.opus", False, 4356069, 1732107822.0),
                                  Found("/Download/a\tname with a tab.txt", False, 12, 1732107822.0)])
        self.assertEqual(listed[1].name, "Shape of You.opus")

    def test_how_well_a_name_answers(self):
        self.assertEqual(score(["invoice"], "invoice.pdf"), 3)
        self.assertEqual(score(["invoice"], "invoice march.pdf"), 2)
        self.assertEqual(score(["invoice"], "march_invoice.pdf"), 1)
        self.assertEqual(score(["voice"], "invoice.pdf"), 0)
        self.assertEqual(score(["march", "invoice"], "invoice march.pdf"), 1)
        self.assertIsNone(score(["invoice", "april"], "invoice march.pdf"))  # every word, or none


class Phones(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HANDSET_ADB", "FAKEADB_DIR", "FAKEIOS_DIR", "XDG_CACHE_HOME",
                                                     "XDG_CONFIG_HOME")}
        os.environ.update(HANDSET_ADB=os.path.join(HERE, "fakeadb.py"), FAKEADB_DIR=self.tmp.name,
                          FAKEIOS_DIR=self.tmp.name, XDG_CACHE_HOME=os.path.join(self.tmp.name, "cache"),
                          XDG_CONFIG_HOME=os.path.join(self.tmp.name, "config"))

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def make(self, *paths):
        for path in paths:
            full = os.path.join(self.tmp.name, path)
            if path.endswith("/"):
                os.makedirs(full, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as file:
                    file.write("on the phone")


class ListingTest(Phones):
    def test_an_android_phones_storage(self):
        self.make("sdcard/DCIM/Camera/PXL_1.jpg", "sdcard/Download/Invoice.pdf", "sdcard/.hidden/x",
                  "sdcard/Android/data/app/cache.bin")
        found = {f.path: f for f in android_files("FAKE7")}
        self.assertEqual(sorted(found), ["/DCIM", "/DCIM/Camera", "/DCIM/Camera/PXL_1.jpg", "/Download",
                                         "/Download/Invoice.pdf"])  # not the hidden, nor apps' own
        self.assertTrue(found["/DCIM"].folder)
        self.assertEqual(found["/Download/Invoice.pdf"].size, len("on the phone"))

    def test_an_iphones_media_and_apps(self):
        self.make("media/DCIM/100APPLE/IMG_0001.HEIC", "media/Recordings/Lecture.m4a",
                  "apps/org.videolan.vlc-ios/Films/Holiday.mp4")
        session = FilesSession(Link("UDID-1", None, None, os.path.join(HERE, "fakeios.py")))
        try:
            with mock.patch.object(FilesSession, "sharing", return_value={"org.videolan.vlc-ios": "VLC"}):
                found = {f.path: f for f in iphone_files(session)}
        finally:
            session.stop()
        self.assertEqual(sorted(found), [
            "/DCIM", "/DCIM/100APPLE", "/DCIM/100APPLE/IMG_0001.HEIC", "/On My iPhone/VLC",
            "/On My iPhone/VLC/Films", "/On My iPhone/VLC/Films/Holiday.mp4",  # as its folder here names it
            "/Recordings", "/Recordings/Lecture.m4a"])
        self.assertTrue(found["/On My iPhone/VLC/Films"].folder)


class SearchTest(Phones):
    def setUp(self):
        super().setUp()
        self.listed = {"pixel": [Found("/Download", True), Found("/Download/Invoice.pdf", False, 10, 100.0),
                                 Found("/Documents/old invoice.pdf", False, 10, 50.0),
                                 Found("/DCIM/Camera/PXL_1.jpg", False, 10, 300.0)],
                       "iphone": [Found("/On My iPhone/VLC/Invoice scan.pdf", False, 10, 200.0)]}
        self.here = {"pixel": ("Pixel 7", "/home/me/Phones/Pixel 7"), "iphone": ("My iPhone", "/home/me/Phones/My iPhone")}
        self.search = PhoneSearch(lambda: {phone_id: (name, folder, lambda p=phone_id: self.listed[p])
                                           for phone_id, (name, folder) in self.here.items()})
        for phone_id in self.here:
            self.search.phone_here(phone_id)
        self.assertTrue(spin(lambda: len(self.search.indexes) == 2 and not self.search.indexing))

    def test_names_found_and_shown(self):
        found = self.search.search(["Invoice"])
        self.assertEqual(found, ["pixel\n/Download/Invoice.pdf",  # its name; then newest first
                                 "iphone\n/On My iPhone/VLC/Invoice scan.pdf", "pixel\n/Documents/old invoice.pdf"])
        self.assertEqual(self.search.search(["  "]), [])
        metas = {m["id"].unpack(): {k: v.unpack() for k, v in m.items()}
                 for m in self.search.metas(found + ["pixel\n/nothing", "gone\n/x"])}
        self.assertEqual(len(metas), 3)  # only what's there
        self.assertEqual(metas["iphone\n/On My iPhone/VLC/Invoice scan.pdf"]["description"],
                         "My iPhone › On My iPhone › VLC")
        shown = metas["pixel\n/Download/Invoice.pdf"]
        self.assertEqual((shown["name"], shown["description"]), ("Invoice.pdf", "Pixel 7 › Download"))
        self.assertIn("pdf", shown["gicon"])
        folder = self.search.metas(self.search.search(["download"]))[0]
        self.assertIn("folder", folder["gicon"].unpack())

    def test_a_result_opened_from_the_phones_folder(self):
        with mock.patch.object(phonesearch, "open_here") as opened:
            self.search.activate("pixel\n/Download/Invoice.pdf")
            self.search.activate("pixel\n/etc/passwd")  # never offered: nothing
            self.search.activate("pixel\n/Download")
            self.search.launch_search()
        self.assertEqual([c.args for c in opened.call_args_list], [
            ("/home/me/Phones/Pixel 7/Download/Invoice.pdf", False), ("/home/me/Phones/Pixel 7/Download", True),
            (os.path.join(os.path.expanduser("~"), "Phones") if not os.environ.get("HANDSET_PHONES_FOLDER")
             else os.environ["HANDSET_PHONES_FOLDER"], True)])

    def test_a_phone_away_isnt_searched(self):
        del self.here["iphone"]
        self.assertEqual(self.search.search(["scan"]), [])
        self.assertEqual(self.search.metas(["iphone\n/On My iPhone/VLC/Invoice scan.pdf"]), [])

    def test_the_index_kept_for_next_time(self):
        again = PhoneSearch(lambda: {"pixel": ("Pixel 7", "/x", lambda: None)})  # the phone doesn't answer now
        again.phone_here("pixel")
        self.assertTrue(spin(lambda: not again.indexing))
        self.assertEqual(again.search(["pxl"]), ["pixel\n/DCIM/Camera/PXL_1.jpg"])

    def test_the_phone_asked_again_only_when_searched_and_old(self):
        asked = []
        again = PhoneSearch(lambda: {"pixel": ("Pixel 7", "/x", lambda: asked.append(1) or self.listed["pixel"])})
        again.phone_here("pixel")  # its index from a moment ago: the phone is left alone
        self.assertTrue(spin(lambda: not again.indexing))
        self.assertEqual(asked, [])
        again.indexes["pixel"].made -= phonesearch.INDEX_EVERY_S  # old now
        self.assertEqual(again.search(["pxl"]), ["pixel\n/DCIM/Camera/PXL_1.jpg"])  # answered at once
        self.assertTrue(spin(lambda: asked and not again.indexing))
        self.assertEqual(asked, [1])

    def test_asked_over_the_bus(self):
        answered = []

        class Invocation:
            def return_value(self, value):
                answered.append(value.unpack() if value is not None else None)

        call = lambda method, signature, *args: self.search._on_call(None, ":1.5", phonesearch.PATH, "", method,
                                                                    GLib.Variant(signature, args), Invocation())
        call("GetInitialResultSet", "(as)", ["pxl"])
        call("GetSubsearchResultSet", "(asas)", [], ["pxl", "1"])
        call("GetResultMetas", "(as)", ["pixel\n/DCIM/Camera/PXL_1.jpg"])
        self.assertEqual(answered[0], (["pixel\n/DCIM/Camera/PXL_1.jpg"],))
        self.assertEqual(answered[1], (["pixel\n/DCIM/Camera/PXL_1.jpg"],))
        self.assertEqual(answered[2][0][0]["name"], "PXL_1.jpg")


if __name__ == "__main__":
    unittest.main()
