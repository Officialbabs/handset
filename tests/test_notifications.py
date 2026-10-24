"""Phone notifications: the Android phone's event log and records, Apple's notification service's
packets, and an Android phone's notifications followed through the stand-in adb."""

import os
import struct
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import ancs
from handset.notifications import AndroidNotifications, parse_event, parse_record, worth_showing

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "0|com.example.chat|7|null|10123"
POSTED = ("1792850002.999  1583  4118 I notification_enqueue: [10123,4118,com.example.chat,7,NULL,0,"
          "Notification(channel=msgs shortcut=null contentView=null vibrate=null flags=0x10 color=0x00000000),0,1]")
UPDATED = POSTED.replace("),0,1]", "),1,1]")
GONE = "1792850009.120  1583  1583 I notification_canceled: [0|com.example.chat|7|null|10123,2,6022,6022,6022,-1,-1,NULL]"
RECORD = """    NotificationRecord(0x0abc: pkg=com.example.chat user=UserHandle{0} id=7 tag=null importance=4 key=0|com.example.chat|7|null|10123: Notification(channel=msgs))
      uid=10123 userId=0
      flags=0x10
      originalFlags=0x10
      key=0|com.example.chat|7|null|10123
      notification=
            extras={
                android.title=String (Ada)
                android.text=SpannableString (See you at 6)
                android.bigText=String (See you at 6
and bring the charger)
                android.subText=null
            }
      mIntercept=false
      mUpdateTimeMs=4102444800000
"""


def spin(until, seconds=10.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.01)


class AndroidWordsTest(unittest.TestCase):
    def test_events(self):
        self.assertEqual(parse_event(POSTED), (1792850002.999, "posted", KEY, 0))
        self.assertEqual(parse_event(UPDATED)[3], 1)
        self.assertEqual(parse_event(GONE), (1792850009.12, "gone", KEY, 0))
        self.assertIsNone(parse_event("--------- beginning of events"))

    def test_a_record(self):
        record = parse_record(RECORD)
        self.assertEqual((record["pkg"], record["flags"], record["importance"], record["intercepted"]),
                         ("com.example.chat", 0x10, 4, False))
        self.assertEqual(record["extras"]["android.title"], "Ada")
        self.assertEqual(record["extras"]["android.text"], "See you at 6")
        self.assertEqual(record["extras"]["android.bigText"], "See you at 6\nand bring the charger")
        self.assertTrue(worth_showing(record))

    def test_the_ones_the_desk_doesnt_show(self):
        for change in (("flags=0x10", "flags=0x12"),  # ongoing (music, a download)
                       ("flags=0x10", "flags=0x210"),  # a group's summary
                       ("importance=4", "importance=2"),  # silent
                       ("mIntercept=false", "mIntercept=true")):  # Do Not Disturb holds it
            self.assertFalse(worth_showing(parse_record(RECORD.replace(*change))), change)


class AncsPacketsTest(unittest.TestCase):
    def test_app_names_without_invisible_marks(self):
        self.assertEqual(ancs.plain_name("\u200eWhatsApp"), "WhatsApp")
        self.assertEqual(ancs.plain_name(" Snapchat\u200f "), "Snapchat")
        self.assertEqual(ancs.plain_name(""), "")

    def test_the_source(self):
        self.assertEqual(ancs.parse_source(struct.pack("<BBBBI", 0, 0x12, 4, 1, 77)), (0, 0x12, 4, 77))
        self.assertIsNone(ancs.parse_source(b"\0\0"))

    def test_asking_and_a_reply_in_pieces(self):
        asked = ancs.ask_notification(77, ancs.POSITIVE)
        self.assertEqual(asked[:5], struct.pack("<BI", 0, 77))
        self.assertIn(bytes([ancs.POSITIVE_LABEL]), asked[5:])
        reply = struct.pack("<BI", 0, 77)
        for attribute, text in ((0, "net.whatsapp.WhatsApp"), (1, "Ada"), (2, ""), (3, "See you"), (6, "Reply")):
            reply += struct.pack("<BH", attribute, len(text.encode())) + text.encode()
        self.assertIsNone(ancs.parse_reply(reply[:20], 5))  # still coming
        self.assertEqual(ancs.parse_reply(reply, 5),
                         (0, 77, {0: "net.whatsapp.WhatsApp", 1: "Ada", 2: "", 3: "See you", 6: "Reply"}))

    def test_an_apps_name(self):
        self.assertEqual(ancs.ask_app("com.apple.MobileSMS"), b"\x01com.apple.MobileSMS\x00\x00")
        reply = b"\x01com.apple.MobileSMS\x00" + struct.pack("<BH", 0, 8) + b"Messages"
        self.assertEqual(ancs.parse_reply(reply, 1), (1, "com.apple.MobileSMS", {0: "Messages"}))
        self.assertEqual(ancs.perform(77, False), struct.pack("<BIB", 2, 77, 1))


class AndroidFollowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.get(key) for key in ("HANDSET_ADB", "FAKEADB_DIR")}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["FAKEADB_DIR"] = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, "records"))
        with open(os.path.join(self.tmp.name, "records", KEY.replace("|", "_")), "w") as file:
            file.write(RECORD)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def event(self, line: str):
        stamp = f"{time.time():.3f}"
        with open(os.path.join(self.tmp.name, "events"), "a") as file:
            file.write(stamp + line[line.index(" "):] + "\n")

    def test_posted_updated_and_gone(self):
        notes, gone = [], []
        follower = AndroidNotifications("FAKE7", "FAKE7", notes.append, lambda phone, key: gone.append(key))
        with open(os.path.join(self.tmp.name, "events"), "w") as file:
            file.write(POSTED + "\n")  # from before: not shown
        follower.start()
        try:
            time.sleep(1.5)
            self.event(POSTED)
            spin(lambda: notes)
            self.assertEqual((notes[0].app, notes[0].title, notes[0].text),
                             ("com.example.chat", "Ada", "See you at 6\nand bring the charger"))
            self.event(UPDATED)  # an update that says the same: not again
            time.sleep(1)
            self.assertEqual(len(notes), 1)
            self.event(GONE)
            spin(lambda: gone)
            self.assertEqual(gone, [KEY])
        finally:
            follower.stop()


if __name__ == "__main__":
    unittest.main()
