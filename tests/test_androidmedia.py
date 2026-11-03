"""What an Android phone plays, as Handset reads it over adb: the media sessions' dump, the song
Bluetooth devices are given, which session the desk shows, and the stand-in adb (fakeadb.py)
playing a phone whose sessions change."""

import base64
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile

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

    def test_the_helper_leaves_a_long_paused_session_off_too(self):
        """The helper (android/HandsetMedia.java, data/handset-media.jar built from it) chooses a
        paused session only while it's been paused less than the shell's view keeps one
        (PAUSED_FOR_S): an app leaving its session paused doesn't stay on the desk for good."""
        with open(os.path.join(os.path.dirname(HERE), "android", "HandsetMedia.java")) as file:
            source = file.read()
        limit = re.search(r"long PAUSED_FOR_MS = ([\d *]+);", source)
        self.assertIsNotNone(limit, "the helper keeps paused sessions with no limit")
        self.assertEqual(math.prod(int(part) for part in limit[1].split("*")), androidmedia.PAUSED_FOR_S * 1000)
        self.assertIn('getLastPositionUpdateTime") < PAUSED_FOR_MS', source)  # what a paused one's chosen by
        # One left off still looked at every POLL_MS where sessions don't say what changes, and
        # worked all the same with none shown (auto-pause playing it again, paused longer than that).
        self.assertIn("((current == null && leftOff == null) || !heard.isEmpty())", source)
        self.assertIn("Object controller = current != null ? current : leftOff;", source)
        # Left off: the one paused too long, if it's the one shown till then (auto-pause's, not
        # another app's paused long ago), and said so.
        self.assertIn("leftOff = chosen == null ? left : null;", source)
        self.assertRegex(source, r'else if \(left == null && shown != null && shown\.equals\(call\(controller, '
                                 r'"asBinder"\)\)\) \{(\s*//[^\n]*)*\s*left = controller;')
        self.assertRegex(source, r'if \(chosen != null\) \{\s*shown = call\(chosen, "asBinder"\);')
        self.assertIn(r'leftOff == null ? "{\"event\":\"none\"}" : "{\"event\":\"none\",\"left_off\":true}"', source)
        with zipfile.ZipFile(androidmedia.HELPER) as jar:  # what the phone runs: built from that source
            dex = jar.read("classes.dex")
        for name in (b"PAUSED_FOR_MS", b"leftOff", b"shown", b"left_off"):
            self.assertTrue(name in dex, f"data/handset-media.jar isn't rebuilt from it (no {name.decode()})")


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

    def test_a_session_left_paused_goes(self):
        """A session left paused, its lines unchanged on the shell's view, goes from the desk once
        it's been paused PAUSED_FOR_S: the phone's loop says them again now and then."""
        saved = androidmedia.PAUSED_FOR_S
        androidmedia.PAUSED_FOR_S = 1
        self.addCleanup(lambda: setattr(androidmedia, "PAUSED_FOR_S", saved))
        self.write("media_session", dump())
        self.follower.start()
        self.assertTrue(self.wait(lambda: self.shown and self.shown[-1] is not None))
        # Paused now (fakeadb's uptime is this clock).
        self.write("media_session", dump(state=2, name="PAUSED", updated=round(time.monotonic() * 1000)))
        self.assertTrue(self.wait(lambda: self.shown[-1] is not None and not self.shown[-1].playing))
        self.assertTrue(self.wait(lambda: self.shown[-1] is None, 5))

    def test_left_off_only_the_session_it_showed(self):
        """Showing nothing on the shell's view, the phone's session is left off only for its age
        (auto-pause's play plays it again) while it's the one shown last, paused still: not
        another app's paused long ago, nor the one shown once it's stopped."""
        saved = androidmedia.PAUSED_FOR_S
        androidmedia.PAUSED_FOR_S = 1
        self.addCleanup(lambda: setattr(androidmedia, "PAUSED_FOR_S", saved))
        self.write("media_session", dump())
        self.follower.start()
        self.assertTrue(self.wait(lambda: self.shown and self.shown[-1] is not None))
        self.write("media_session", dump(state=2, name="PAUSED", updated=round(time.monotonic() * 1000)))
        self.assertTrue(self.wait(lambda: self.shown[-1] is None, 5))
        self.assertTrue(self.follower.left_off)
        long_ago = round((time.monotonic() - 60) * 1000)
        self.write("media_session", dump(state=2, name="PAUSED", updated=long_ago).replace(MUSIC, "org.example.talk"))
        self.assertTrue(self.wait(lambda: not self.follower.left_off))  # another app's
        self.write("media_session", dump(state=2, name="PAUSED", updated=long_ago))
        self.assertTrue(self.wait(lambda: self.follower.left_off))
        self.write("media_session", dump(state=1, name="STOPPED"))
        self.assertTrue(self.wait(lambda: not self.follower.left_off))  # stopped
        self.assertEqual(self.shown[-1], None)

    def test_left_off_as_the_helper_says(self):
        """Through the helper, which knows which session it showed: left off only when it says so."""
        self.write("media_helper.jsonl", json.dumps({"event": "none", "left_off": True}) + "\n")
        self.follower.start()
        self.assertTrue(self.wait(lambda: self.follower.left_off))
        self.write("media_helper.jsonl", json.dumps({"event": "none"}) + "\n")
        self.assertTrue(self.wait(lambda: not self.follower.left_off))

    def test_the_phones_loop_says_the_sessions_again_anyway(self):
        """The loop on the phone (WATCH) says the sessions' lines again though they're unchanged:
        run here, a stand-in for dumpsys giving them, its looks quick."""
        tools = os.path.join(self.tmp.name, "bin")
        os.mkdir(tools)
        self.write("media_session", dump(state=2, name="PAUSED"))
        with open(os.path.join(tools, "dumpsys"), "w") as file:
            file.write(f"#!/bin/sh\ncat '{os.path.join(self.tmp.name, 'media_session')}'\n")
        os.chmod(os.path.join(tools, "dumpsys"), 0o755)
        watch = re.sub(r"sleep \d+", "sleep 0.01", androidmedia.WATCH)
        process = subprocess.Popen(["sh", "-c", watch], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                   env={**os.environ, "PATH": tools + os.pathsep + os.environ.get("PATH", "")})
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        stopping = threading.Timer(10, process.kill)  # one never said again: its output ends
        stopping.start()
        self.addCleanup(stopping.cancel)
        blocks, block = [], []
        with process.stdout:
            for line in process.stdout:
                if line.strip() == "--":
                    blocks.append(block[1:])  # its uptime first
                    block = []
                    if len(blocks) == 2:
                        break
                else:
                    block.append(line.rstrip("\n"))
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], blocks[1])
        self.assertIn(f"      package={MUSIC}", blocks[0])


if __name__ == "__main__":
    unittest.main()
