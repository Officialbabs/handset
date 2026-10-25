"""What an Android phone plays, as Handset reads it over adb: the media sessions' dump, the song
Bluetooth devices are given, which session the desk shows, and the stand-in adb (fakeadb.py)
playing a phone whose sessions change."""

import base64
import json
import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import androidmedia
from handset.androidmedia import (MediaFollower, choose, helped, now_playing, parse_sessions, parse_song,
                                  split_description)

HERE = os.path.dirname(os.path.abspath(__file__))
MUSIC = "org.example.music"


def dump(state=3, name="PLAYING", position=30000, updated=1000000, actions=0x237, description="A Song, A Singer, An Album"):
    """`dumpsys media_session`'s lines as the phone's loop keeps them (grep'd), one music app's session."""
    return f"""Global priority session is com.android.server.telecom/HeadsetMediaButton/1 (userId=0)
  HeadsetMediaButton com.android.server.telecom/HeadsetMediaButton/1 (userId=0)
    ownerPid=1583, ownerUid=1000, userId=0
    package=com.android.server.telecom
    state=null
    metadata: null
  Media button session is {MUSIC}/Music player/4 (userId=0)
    Music player {MUSIC}/Music player/4 (userId=0)
      ownerPid=31728, ownerUid=10219, userId=0
      package={MUSIC}
      state=PlaybackState {{state={name}({state}), position={position}, buffered position=0, speed=1.0, updated={updated}, actions={actions}, custom actions=[], active item id=1, error=null}}
      metadata: size=9, description={description}
"""


AVRCP = """Profile: AvrcpTargetService
  AVRCP version: 1.6
  List of MediaControllers: size=2
  <Active>   Media Player 1: org.example.music
    MediaController (org.example.music@67397d5) A Song, A Singer, An Album
    Current Data:
      Song: { mediaId="currsong" title="A Song" artist="A Singer" album="An Album" genre="" duration=195000 trackPosition=1/1 image=<Image source=2> }
      PlayState: PlaybackState {state=PLAYING(3), position=30000, buffered position=0, speed=1.0, updated=1000000, actions=567, active item id=1, error=null}
"""


class ParseTest(unittest.TestCase):
    def test_sessions(self):
        sessions, button = parse_sessions(dump())
        self.assertEqual(button, (MUSIC, "Music player", "4"))
        music = sessions[-1]
        self.assertEqual((music.package, music.tag, music.state, music.position, music.updated),
                         (MUSIC, "Music player", 3, 30000, 1000000))
        self.assertEqual(music.description, "A Song, A Singer, An Album")
        self.assertEqual(sessions[0].description, "")  # the calls' session, with nothing

    def test_descriptions(self):
        self.assertEqual(split_description("A Song, A Singer, An Album"), ("A Song", "A Singer", "An Album"))
        self.assertEqual(split_description("A Song, One, Two, An Album"), ("A Song", "One, Two", "An Album"))
        self.assertEqual(split_description("A Song, A Singer, null"), ("A Song", "A Singer", ""))
        self.assertEqual(split_description("A Song"), ("A Song", "", ""))

    def test_the_song_bluetooth_is_given(self):
        package, song = parse_song(AVRCP)
        self.assertEqual(package, MUSIC)
        self.assertEqual(song, {"title": "A Song", "artist": "A Singer", "album": "An Album", "duration": 195.0})
        self.assertIsNone(parse_song("Profile: AvrcpTargetService\n  List of MediaControllers: size=0\n"))

    def test_which_the_desk_shows(self):
        uptime = 1_005_000
        sessions, button = parse_sessions(dump())
        self.assertEqual(choose(sessions, button, uptime).package, MUSIC)
        sessions, button = parse_sessions(dump(state=2, name="PAUSED"))
        self.assertIsNotNone(choose(sessions, button, uptime))  # paused a moment ago
        self.assertIsNone(choose(sessions, button, 1_000_000 + androidmedia.PAUSED_FOR_S * 1000 + 1))
        sessions, button = parse_sessions(dump(state=1, name="STOPPED"))
        self.assertIsNone(choose(sessions, button, uptime))  # stopped: off the desk

    def test_now_playing(self):
        sessions, _button = parse_sessions(dump())
        now = now_playing(sessions[-1], 1_005_000, time.monotonic(), None)
        self.assertEqual((now.app, now.title, now.artist, now.album), (MUSIC, "A Song", "A Singer", "An Album"))
        self.assertTrue(now.playing)
        self.assertAlmostEqual(now.now(), 35.0, delta=0.2)  # 30 s in, 5 s before it was read
        self.assertEqual(now.duration, 0.0)
        self.assertTrue(now.can_next and now.can_previous)  # actions 0x237: both skips
        with_song = now_playing(sessions[-1], 1_005_000, time.monotonic(), parse_song(AVRCP)[1])
        self.assertEqual(with_song.duration, 195.0)
        sessions, _button = parse_sessions(dump(state=2, name="PAUSED", actions=0x207))
        paused = now_playing(sessions[-1], 1_005_000, time.monotonic(), None)
        self.assertEqual(paused.now(), 30.0)  # paused: where it stopped
        self.assertFalse(paused.can_next)


class HelperWordsTest(unittest.TestCase):
    def test_what_the_helper_says(self):
        event = {"event": "media", "package": MUSIC, "title": "A Song, Two", "artist": "A Singer",
                 "album": "An Album", "duration": 195000, "state": 3, "position_now": 30000, "speed": 1.0,
                 "actions": 0x337, "cover_id": "abc"}
        now = helped(event, time.monotonic(), "/covers/abc.jpg")
        self.assertEqual((now.title, now.artist, now.duration, now.art), ("A Song, Two", "A Singer", 195.0,
                                                                         "/covers/abc.jpg"))
        self.assertTrue(now.playing and now.can_seek and now.can_next)
        self.assertAlmostEqual(now.now(), 30.0, delta=0.2)
        self.assertFalse(helped(dict(event, actions=0x37), time.monotonic()).can_seek)  # no SEEK_TO
        self.assertIsNone(helped({"event": "none"}, time.monotonic()))


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.get(key) for key in ("HANDSET_ADB", "FAKEADB_DIR")}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["FAKEADB_DIR"] = self.tmp.name
        self.shown = []
        self.follower = MediaFollower("FAKE7", self.shown.append)

    def tearDown(self):
        self.follower.stop()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.tmp.name, name + ".new"), "w") as file:
            file.write(text)
        os.replace(os.path.join(self.tmp.name, name + ".new"), os.path.join(self.tmp.name, name))

    def wait(self, until, timeout=10.0):
        context = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.01)
        return until()

    def asked(self):
        try:
            with open(os.path.join(self.tmp.name, "log.jsonl")) as file:
                return [json.loads(line) for line in file]
        except OSError:
            return []

    def test_through_the_helper_its_covers_and_seeking(self):
        saved = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.tmp.name
        try:
            cover = base64.b64encode(b"\xff\xd8a jpeg").decode()
            self.write("media_helper.jsonl", json.dumps({
                "event": "media", "package": MUSIC, "title": "A Song", "artist": "A Singer", "album": "An Album",
                "duration": 195000, "state": 3, "position_now": 1000, "speed": 1.0, "actions": 0x337,
                "cover_id": "c1", "cover": cover}) + "\n")
            self.follower.start()
            self.assertTrue(self.wait(lambda: self.shown and self.shown[-1] is not None))
            now = self.shown[-1]
            self.assertTrue(now.can_seek)
            with open(now.art, "rb") as file:
                self.assertEqual(file.read(), b"\xff\xd8a jpeg")
            self.follower.control("seek", 42.5)
            commands = os.path.join(self.tmp.name, "helper_commands.jsonl")
            self.assertTrue(self.wait(lambda: os.path.exists(commands) and "42500" in open(commands).read()))
            with open(commands) as file:
                self.assertEqual(json.loads(file.read().splitlines()[-1]), {"op": "seek", "position": 42500})
        finally:
            if saved is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = saved

    def test_follows_and_works_the_phone(self):
        self.write("avrcp", AVRCP)
        self.write("media_session", dump())
        self.follower.start()
        self.assertTrue(self.wait(lambda: self.shown and self.shown[-1] is not None))
        self.assertEqual((self.shown[-1].title, self.shown[-1].duration), ("A Song", 195.0))
        self.follower.control("toggle")  # playing: a pause
        self.assertTrue(self.wait(lambda: ["shell", "cmd media_session dispatch pause"] in self.asked()))
        self.write("media_session", dump(state=1, name="STOPPED"))
        self.assertTrue(self.wait(lambda: self.shown[-1] is None))


if __name__ == "__main__":
    unittest.main()
