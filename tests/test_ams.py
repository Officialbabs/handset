"""The iPhone's now playing over Bluetooth (Apple Media Service), with a stand-in for BlueZ: what
its updates mean, and the writes that ask for them and work its player."""

import time
import unittest

from gi.repository import GLib

from handset import ams
from handset.ams import Ams, Writes, parse_entity, parse_playback

CHARS = {ams.REMOTE_COMMAND: "/dev/svc/command", ams.ENTITY_UPDATE: "/dev/svc/update",
         ams.ENTITY_ATTRIBUTE: "/dev/svc/attribute"}


class FakeBus:
    """BlueZ as the client sees it: each call answered at once (or held, to be answered later)."""

    def __init__(self):
        self.calls = []
        self.held = []
        self.hold = False
        self.reads = {}

    def call(self, _name, path, _interface, method, args, _reply_type, _flags, _timeout, _cancel, callback,
             data=None):
        self.calls.append((path, method, bytes(args.unpack()[0]) if method == "WriteValue" else None))
        answer = (callback, data, path, method)
        if self.hold:
            self.held.append(answer)
        elif callback:
            callback(self, answer, data)

    def answer(self):
        callback, data, path, method = self.held.pop(0)
        callback(self, (callback, data, path, method), data)

    def call_finish(self, result):
        _callback, _data, path, method = result
        if method == "ReadValue":
            return GLib.Variant("(ay)", (self.reads.get(path, b""),))
        return None

    def writes(self, path):
        return [value for p, method, value in self.calls if p == path and method == "WriteValue"]


def update(entity, attribute, text, flags=0):
    return bytes([entity, attribute, flags]) + text.encode()


def settle():
    context = GLib.MainContext.default()
    end = time.monotonic() + (ams.SETTLE_MS + 200) / 1000
    while time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)


class PacketsTest(unittest.TestCase):
    def test_updates_and_playback(self):
        self.assertEqual(parse_entity(update(2, 2, "A Song")), (2, 2, False, "A Song"))
        self.assertEqual(parse_entity(update(2, 0, "A Sing", ams.TRUNCATED)), (2, 0, True, "A Sing"))
        self.assertIsNone(parse_entity(b"\x02\x00"))
        self.assertEqual(parse_playback("1,1.0,42.317"), (1, 1.0, 42.317))
        self.assertEqual(parse_playback("0,0.0,0"), (0, 0.0, 0.0))
        self.assertIsNone(parse_playback(""))  # nothing playing: empty
        self.assertIsNone(parse_playback("1,x,2"))


class AmsTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.shown = []
        self.media = Ams(self.bus, Writes(self.bus), self.shown.append,
                         lambda name: {"Music": "com.apple.Music"}.get(name, ""))

    def test_asks_for_updates_once_they_are_on(self):
        self.bus.hold = True
        self.media.attach(CHARS)
        self.assertEqual([m for _p, m, _v in self.bus.calls], ["StartNotify", "StartNotify"])
        self.bus.answer()  # the commands' updates on
        self.assertEqual(self.bus.writes(CHARS[ams.ENTITY_UPDATE]), [])
        self.bus.answer()  # the entities' updates on: only now does the iPhone take what's asked
        self.assertEqual(self.bus.writes(CHARS[ams.ENTITY_UPDATE]), [bytes([0, 0, 1])])
        self.bus.answer()  # the player's asked for: then the track's, one write at a time
        self.assertEqual(self.bus.writes(CHARS[ams.ENTITY_UPDATE]), [bytes([0, 0, 1]), bytes([2, 0, 1, 2, 3])])

    def test_a_song_playing_and_paused(self):
        self.media.attach(CHARS)
        path = CHARS[ams.ENTITY_UPDATE]
        for packet in (update(0, 0, "Music"), update(0, 1, "1,1.0,42.5"), update(2, 0, "A Singer"),
                       update(2, 1, "An Album"), update(2, 2, "A Song"), update(2, 3, "215.3")):
            self.media.changed(path, packet)
        settle()
        self.assertEqual(len(self.shown), 1)  # its pieces shown together
        now = self.shown[-1]
        self.assertEqual((now.app, now.title, now.artist, now.album, now.duration),
                         ("com.apple.Music", "A Song", "A Singer", "An Album", 215.3))
        self.assertTrue(now.playing)
        self.assertGreaterEqual(now.now(), 42.5)
        self.assertFalse(now.can_seek)  # the service has no place to go to
        self.media.changed(path, update(0, 1, "0,0.0,50.0"))
        settle()
        self.assertFalse(self.shown[-1].playing)
        self.assertEqual(self.shown[-1].now(), 50.0)
        self.media.changed(path, update(2, 2, ""))  # nothing to play any more
        settle()
        self.assertIsNone(self.shown[-1])

    def test_what_the_player_takes(self):
        self.media.attach(CHARS)
        path = CHARS[ams.ENTITY_UPDATE]
        self.media.changed(path, update(2, 2, "A Song"))
        self.media.changed(CHARS[ams.REMOTE_COMMAND], bytes([0, 1, 2]))  # no next or previous now
        settle()
        self.assertFalse(self.shown[-1].can_next)
        self.assertFalse(self.shown[-1].can_previous)
        self.media.control("toggle")
        self.media.control("next")
        self.assertEqual(self.bus.writes(CHARS[ams.REMOTE_COMMAND]), [bytes([2]), bytes([3])])

    def test_a_long_title_read_whole(self):
        self.media.attach(CHARS)
        self.bus.reads[CHARS[ams.ENTITY_ATTRIBUTE]] = "A Very Long Song Title".encode()
        self.media.changed(CHARS[ams.ENTITY_UPDATE], update(2, 2, "A Very", ams.TRUNCATED))
        self.assertEqual(self.bus.writes(CHARS[ams.ENTITY_ATTRIBUTE]), [bytes([2, 2])])
        settle()
        self.assertEqual(self.shown[-1].title, "A Very Long Song Title")

    def test_gone_with_the_iphone(self):
        self.media.attach(CHARS)
        self.media.changed(CHARS[ams.ENTITY_UPDATE], update(2, 2, "A Song"))
        settle()
        self.media.detach()
        self.assertIsNone(self.shown[-1])


class WritesTest(unittest.TestCase):
    def test_one_at_a_time_for_each(self):
        bus = FakeBus()
        bus.hold = True
        done = []
        writes = Writes(bus)
        writes.write("/a", b"1", done.append)
        writes.write("/a", b"2", done.append)
        writes.write("/b", b"3")
        self.assertEqual([(p, v) for p, _m, v in bus.calls], [("/a", b"1"), ("/b", b"3")])
        bus.answer()  # "/a"'s first done: its second goes
        self.assertEqual(done, [True])
        self.assertEqual(bus.calls[-1][2], b"2")


if __name__ == "__main__":
    unittest.main()
