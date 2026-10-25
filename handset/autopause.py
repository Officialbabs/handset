"""One thing playing at a time: whichever starts, the others pause, as headphones switching
between devices do. A phone starting to play pauses the other phones and the computer's own
players (MPRIS: a browser's video, a music app); a player on the computer starting pauses the
phones. The players standing for a phone over Bluetooth (BlueZ's mpris-proxy) are left alone:
they're that phone. Off with `"auto_pause": false` in Handset's settings.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import debug

MPRIS = "org.mpris.MediaPlayer2."
PLAYER = "org.mpris.MediaPlayer2.Player"
PATH = "/org/mpris/MediaPlayer2"
OURS = MPRIS + "Handset."  # the phones' own players (media.PhonePlayer)


def wanted() -> bool:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config, "handset", "settings.json")) as file:
            return json.load(file).get("auto_pause", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


class AutoPause:
    """`phones()` gives the phones' players (phone id → media.PhonePlayer)."""

    def __init__(self, phones: Callable[[], dict], bus: Gio.DBusConnection | None = None):
        self.phones = phones
        self.bus = bus
        self.watch = 0

    def start(self):
        try:
            self.bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error:
            return
        self.watch = self.bus.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged", PATH,
                                               PLAYER, Gio.DBusSignalFlags.NONE, self._changed)

    def stop(self):
        if self.watch and self.bus:
            self.bus.signal_unsubscribe(self.watch)
            self.watch = 0

    # -- a phone started

    def phone_started(self, phone_id: str):
        """The phone started playing: the others stop."""
        for other, player in self.phones().items():
            if other != phone_id and player.now and player.now.playing:
                debug("auto pause: the phone", other)
                player.control("pause", 0.0)
        if self.bus:
            self.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", None,
                          GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 3000, None, self._listed)

    def _listed(self, bus, result):
        try:
            names = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        for name in names:
            if name.startswith(MPRIS) and not name.startswith(OURS):
                self._pause_if_playing(name)

    def _pause_if_playing(self, name: str):
        def got(bus, result):
            try:
                status = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            if status == "Playing" and not self._a_phone(name):
                debug("auto pause:", name)
                bus.call(name, PATH, PLAYER, "Pause", None, None, Gio.DBusCallFlags.NONE, 3000, None, None)

        self.bus.call(name, PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (PLAYER, "PlaybackStatus")),
                      GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 3000, None, got)

    # -- a player on the computer started

    def _changed(self, bus, sender, _path, _interface, _signal, parameters):
        _interface, changed, _gone = parameters.unpack()
        if changed.get("PlaybackStatus") != "Playing" or sender == bus.get_unique_name():
            return
        if self._a_phone(sender):
            return
        for phone_id, player in self.phones().items():
            if player.now and player.now.playing:
                debug("auto pause: the phone", phone_id, "for", sender)
                player.control("pause", 0.0)

    def _a_phone(self, name: str) -> bool:
        """Whether a player stands for a phone: Handset's own, or BlueZ's for a phone over
        Bluetooth (mpris-proxy)."""
        if name.startswith(OURS):
            return True
        try:
            reply = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                       "GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)),
                                       GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 1000, None)
            with open(f"/proc/{reply.unpack()[0]}/comm") as file:
                program = file.read().strip()
        except (GLib.Error, OSError):
            return False
        return program in ("mpris-proxy", "handset")
