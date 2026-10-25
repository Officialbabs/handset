"""One thing playing at a time, on a bus of the test's own: a phone starting pauses the other phone
and the computer's player; the computer's player starting pauses the phones."""

import os
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace

from gi.repository import Gio, GLib

from handset.autopause import AutoPause

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


class AutoPauseTest(unittest.TestCase):
    def setUp(self):
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

    def test_a_phone_starting_pauses_the_rest(self):
        self.phones["PIXEL"].now.playing = True
        self.pause.phone_started("PIXEL")
        self.assertEqual(self.phones["IPHONE"].sent, ["pause"])
        self.assertEqual(self.phones["PIXEL"].sent, [])  # not itself
        self.assertTrue(spin(lambda: self.desk_paused == ["Pause"]))

    def test_the_computer_starting_pauses_the_phones(self):
        self.desk.emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player",
                                                          {"PlaybackStatus": GLib.Variant("s", "Playing")}, [])))
        self.assertTrue(spin(lambda: self.phones["IPHONE"].sent == ["pause"]))
        self.assertEqual(self.phones["PIXEL"].sent, [])  # not playing: left alone


if __name__ == "__main__":
    unittest.main()
