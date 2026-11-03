"""One thing playing at a time, on a bus of the test's own: a phone starting pauses the other phone
and the computer's player; the computer's player starting pauses the phones. The phones' sound
lowered a moment through a stand-in pactl (fakepactl.py)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from gi.repository import Gio, GLib

from handset import autopause
from handset.autopause import AutoPause, Lowered, phone_streams

HERE = os.path.dirname(os.path.abspath(__file__))

BUS_CONFIG = """<busconfig>
  <type>session</type>
  <listen>unix:dir={folder}</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>"""

DESK_PLAYER = """<node><interface name="org.mpris.MediaPlayer2.Player">
  <method name="Pause"/>
  <method name="Play"/>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Position" type="x" access="read"/>
</interface></node>"""


def spin(until, timeout=3.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    return until()


class Phone:
    def __init__(self, playing):
        self.now = SimpleNamespace(playing=playing, duration=0.0)  # media.NowPlaying: no length said
        self.sent = []

    def control(self, command, place):
        self.sent.append(command)


class NoBus:
    """A bus nothing's heard on: the session's never asked."""

    def signal_subscribe(self, *_args):
        return 0

    def signal_unsubscribe(self, _watch):
        pass


class FakeLowered:
    """The sound server stands in: never the computer's own."""

    def __init__(self):
        self.said = []
        self.owed = {}

    def lower(self):
        self.said.append("lower")

    def restore(self):
        self.said.append("restore")

    def stop(self):
        self.said.append("stop")


class AutoPauseTest(unittest.TestCase):
    def setUp(self):
        self.saved = (autopause.SOUND_S, autopause.BETWEEN_MS)
        autopause.SOUND_S, autopause.BETWEEN_MS = 0, 10
        self.addCleanup(lambda: setattr(autopause, "SOUND_S", self.saved[0]))
        self.addCleanup(lambda: setattr(autopause, "BETWEEN_MS", self.saved[1]))
        self.tmp = tempfile.TemporaryDirectory()
        config = os.path.join(self.tmp.name, "bus.conf")
        with open(config, "w") as file:
            file.write(BUS_CONFIG.format(folder=self.tmp.name))
        self.daemon = subprocess.Popen(["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address"],
                                       stdout=subprocess.PIPE, text=True)
        address = self.daemon.stdout.readline().strip()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.handset = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.desk = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        # The computer's own player, playing.
        self.desk_status = "Playing"
        self.desk_length = 0  # its track's length in µs (0: it doesn't say)
        self.desk_position = 0  # where it is in it, in µs
        self.desk_paused = []
        self.put_back()
        self.phones = {"PIXEL": Phone(False), "IPHONE": Phone(True)}
        self.pause = AutoPause(lambda: self.phones, bus=self.handset)
        self.pause.lowered = FakeLowered()
        self.pause.start()
        spin(lambda: False, 0.2)

    def _desk_call(self, _bus, _sender, _path, _interface, method, _params, invocation):
        self.desk_paused.append(method)
        invocation.return_value(None)

    def _desk_get(self, _bus, _sender, _path, _interface, name):
        if name == "Metadata":
            return GLib.Variant("a{sv}", {"mpris:length": GLib.Variant("x", self.desk_length)} if self.desk_length else {})
        if name == "Position":
            return GLib.Variant("x", self.desk_position)
        return GLib.Variant("s", self.desk_status)

    def tearDown(self):
        self.pause.stop()
        self.handset.close_sync(None)
        if not self.desk.is_closed():  # not quit by the test
            self.desk.close_sync(None)
        self.daemon.kill()
        self.daemon.wait()
        self.daemon.stdout.close()
        self.tmp.cleanup()

    def status(self, status: str | None, length_s: float | None = None):
        """The computer's player says it plays (or not; None: only its track); with `length_s`, its
        track's length too, in the same signal (otherwise Handset asks for it; 0: none, its metadata
        cleared, as a player says once it has nothing to play)."""
        changed = {}
        if status is not None:
            self.desk_status = status
            changed["PlaybackStatus"] = GLib.Variant("s", status)
        if length_s is not None:
            self.desk_length = int(length_s * 1_000_000)
            changed["Metadata"] = GLib.Variant("a{sv}", {"mpris:length": GLib.Variant("x", self.desk_length)}
                                               if self.desk_length else {})
        self.desk.emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", changed, [])))

    def take_down(self):
        """The computer's player taken down with its connection kept, as WebKit takes a page's: its
        object gone and its name given up, and nothing more said."""
        self.desk.unregister_object(self.desk_object)
        Gio.bus_unown_name(self.desk_name)

    def put_back(self, name="org.mpris.MediaPlayer2.desk"):
        """The computer's player there (again, on the same connection, once it's taken down; with
        another `name`, another player of the same page, as WebKit names each sound's)."""
        info = Gio.DBusNodeInfo.new_for_xml(DESK_PLAYER).interfaces[0]
        self.desk_object = self.desk.register_object_with_closures2("/org/mpris/MediaPlayer2", info, self._desk_call,
                                                                    self._desk_get, None)
        self.desk_name = Gio.bus_own_name_on_connection(self.desk, name, Gio.BusNameOwnerFlags.NONE, None, None)

    def short_tracks(self):
        """Tracks a second or more long are something to listen to, for tracks quick to play."""
        saved = autopause.SHORT_S
        autopause.SHORT_S = 1
        self.addCleanup(lambda: setattr(autopause, "SHORT_S", saved))

    def test_a_phone_starting_pauses_the_rest(self):
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.assertEqual(self.phones["PIXEL"].sent, [])  # not itself
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause"]))

    def test_the_computer_starting_pauses_the_phones(self):
        self.status("Playing")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.assertEqual(self.phones["PIXEL"].sent, [])  # not playing: left alone

    def test_a_moments_sound_only_lowers_the_rest(self):
        autopause.SOUND_S = 1
        self.status("Playing")  # a message's tone
        spin(lambda: False, 0.2)
        self.status("Paused")
        spin(lambda: False, 1.5)
        self.assertEqual(self.phones["IPHONE"].sent, [])  # never paused
        self.assertEqual(self.pause.lowered.said, ["lower", "restore"])

    def test_a_phone_never_lowers_its_own_sound(self):
        autopause.SOUND_S = 1
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"], 3))  # the rest still give way
        self.assertEqual(self.pause.lowered.said, [])  # but its song was never turned down

    def test_the_next_song_is_the_same_thing_going_on(self):
        autopause.SOUND_S, autopause.BETWEEN_MS = 1, 600
        self.status("Playing")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"], 2))
        self.phones["IPHONE"].now.playing = False
        lowered = list(self.pause.lowered.said)
        self.status("Stopped")  # between songs
        spin(lambda: False, 0.2)
        self.status("Playing")
        spin(lambda: False, 1.2)
        self.assertEqual(self.pause.lowered.said, lowered)  # not turned down again
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # and the phone stays paused
        self.status("Paused")  # over: the phone plays again
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 2))

    def test_a_phones_next_song(self):
        autopause.SOUND_S, autopause.BETWEEN_MS = 0, 600
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause"]))
        self.phones["PIXEL"].now.playing = False
        self.pause.phone_stopped("PIXEL")  # the next song, loading
        spin(lambda: False, 0.2)
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        spin(lambda: False, 1.0)
        self.assertEqual(self.desk_paused, ["Pause"])  # the computer's player not played in between

    def test_chimes_never_pause_the_music(self):
        autopause.SOUND_S = 1
        for case, said_with_it in (("said with it", True), ("asked for", False)):
            with self.subTest(case):
                self.phones["IPHONE"].sent.clear()
                self.pause.lowered.said.clear()
                self.desk_length = 1_500_000
                # Two notification chimes one after the other: the page's player plays on past the wait.
                self.status("Playing", 1.5 if said_with_it else None)
                spin(lambda: False, 2.3)
                self.assertEqual(self.phones["IPHONE"].sent, [])  # the music plays on
                self.assertEqual(self.pause.lowered.said, ["lower", "restore"])  # only lowered a moment
                self.status("Stopped")
                spin(lambda: False, 0.2)

    def test_a_chime_left_playing_is_no_music(self):
        autopause.BETWEEN_MS = 10
        self.status("Playing", 1.2)  # a chime, its player left saying it plays
        spin(lambda: False, 0.2)
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        spin(lambda: False, 0.3)
        self.assertEqual(self.desk_paused, [])  # never paused, so never chimes again on a resume
        self.phones["IPHONE"].now.playing = False  # as it said once paused
        self.phones["PIXEL"].now.playing = False
        self.pause.phone_stopped("PIXEL")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))  # not held up by it

    def test_a_phones_short_sound_isnt_music(self):
        autopause.SOUND_S = 0
        self.phones["PIXEL"].now = SimpleNamespace(playing=True, duration=2.0)
        self.pause.phone_started("PIXEL")
        spin(lambda: False, 0.5)
        self.assertEqual(self.phones["IPHONE"].sent, [])

    def test_what_paused_plays_again_once_its_over(self):
        self.status("Playing")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))

    def test_a_player_never_heard_from_plays_again_too(self):
        """The computer's player playing before Handset heard of it (found by its well-known name)
        is paused when a phone starts, and plays again with the other phone once it stops: what it
        says after its Pause comes from its unique name, and is the one kept."""
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause"]))
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False  # as they say once paused
        self.status("Paused")
        spin(lambda: False, 0.1)
        # Kept once, by the name it speaks from.
        self.assertEqual(set(self.pause.desk_status), {self.desk.get_unique_name()})
        self.phones["PIXEL"].now.playing = False
        self.pause.phone_stopped("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause", "Play"]))
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))

    def test_a_player_paused_isnt_taken_for_one_playing(self):
        """Paused when a phone starts (one that says nothing when it pauses), it's kept as the
        Pause leaves it: the other phone starting meanwhile doesn't pause it again."""
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause"]))
        self.pause.phone_started("IPHONE")
        self.assertTrue(spin(lambda: self.phones["PIXEL"].sent == ["pause"]))
        spin(lambda: False, 0.3)
        self.assertEqual(self.desk_paused, ["Pause"])

    def test_a_player_played_again_is_paused_again(self):
        """Paused when a phone starts and played again once it stops (one that says nothing either
        time), it's paused again when the phone starts again."""
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause"]))
        self.phones["IPHONE"].now.playing = False  # as they say once paused
        self.phones["PIXEL"].now.playing = False
        self.pause.phone_stopped("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause", "Play"]))
        spin(lambda: False, 0.1)
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause", "Play", "Pause"]))

    def test_a_player_taken_down_playing_on(self):
        """The computer's player taken down while it plays (WebKit's, for a page's voice note, once
        another sound in the page starts): it says nothing more, so what paused for it plays again
        once its track would be over, not before and not never."""
        self.short_tracks()
        self.status("Playing", 1.5)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False  # as it says once paused
        self.take_down()
        spin(lambda: False, 0.6)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # the voice note still plays
        self.assertNotIn(self.desk.get_unique_name(), self.pause.desk_status)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))

    def test_a_player_taken_down_saying_no_length(self):
        """Taken down while it plays, its track's length never said: what paused for it plays
        again at once, not held up by what it last said."""
        self.status("Playing")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))
        self.assertNotIn(self.desk.get_unique_name(), self.pause.desk_status)

    def test_a_player_taken_down_partway_through(self):
        """Taken down while it plays a track it was well into when the others gave way (a voice
        note played from its middle): what paused for it plays again once the rest of it would be
        over, not its whole length."""
        self.short_tracks()
        self.desk_position = 2_500_000  # 2.5 s into its 3 s
        self.status("Playing", 3)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.assertTrue(spin(lambda: self.desk.get_unique_name() in self.pause.desk_read))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 1.5))

    def test_a_player_quitting_while_it_plays(self):
        """The computer's player playing a long track quits (its program closed, or crashed): its
        name goes first, then its connection, and what paused for it plays again a moment on, not
        once its track would have been over."""
        self.status("Playing", 60)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.desk.close_sync(None)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))
        self.assertEqual((self.pause.resuming, self.pause.held), ({}, {}))

    def test_a_player_taken_down_coming_back_playing(self):
        """Taken down playing on, and put back by the same page, playing still (paused a moment
        and played again, or past when its track's end was guessed): what paused for it stays
        paused till it stops, and plays again then."""
        self.short_tracks()
        self.status("Playing", 1.5)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        self.put_back()
        self.status("Playing")
        spin(lambda: False, 2.0)  # past when its track would have been over
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))

    def test_a_player_taken_down_coming_back_paused(self):
        """Taken down playing on, and put back by the same page, paused: what paused for it plays
        again a moment on, not once its track would have been over."""
        self.short_tracks()
        self.status("Playing", 10)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        self.put_back()
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 1.0))

    def put_back_playing(self):
        """A video (10 s) the phones gave way to, taken down playing on, and put back by the same
        page playing (as WebKit puts a page's back once it plays again)."""
        self.short_tracks()
        self.status("Playing", 10)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        self.put_back()
        self.status("Playing")

    def test_a_player_put_back_playing_paused_before_its_end(self):
        """Taken down playing on, put back playing, and paused well before its track's end was
        guessed: what paused for it plays again a moment on, not once its track would be over."""
        self.put_back_playing()
        key = f"desk:{self.desk.get_unique_name()}"
        self.assertTrue(spin(lambda: key in self.pause.waiting))
        self.assertTrue(spin(lambda: key not in self.pause.waiting, 2))  # its wait over
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 1.0))

    def test_a_player_put_back_playing_paused_at_once(self):
        """Taken down playing on, put back playing, and paused again before its wait is over: what
        paused for it plays again a moment on all the same, not once its track would be over or never."""
        autopause.SOUND_S = 1
        self.put_back_playing()
        spin(lambda: False, 0.3)
        self.assertIn(f"desk:{self.desk.get_unique_name()}", self.pause.waiting)  # its wait not over
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 1.0))

    def test_a_player_put_back_playing_taken_down_in_its_wait(self):
        """A voice note (6 s) taken down playing on, put back playing by its page (the chime over),
        and taken down for another before its wait's over: what paused for it plays again once it
        would be over, from where it plays on, not its whole length from then."""
        self.short_tracks()
        self.status("Playing", 6)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        began = time.monotonic()
        self.assertTrue(spin(lambda: self.desk.get_unique_name() in self.pause.desk_read))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.5)
        autopause.SOUND_S = 2
        self.desk_position = int((time.monotonic() - began) * 1_000_000)  # where it plays on
        self.put_back()
        self.status("Playing", 6)
        key = f"desk:{self.desk.get_unique_name()}"
        self.assertTrue(spin(lambda: key in self.pause.waiting))
        spin(lambda: False, 1.0)
        self.take_down()  # for another chime, its wait not over
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 6))
        self.assertGreater(time.monotonic() - began, 5.9)
        self.assertLess(time.monotonic() - began, 6.5)

    def chime_after_a_voice_note(self):
        """A voice note (3 s) the phones gave way to, taken down for a chime in its page: the
        chime's own player put up on the same connection, playing (WebKit's, the voice note's
        playing on)."""
        self.short_tracks()
        self.status("Playing", 3)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        self.put_back("org.mpris.MediaPlayer2.desk.instance-2")
        self.status("Playing", 0.5)

    def test_a_chime_after_a_player_taken_down(self):
        """A chime after a voice note taken down for it, its player left saying it plays: what
        paused for the voice note plays again once it would be over, not held by the chime's."""
        self.chime_after_a_voice_note()
        spin(lambda: False, 1.5)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # the voice note plays on
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 3))

    def test_a_chime_over_before_the_voice_note(self):
        """A chime after a voice note taken down for it, over before it: what paused for the voice
        note isn't played again as the chime stops, but once the voice note would be over."""
        self.chime_after_a_voice_note()
        spin(lambda: False, 0.2)
        self.status("Paused")  # the chime's
        spin(lambda: False, 1.0)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # the voice note plays on
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 3))

    def test_a_chime_over_saying_no_length(self):
        """A chime after a voice note taken down for it, its player saying no length as it stops
        (its metadata cleared with it): what paused for the voice note isn't played again then
        either, but once the voice note would be over."""
        self.chime_after_a_voice_note()
        spin(lambda: False, 0.2)
        self.status("Stopped", 0)  # the chime's
        spin(lambda: False, 1.0)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # the voice note plays on
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 3))

    def test_a_chime_saying_no_length_then_over(self):
        """The same, its metadata cleared just before it says it stopped."""
        self.chime_after_a_voice_note()
        spin(lambda: False, 0.2)
        self.status(None, 0)  # the chime's
        spin(lambda: False, 0.05)
        self.status("Stopped")
        spin(lambda: False, 1.0)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # the voice note plays on
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 3))

    def test_a_page_closed_after_a_chime(self):
        """A chime after a voice note taken down for it, and the page closed (the chime's player
        gone with it first): what paused for the voice note plays again a moment on, not once it
        would have been over."""
        self.chime_after_a_voice_note()
        spin(lambda: False, 0.2)
        self.desk.close_sync(None)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 1.0))

    def test_the_next_voice_note_taken_down_too(self):
        """A voice note (3 s) taken down playing on, the next (4 s) played by the same page on its
        own player (as WhatsApp plays them one after the other), and taken down in its turn before
        the first would be over: what paused for them plays again once the next would be over, as
        far as it was read, not the first."""
        self.short_tracks()
        self.status("Playing", 3)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        self.put_back("org.mpris.MediaPlayer2.desk.instance-2")
        began = time.monotonic()
        self.status("Playing", 4)
        key = f"desk:{self.desk.get_unique_name()}"
        self.assertTrue(spin(lambda: key in self.pause.waiting))
        self.assertTrue(spin(lambda: key not in self.pause.waiting, 2))  # its wait over
        spin(lambda: False, 0.2)  # where it is read
        self.take_down()  # for a chime
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 6))
        self.assertGreater(time.monotonic() - began, 3.9)

    def test_the_next_voice_note_taken_down_in_its_wait(self):
        """A voice note (3 s) taken down playing on, and the next (6 s) on its own player taken
        down in its turn before its wait's over: what paused for them plays again once the next
        would be over, not the first."""
        self.short_tracks()
        self.status("Playing", 3)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        autopause.SOUND_S = 2
        self.put_back("org.mpris.MediaPlayer2.desk.instance-2")
        began = time.monotonic()
        self.status("Playing", 6)
        key = f"desk:{self.desk.get_unique_name()}"
        self.assertTrue(spin(lambda: key in self.pause.waiting))
        spin(lambda: False, 0.5)
        self.take_down()  # for a chime, its wait not over
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 8))
        self.assertGreater(time.monotonic() - began, 5.9)

    def test_the_next_voice_note_taken_down_in_its_wait_once_held_on(self):
        """The same, the first's end guessed while the next plays (held on for it, its wait not
        over yet): what paused for them plays again once the next would be over, not never."""
        self.short_tracks()
        self.status("Playing", 1.5)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 1.2)
        autopause.SOUND_S = 2
        self.put_back("org.mpris.MediaPlayer2.desk.instance-2")
        began = time.monotonic()
        self.status("Playing", 3)
        key = f"desk:{self.desk.get_unique_name()}"
        self.assertTrue(spin(lambda: key in self.pause.waiting))
        self.assertTrue(spin(lambda: key not in self.pause.resuming, 1.5))  # the first's end, held on
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])
        self.assertIn(key, self.pause.waiting)
        self.take_down()  # for a chime, its wait not over
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"], 6))
        self.assertGreater(time.monotonic() - began, 2.9)

    def test_a_long_track_after_a_player_taken_down(self):
        """Taken down playing on, the phone played by the person meanwhile, and something long
        started in the same page then (its own player): the phone gives way to it, as to anything."""
        self.short_tracks()
        self.status("Playing", 6)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.2)
        self.phones["IPHONE"].now.playing = True  # the person plays it
        self.pause.phone_started("IPHONE")
        self.assertTrue(spin(lambda: "phone:IPHONE" not in self.pause.waiting))
        spin(lambda: False, 0.3)  # its look for players answered
        self.put_back("org.mpris.MediaPlayer2.desk.instance-3")
        self.status("Playing", 10)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "pause"], 1.5))

    def test_a_hold_past_its_time_isnt_brought_back(self):
        """A phone paused for a player taken down playing something that ends past HELD_FOR_S (left
        held, as nothing would play again): the player pausing another phone later doesn't play
        that one again with the other, long after."""
        saved = autopause.HELD_FOR_S
        autopause.HELD_FOR_S = 1
        self.addCleanup(lambda: setattr(autopause, "HELD_FOR_S", saved))
        self.status("Playing", 60)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 1.2)  # past HELD_FOR_S
        self.phones["PIXEL"].now.playing = True  # the person plays the other phone
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: "phone:PIXEL" not in self.pause.waiting))
        spin(lambda: False, 0.3)  # its look for players answered
        self.put_back()
        self.status("Playing", 60)  # and the page something long again
        self.assertTrue(spin(lambda: self.phones["PIXEL"].sent == ["pause"]))
        self.phones["PIXEL"].now.playing = False
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["PIXEL"].sent == ["pause", "play"]))
        spin(lambda: False, 0.2)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])

    def test_a_player_taken_down_playing_past_its_hold(self):
        """Taken down playing a track that ends past HELD_FOR_S (a live stream's, said endless):
        no guess at its end, as nothing would play again then; what paused for it plays again
        once its page goes."""
        errors = []
        saved = sys.excepthook
        sys.excepthook = lambda *error: errors.append(error)  # a signal handler's, said only so
        self.addCleanup(lambda: setattr(sys, "excepthook", saved))
        self.status("Playing", 1e7)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        self.assertEqual(errors, [])
        self.assertEqual(self.pause.resuming, {})
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # it plays on
        self.desk.close_sync(None)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))

    def test_a_phone_played_meanwhile_is_the_persons(self):
        """A phone paused for a player since taken down playing on, played by the person and
        paused again before that player's track would be over: it stays paused then, as they left it."""
        self.short_tracks()
        self.status("Playing", 2.5)
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.take_down()
        spin(lambda: False, 0.3)
        self.phones["IPHONE"].now.playing = True  # the person plays it
        self.pause.phone_started("IPHONE")
        spin(lambda: False, 0.3)
        self.phones["IPHONE"].now.playing = False  # and pauses it
        self.pause.phone_stopped("IPHONE")
        spin(lambda: False, 2.8)  # past when the voice note would be over
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])

    def test_an_android_phone_left_off_the_desk_plays_again(self):
        """An Android phone paused for something long on the computer (a 12-minute video) is left
        off the desk meanwhile, paused longer than it's shown: it plays again all the same once
        that's over. An iPhone showing nothing isn't played (its own player would start)."""
        self.pause.android = lambda phone_id: phone_id == "PIXEL"
        self.phones["PIXEL"].now.playing = True
        self.status("Playing")
        self.assertTrue(spin(lambda: [phone.sent for phone in self.phones.values()] == [["pause"], ["pause"]]))
        self.phones["PIXEL"].now = self.phones["IPHONE"].now = None  # left off, both
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["PIXEL"].sent == ["pause", "play"]))
        spin(lambda: False, 0.2)
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])

    def test_kde_connects_players_are_the_phones_own(self):
        """KDE Connect's player for a phone is that phone: its playing pauses no phone, and a phone
        starting doesn't pause it (which would pause the phone itself)."""
        with open("/proc/self/comm") as file:
            me = file.read().strip()
        saved = autopause.PHONE_PROGRAMS
        autopause.PHONE_PROGRAMS = (*saved, me)  # the desk's player stands in for KDE Connect's
        self.addCleanup(lambda: setattr(autopause, "PHONE_PROGRAMS", saved))
        self.status("Playing")
        spin(lambda: False, 0.3)
        self.assertEqual(self.phones["IPHONE"].sent, [])
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        spin(lambda: False, 0.3)
        self.assertEqual(self.desk_paused, [])

    def test_the_phones_sound_among_the_servers(self):
        listed = [{"index": 7, "properties": {"application.name": autopause.HANDSET_SOUND},
                   "volume": {"front-left": {"value_percent": "80%"}}},
                  {"index": 9, "properties": {"application.name": "Firefox"}, "volume": {}},
                  {"index": 12, "properties": {"device.api": "bluez5", "node.name": "bluez_input.F0_99_B6_12_34_56"},
                   "volume": {"mono": {"value_percent": "100%"}}}]
        self.assertEqual(phone_streams(listed), [(7, autopause.HANDSET_SOUND, ("80%",)),
                                                 (12, "bluez_input.F0_99_B6_12_34_56", ("100%",))])


def wait(until, timeout=5.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        time.sleep(0.01)
    return until()


class LoweredTest(unittest.TestCase):
    """The phones' sound lowered and put back through the sound server, played by fakepactl.py (the
    computer's own is never asked): Handset's own stream at 80%, and a browser's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tools = os.path.join(self.tmp.name, "bin")
        os.mkdir(tools)
        os.symlink(os.path.join(HERE, "fakepactl.py"), os.path.join(tools, "pactl"))
        path = tools + os.pathsep + os.environ.get("PATH", "")
        self.assertEqual(shutil.which("pactl", path=path), os.path.join(tools, "pactl"))  # the stand-in it finds
        self.saved = {key: os.environ.get(key) for key in ("PATH", "FAKEPACTL_DIR", "LANG", "LC_ALL", "XDG_CACHE_HOME",
                                                           "XDG_CONFIG_HOME")}
        os.environ["PATH"] = path
        os.environ["FAKEPACTL_DIR"] = self.tmp.name
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")  # what's owed when it stops
        self.streams({7: "80%"})
        self.lowered = Lowered()

    def tearDown(self):
        self.lowered.stop()  # while the stand-in is still the pactl found
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write(self, name, text):
        with open(os.path.join(self.tmp.name, name + ".new"), "w") as file:
            file.write(text)
        os.replace(os.path.join(self.tmp.name, name + ".new"), os.path.join(self.tmp.name, name))

    def streams(self, levels: dict[int, str | tuple[str, str]], browser: str = "100%", chiming: bool = False):
        """The sound server's streams: the phone's (Handset's own) at these indexes and volumes
        (both channels', or each one's), and a browser's (and a notification's chime, `chiming`)."""
        listed = [{"index": index, "properties": {"application.name": autopause.HANDSET_SOUND},
                   "volume": {channel: {"value_percent": part} for channel, part in
                              zip(("front-left", "front-right"), (level, level) if isinstance(level, str) else level)}}
                  for index, level in levels.items()]
        listed.append({"index": 3, "properties": {"application.name": "Firefox"},
                       "volume": {"mono": {"value_percent": browser}}})
        if chiming:
            listed.append({"index": 4, "properties": {"application.name": "Chimes"},
                           "volume": {"mono": {"value_percent": "100%"}}})
        self.write("sink-inputs.json", json.dumps(listed))

    def levels(self, both: bool = False) -> dict[int, str | tuple[str, str]]:
        """The phone's streams' volumes, as the sound server has them (with `both`, each channel's)."""
        with open(os.path.join(self.tmp.name, "sink-inputs.json")) as file:
            return {stream["index"]: tuple(channel["value_percent"] for channel in stream["volume"].values()) if both
                    else stream["volume"]["front-left"]["value_percent"] for stream in json.load(file)
                    if stream["properties"].get("application.name") == autopause.HANDSET_SOUND}

    def owed_a_moment(self):
        """The phone's sound gone while lowered: owed its volume, and the sound server listened to."""
        self.lowered_a_moment()
        self.streams({})
        self.lowered.restore()
        self.assertTrue(wait(lambda: os.path.exists(os.path.join(self.tmp.name, "subscribed"))))

    def lowered_a_moment(self):
        self.lowered.lower()
        self.assertTrue(wait(lambda: self.levels() == {7: "30%"}))

    def test_put_back_while_it_was_still_being_lowered(self):
        """A computer's player playing and stopping again while the streams are still being listed
        to lower them: the phone's sound is left as it was, not lowered with nothing to put it back."""
        self.write("slow", "0.3")
        self.lowered.lower()
        self.lowered.restore()
        time.sleep(1.0)
        self.assertEqual(self.levels(), {7: "80%"})

    def test_a_stream_made_again_while_lowered(self):
        """The phone's sound started again while lowered (its link starting again): the sound server
        makes it lowered, as it remembers it, and it's put back with the rest."""
        self.lowered_a_moment()
        self.streams({8: "30%"})
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))

    def test_a_stream_gone_while_lowered_made_again_after(self):
        """The phone's sound gone while lowered, and started again only once the rest is put back
        (Play Sound Here trying again a while on): put back once it's there, lowered by the sound
        server a moment after it's made, and the sound server isn't listened to after."""
        self.lowered_a_moment()
        self.streams({})
        self.lowered.restore()
        self.assertTrue(wait(lambda: os.path.exists(os.path.join(self.tmp.name, "subscribed"))))
        self.streams({8: "100%"})
        time.sleep(0.1)
        self.streams({8: "30%"})
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))
        self.assertTrue(wait(lambda: self.lowered.listening is None))

    def test_a_stream_gone_as_its_put_back(self):
        """The phone's sound gone between being listed and put back (its link starting again just
        then): owed its volume all the same, and put back once it's made again lowered, not left
        lowered for good."""
        self.lowered_a_moment()
        self.write("vanish", "")
        self.lowered.restore()
        self.assertTrue(wait(lambda: os.path.exists(os.path.join(self.tmp.name, "subscribed"))))
        self.streams({8: "30%"})
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))
        self.assertTrue(wait(lambda: self.lowered.listening is None))

    def test_a_stream_made_before_the_sound_server_listens(self):
        """The phone's sound made again lowered as the sound server starts being listened to, so
        its news never tells of it: put back all the same, a moment on."""
        self.write("quiet", "")
        self.lowered_a_moment()
        self.streams({})
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.lowered.listening is not None))
        self.streams({8: "30%"})
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))
        self.assertTrue(wait(lambda: self.lowered.listening is None))

    def test_heard_whatever_the_language(self):
        """The sound server's news read the same in a user's language other than English (pactl
        says it translated otherwise)."""
        os.environ["LANG"] = os.environ["LC_ALL"] = "de_DE.UTF-8"
        self.lowered_a_moment()
        self.streams({})
        self.lowered.restore()
        self.assertTrue(wait(lambda: os.path.exists(os.path.join(self.tmp.name, "subscribed"))))
        time.sleep(1.2)  # past its look once it listens: what's put back after is what it heard
        self.streams({8: "30%"})
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))

    def test_lowered_again_before_its_put_back(self):
        """Made again lowered and not put back yet (nothing heard from the sound server), the
        phone's sound lowered again keeps its own volume, not the lowered one."""
        self.write("quiet", "")
        self.lowered_a_moment()
        self.streams({})
        self.lowered.restore()
        time.sleep(0.3)
        self.streams({8: "30%"})
        self.lowered.lower()
        self.assertTrue(wait(lambda: self.lowered.was == {8: ("80%", "80%")}))
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))

    def test_put_back_before_the_sync_goes(self):
        """Auto-pause stopping (the sync going, or starting itself again at once) puts the phone's
        sound back before it returns, not in a thread that wouldn't outlive it."""
        self.lowered_a_moment()
        pause = AutoPause(lambda: {})
        pause.lowered = self.lowered
        pause.stop()
        self.assertEqual(self.levels(), {7: "80%"})

    def test_put_back_though_not_listed_then(self):
        """The streams not listed when the phone's sound is put back (the sound server slow to
        answer): put back all the same by what was lowered, and nothing's owed, so a volume the
        person sets on it later (this low, too) is theirs."""
        self.lowered_a_moment()
        self.write("slow", "3.5")
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.levels() == {7: "80%"}))
        os.remove(os.path.join(self.tmp.name, "slow"))
        self.assertTrue(wait(lambda: self.lowered.was == {}))
        self.assertEqual((self.lowered.owed, self.lowered.listening), ({}, None))
        self.streams({7: "30%"})  # the person, in the sound settings
        time.sleep(1.5)
        self.assertEqual(self.levels(), {7: "30%"})

    def test_each_channel_as_loud_as_it_was(self):
        """The phone's sound with its channels set apart (its balance): put back each as it was."""
        self.streams({7: ("80%", "60%")})
        self.lowered.lower()
        self.assertTrue(wait(lambda: self.levels(both=True) == {7: ("30%", "30%")}))
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.levels(both=True) == {7: ("80%", "60%")}))

    def test_made_again_with_other_channels(self):
        """The phone's sound made again lowered with fewer channels than it had (its link made
        another way): put back as loud as its first channel was, not refused for the rest."""
        self.streams({7: ("80%", "60%")})
        self.lowered.lower()
        self.assertTrue(wait(lambda: self.levels(both=True) == {7: ("30%", "30%")}))
        self.streams({8: ("30%",)})
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.levels(both=True) == {8: ("80%",)}))
        self.assertTrue(wait(lambda: self.lowered.owed == {}))

    def test_a_second_phone_set_low_keeps_its_own(self):
        """Two phones' sound, one set this low by the person: each put back as loud as it was,
        not the low one as loud as the other (both have Handset's name)."""
        self.streams({7: "80%", 9: "30%"})
        self.lowered.lower()
        self.assertTrue(wait(lambda: len(self.lowered.was) == 2))
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.lowered.was == {}))
        self.assertEqual(self.levels(), {7: "80%", 9: "30%"})

    def test_two_phones_made_again_while_owed(self):
        """Both phones' sound gone while lowered and made again lowered, and lowered again before
        it's put back: both as loud as they were, once put back."""
        self.write("quiet", "")
        self.lowered_a_moment()
        self.streams({})
        self.lowered.restore()
        time.sleep(0.3)
        self.streams({8: "30%", 9: "30%"})
        self.lowered.lower()
        self.assertTrue(wait(lambda: len(self.lowered.was) == 2))
        self.lowered.restore()
        self.assertTrue(wait(lambda: self.levels() == {8: "80%", 9: "80%"}))

    def test_another_streams_volume_is_no_news(self):
        """Owed, the phone's sound made again not lowered: volumes set a while on (a browser's
        dragged about, then the person's on it, this low too) aren't the one the sound server
        remembers for a stream just made, and are left as they're set; and it's owed nothing, so
        another stream made later (a notification's chime) doesn't put it back either."""
        self.owed_a_moment()
        self.streams({8: "100%"})
        time.sleep(2.5)  # its own volume set long since
        for level in range(20, 40):
            self.streams({8: "100%"}, browser=f"{level}%")
            time.sleep(0.06)
        self.streams({8: "30%"})  # the person, in the sound settings
        time.sleep(1.0)
        self.assertEqual(self.levels(), {8: "30%"})
        self.streams({8: "30%"}, chiming=True)
        time.sleep(1.0)
        self.assertEqual(self.levels(), {8: "30%"})
        self.assertEqual((self.lowered.owed, self.lowered.listening), ({}, None))

    def test_listened_to_again_once_the_sound_server_is_back(self):
        """The sound server started again while the phone's sound is owed (its listener gone with
        it), and the phone's sound made again lowered meanwhile: put back all the same."""
        saved = autopause.LISTENED_S
        autopause.LISTENED_S = 0.5
        self.addCleanup(lambda: setattr(autopause, "LISTENED_S", saved))
        self.owed_a_moment()
        time.sleep(1.2)  # past its look once it listens
        listener = self.lowered.listening
        listener.terminate()
        self.assertTrue(wait(lambda: self.lowered.listening not in (None, listener)))  # listened to again
        self.streams({8: "30%"})
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))
        self.assertTrue(wait(lambda: self.lowered.listening is None))

    def test_listened_to_again_once_the_sound_server_is_down_a_while(self):
        """The sound server gone a while (its listener refused at once on trying again, as it would
        be with auto-pause off: nothing lowers or puts back the phones' sound meanwhile): tried
        again now and then, and the phone's sound made again lowered once it's back is put back."""
        saved = autopause.LISTENED_S, autopause.RETRY_S
        autopause.LISTENED_S, autopause.RETRY_S = 2, 1.5
        self.addCleanup(lambda: (setattr(autopause, "LISTENED_S", saved[0]), setattr(autopause, "RETRY_S", saved[1])))
        self.owed_a_moment()
        time.sleep(2.5)  # listened to longer than LISTENED_S: tried again 2 s after it ends
        listener = self.lowered.listening
        self.write("down", "")
        self.assertTrue(wait(lambda: self.lowered.listening not in (None, listener)))
        time.sleep(6)  # refused, again and again
        self.streams({8: "30%"})
        os.remove(os.path.join(self.tmp.name, "down"))
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}, 8))
        self.assertTrue(wait(lambda: self.lowered.listening is None))

    def test_tried_again_only_now_and_then_whatever_lowers_it(self):
        """The sound server gone a while, and the phones' sound lowered and put back meanwhile (the
        computer's players starting, one after another): its listener's tried again only now and
        then all the same, not once more for each."""
        from unittest import mock

        saved = autopause.RETRY_S
        autopause.RETRY_S = 3
        self.addCleanup(lambda: setattr(autopause, "RETRY_S", saved))
        self.owed_a_moment()
        self.write("down", "")
        for change in (self.lowered.lower, self.lowered.restore) * 2:
            time.sleep(1.3)  # each after the one before's listener was refused
            change()
        with mock.patch.object(self.lowered, "_listen", wraps=self.lowered._listen) as listen:
            time.sleep(autopause.RETRY_S)
        self.assertLessEqual(listen.call_count, 1)

    def test_owed_kept_for_the_next_sync(self):
        """The sync stopping while the phone's sound is owed (gone, its link down): the next sync
        puts it back once it's made again lowered, the sound server keeping it so meanwhile."""
        self.owed_a_moment()
        self.lowered.stop()
        self.assertTrue(os.path.exists(autopause.owed_path()))
        self.streams({8: "30%"})  # after a reboot, say
        pause = AutoPause(lambda: {}, bus=NoBus())
        self.lowered = pause.lowered
        self.assertEqual(self.lowered.owed, {autopause.HANDSET_SOUND: ("80%", "80%")})
        pause.start()
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))
        pause.stop()
        self.assertFalse(os.path.exists(autopause.owed_path()))  # nothing owed any more

    def test_owed_but_there_not_lowered(self):
        """Owed from the sync before, the phone's sound there not lowered all the same (the sound
        server didn't keep it so): owed nothing a moment on, as it's set, and not listened for."""
        self.owed_a_moment()
        self.lowered.stop()
        self.streams({8: "100%"})
        pause = AutoPause(lambda: {}, bus=NoBus())
        self.lowered = pause.lowered
        pause.start()
        self.assertTrue(wait(lambda: (self.lowered.owed, self.lowered.listening) == ({}, None), 6))
        self.assertEqual(self.levels(), {8: "100%"})

    def test_put_back_with_auto_pause_switched_off(self):
        """Auto-pause switched off in Handset's window while the phone's sound is owed its volume:
        the sync puts it back all the same once it's made again lowered, not left so for good, and
        it's auto-pause's own again once it's switched on. Its Android phones showing nothing are
        played again only with the session they showed left off for its age."""
        from unittest import mock

        from handset import appsync

        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "config")
        settings = os.path.join(os.environ["XDG_CONFIG_HOME"], "handset", "settings.json")
        os.makedirs(os.path.dirname(settings))
        self.owed_a_moment()
        sync = appsync.AppSync.__new__(appsync.AppSync)
        sync.autopause, sync.leftover, sync.players = None, None, {}
        sync.media = {"PIXEL": SimpleNamespace(left_off=False)}
        unheard = mock.patch.object(autopause, "AutoPause", lambda *args, **kwargs: AutoPause(*args, bus=NoBus(),
                                                                                              **kwargs))
        with unheard:
            sync._auto_pause()
        sync.autopause.lowered = self.lowered  # the one owing it
        self.assertFalse(sync.autopause.android("PIXEL"))
        sync.media["PIXEL"].left_off = True
        self.assertTrue(sync.autopause.android("PIXEL"))
        self.assertFalse(sync.autopause.android("IPHONE"))  # not followed: an iPhone
        with open(settings, "w") as file:
            file.write('{"auto_pause": false}')
        sync._auto_pause()
        self.assertIsNone(sync.autopause)
        self.lowered = sync.leftover
        self.assertTrue(wait(lambda: self.lowered.listening is not None))
        self.streams({8: "30%"})  # made again, lowered
        self.assertTrue(wait(lambda: self.levels() == {8: "80%"}))
        self.assertTrue(wait(lambda: self.lowered.listening is None))
        os.remove(settings)  # switched on again
        with unheard:
            sync._auto_pause()
        self.assertIsNone(sync.leftover)
        self.assertFalse(os.path.exists(autopause.owed_path()))  # nothing owed any more
        self.lowered = sync.autopause.lowered
        sync.autopause.stop()


if __name__ == "__main__":
    unittest.main()
