"""A phone's player on the desk, on a bus of the test's own (never the desktop's): what the desk
reads from it, and its controls reaching the phone."""

import os
import subprocess
import tempfile
import time
import unittest

from gi.repository import Gio, GLib

from handset import media
from handset.media import NowPlaying, PhonePlayer


# A bus of the test's own: nothing started on demand, anyone may own any name.
BUS_CONFIG = """<busconfig>
  <type>session</type>
  <listen>unix:dir={folder}</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>"""


def spin(until, timeout=3.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)
    return until()


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.tmp.name  # no phone apps' entries but the test's
        config = os.path.join(self.tmp.name, "bus.conf")
        with open(config, "w") as file:
            file.write(BUS_CONFIG.format(folder=self.tmp.name))
        self.daemon = subprocess.Popen(["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address"],
                                       stdout=subprocess.PIPE, text=True)
        address = self.daemon.stdout.readline().strip()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.desk = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.sent = []
        self.player = PhonePlayer("PHONE1", "Pixel 7", lambda command, place: self.sent.append((command, place)),
                                  bus=self.bus)
        self.changes = []
        self.seeked = []
        self.desk.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged", media.PATH, None,
                                   Gio.DBusSignalFlags.NONE,
                                   lambda *args: self.changes.append(args[-1].unpack()))
        self.desk.signal_subscribe(None, media.PLAYER, "Seeked", media.PATH, None, Gio.DBusSignalFlags.NONE,
                                   lambda *args: self.seeked.append(args[-1].unpack()[0]))

    def tearDown(self):
        self.player.stop()
        self.bus.close_sync(None)
        self.desk.close_sync(None)
        self.daemon.kill()
        self.daemon.wait()
        self.daemon.stdout.close()
        if self.saved is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self.saved
        self.tmp.cleanup()

    def owned(self) -> bool:
        reply = self.desk.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                    "NameHasOwner", GLib.Variant("(s)", (media.bus_name("PHONE1"),)), None,
                                    Gio.DBusCallFlags.NONE, 1000, None)
        return reply.unpack()[0]

    def get(self, interface, name):
        return self.call("Get", GLib.Variant("(ss)", (interface, name)), "org.freedesktop.DBus.Properties")[0]

    def call(self, method, args=None, interface=media.PLAYER):
        """A call as the desk makes it, answered on this thread's main loop (as the player's are)."""
        result = []
        self.desk.call(media.bus_name("PHONE1"), media.PATH, interface, method, args, None,
                       Gio.DBusCallFlags.NONE, 1000, None, lambda bus, res: result.append(bus.call_finish(res)))
        self.assertTrue(spin(lambda: result))
        return result[0].unpack()

    def song(self, **changes):
        values = dict(app="com.example.music", title="A Song", artist="A Singer", album="An Album", playing=True,
                      duration=200.0, position=10.0, at=time.monotonic(), can_seek=True)
        values.update(changes)
        return NowPlaying(**values)

    def test_what_the_desk_reads(self):
        self.player.show(self.song())
        self.assertTrue(spin(self.owned))
        metadata = self.get(media.PLAYER, "Metadata")
        self.assertEqual(metadata["xesam:title"], "A Song")
        self.assertEqual(metadata["xesam:artist"], ["A Singer"])  # a list, as the desk wants it
        self.assertEqual(metadata["mpris:length"], 200_000_000)
        self.assertTrue(metadata["mpris:trackid"].startswith(media.TRACKS))
        self.assertEqual(self.get(media.PLAYER, "PlaybackStatus"), "Playing")
        self.assertTrue(self.get(media.PLAYER, "CanSeek"))
        self.assertGreaterEqual(self.get(media.PLAYER, "Position"), 10_000_000)
        self.assertEqual(self.get(media.ROOT, "Identity"), "Pixel 7")  # no entry for the app here
        self.assertEqual(self.get(media.ROOT, "DesktopEntry"), "io.github.Officialbabs.Handset")

    def test_controls_reach_the_phone(self):
        self.player.show(self.song())
        self.assertTrue(spin(self.owned))
        self.call("PlayPause")
        self.assertEqual(self.sent, [("toggle", 0.0)])
        self.assertEqual(self.get(media.PLAYER, "PlaybackStatus"), "Paused")  # at once, before the phone says
        self.assertTrue(spin(lambda: any(c[1].get("PlaybackStatus") == "Paused" for c in self.changes)))
        self.call("Next")
        self.assertEqual(self.sent[-1], ("next", 0.0))
        track = self.get(media.PLAYER, "Metadata")["mpris:trackid"]
        self.call("SetPosition", GLib.Variant("(ox)", (track, 50_000_000)))
        self.assertEqual(self.sent[-1], ("seek", 50.0))
        self.assertTrue(spin(lambda: self.seeked == [50_000_000]))

    def test_a_new_track_and_a_jump(self):
        self.player.show(self.song())
        self.assertTrue(spin(self.owned))
        first = self.get(media.PLAYER, "Metadata")["mpris:trackid"]
        self.player.show(self.song(position=120.0, at=time.monotonic()))  # moved on the phone
        self.assertTrue(spin(lambda: self.seeked and self.seeked[-1] >= 120_000_000))
        self.player.show(self.song(title="Another Song", position=0.0, at=time.monotonic()))
        self.assertTrue(spin(lambda: any(c[1].get("Metadata", {}).get("xesam:title") == "Another Song"
                                         for c in self.changes)))
        self.assertNotEqual(self.get(media.PLAYER, "Metadata")["mpris:trackid"], first)

    def test_it_goes_when_nothing_plays(self):
        self.player.show(self.song())
        self.assertTrue(spin(self.owned))
        self.player.show(None)
        self.assertTrue(spin(lambda: not self.owned()))
        self.player.show(self.song(title=""))  # no title: nothing the desk would show
        self.assertFalse(spin(self.owned, timeout=0.3))

    def test_where_it_is(self):
        song = self.song(position=10.0, at=time.monotonic() - 5)
        self.assertAlmostEqual(song.now(), 15.0, delta=0.2)
        self.assertEqual(self.song(playing=False, position=10.0, at=time.monotonic() - 5).now(), 10.0)
        self.assertEqual(self.song(position=199.0, at=time.monotonic() - 50).now(), 200.0)  # not past its end


if __name__ == "__main__":
    unittest.main()
