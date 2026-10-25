"""One thing playing at a time, on a bus of the test's own: a phone starting pauses the other phone
and the computer's player; the computer's player starting pauses the phones."""

import os
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace

from gi.repository import Gio, GLib

from handset import autopause
from handset.autopause import AutoPause, phone_streams

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
</interface></node>"""


def spin(until, timeout=3.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    return until()


class Phone:
    def __init__(self, playing):
        self.now = SimpleNamespace(playing=playing)
        self.sent = []

    def control(self, command, place):
        self.sent.append(command)


class FakeLowered:
    """The sound server stands in: never the computer's own."""

    def __init__(self):
        self.said = []

    def lower(self):
        self.said.append("lower")

    def restore(self):
        self.said.append("restore")


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
        self.desk_paused = []
        info = Gio.DBusNodeInfo.new_for_xml(DESK_PLAYER).interfaces[0]
        self.desk.register_object_with_closures2("/org/mpris/MediaPlayer2", info, self._desk_call, self._desk_get, None)
        Gio.bus_own_name_on_connection(self.desk, "org.mpris.MediaPlayer2.desk", Gio.BusNameOwnerFlags.NONE, None, None)
        self.phones = {"PIXEL": Phone(False), "IPHONE": Phone(True)}
        self.pause = AutoPause(lambda: self.phones, bus=self.handset)
        self.pause.lowered = FakeLowered()
        self.pause.start()
        spin(lambda: False, 0.2)

    def _desk_call(self, _bus, _sender, _path, _interface, method, _params, invocation):
        self.desk_paused.append(method)
        invocation.return_value(None)

    def _desk_get(self, _bus, _sender, _path, _interface, name):
        return GLib.Variant("s", self.desk_status)

    def tearDown(self):
        self.pause.stop()
        self.handset.close_sync(None)
        self.desk.close_sync(None)
        self.daemon.kill()
        self.daemon.wait()
        self.daemon.stdout.close()
        self.tmp.cleanup()

    def status(self, status):
        self.desk_status = status
        self.desk.emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player",
                                                          {"PlaybackStatus": GLib.Variant("s", status)}, [])))

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
        spin(lambda: False, 1.3)
        self.assertEqual(self.pause.lowered.said, [])  # its song was never turned down
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])  # the rest still give way

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

    def test_what_paused_plays_again_once_its_over(self):
        self.status("Playing")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.phones["IPHONE"].now.playing = False
        self.status("Paused")
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause", "play"]))

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
                  {"index": 12, "properties": {"device.api": "bluez5"}, "volume": {"mono": {"value_percent": "100%"}}}]
        self.assertEqual(phone_streams(listed), [(7, "80%"), (12, "100%")])


if __name__ == "__main__":
    unittest.main()
