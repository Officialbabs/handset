"""What the phones play, on the desk: each phone's now playing as one of the desk's media players
(MPRIS, as a music app is one), so the desk shows it (its title, artist, cover and how far in) and
its controls work the phone: play and pause, next, previous, a place in the track, and the app's
window to open. An Android phone's comes from its media sessions over adb (android.MediaFollower),
an iPhone's from its media service over Bluetooth (ancs). A phone's player is there while the
phone has something playing or paused, and goes with it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio, GLib

from . import apps
from .log import debug

ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
PATH = "/org/mpris/MediaPlayer2"
TRACKS = "/io/github/Officialbabs/Handset/track"
# A place in the track more than this far from where it should be has been moved to (Seeked).
JUMP_S = 2.0

XML = """<node>
<interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/>
  <method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
</interface>
<interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/>
  <method name="Previous"/>
  <method name="Pause"/>
  <method name="PlayPause"/>
  <method name="Stop"/>
  <method name="Play"/>
  <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
  <method name="SetPosition">
    <arg direction="in" name="TrackId" type="o"/>
    <arg direction="in" name="Position" type="x"/>
  </method>
  <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
  <signal name="Seeked"><arg name="Position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="Rate" type="d" access="read"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="read"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
</interface>
</node>"""


@dataclass(frozen=True)
class NowPlaying:
    """What a phone plays (or has paused): from its app `app` (package or bundle id)."""

    app: str
    title: str
    artist: str = ""
    album: str = ""
    playing: bool = False
    duration: float = 0.0  # seconds; 0 when the phone doesn't say
    position: float = 0.0  # seconds in, as of `at`
    at: float = 0.0  # time.monotonic() then
    rate: float = 1.0
    can_next: bool = True
    can_previous: bool = True
    can_seek: bool = False
    art: str = ""  # a picture of its cover (a file), when there's one

    def now(self) -> float:
        """How far in it is now."""
        if not self.playing or not self.at:
            return self.position
        moved = self.position + (time.monotonic() - self.at) * self.rate
        return max(0.0, min(moved, self.duration) if self.duration else moved)

    def same_track(self, other: NowPlaying | None) -> bool:
        return other is not None and (self.app, self.title, self.artist, self.album) == \
            (other.app, other.title, other.artist, other.album)


def bus_name(phone_id: str) -> str:
    """The phone's player's name on the session bus (one part per phone: letters and digits)."""
    return f"{ROOT}.Handset.p{hashlib.sha1(phone_id.encode()).hexdigest()[:12]}"


class PhonePlayer:
    """One phone's now playing as a desk media player, while it has one (show(now); show(None)
    when it has none). `control(command, place)` works the phone: "toggle", "play", "pause",
    "next", "previous", or "seek" to `place` seconds in."""

    def __init__(self, phone_id: str, phone_name: str, control: Callable[[str, float], None],
                 bus: Gio.DBusConnection | None = None):
        self.phone_id = phone_id
        self.phone_name = phone_name
        self.control = control
        self.bus = bus
        self.now: NowPlaying | None = None
        self.track = 0  # the track's number, for its id
        self.registrations: list[int] = []
        self.owner = 0

    # ---- What the phone says -----------------------------------------------------------------

    def show(self, now: NowPlaying | None):
        if now is not None and not now.title:
            now = None  # the desk shows no player without a title
        before, self.now = self.now, now
        if now is None:
            self._leave()
            return
        if not now.same_track(before):
            self.track += 1
        if not self.registrations:
            self._arrive()
            return
        self._changed(PLAYER, {name: self._player_property(name) for name in
                               ("PlaybackStatus", "Metadata", "Rate", "CanGoNext", "CanGoPrevious", "CanSeek")
                               if before is None or self._differs(name, before, now)})
        self._changed(ROOT, {name: self._root_property(name) for name in ("Identity", "DesktopEntry")
                             if before is None or before.app != now.app})
        if before is not None and now.same_track(before) and abs(now.now() - before.now()) > JUMP_S:
            self._emit_seeked(now.now())

    @staticmethod
    def _differs(name: str, before: NowPlaying, now: NowPlaying) -> bool:
        if name == "PlaybackStatus":
            return before.playing != now.playing
        if name == "Metadata":
            return not now.same_track(before) or before.duration != now.duration or before.art != now.art
        if name == "Rate":
            return before.rate != now.rate
        return {"CanGoNext": before.can_next != now.can_next, "CanGoPrevious": before.can_previous != now.can_previous,
                "CanSeek": before.can_seek != now.can_seek}[name]

    def stop(self):
        self.now = None
        self._leave()

    # ---- On the bus --------------------------------------------------------------------------

    def _arrive(self):
        bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION)
        self.bus = bus
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        for interface in node.interfaces:
            self.registrations.append(bus.register_object_with_closures2(PATH, interface, self._call, self._get, None))
        self.owner = Gio.bus_own_name_on_connection(bus, bus_name(self.phone_id), Gio.BusNameOwnerFlags.NONE,
                                                    None, None)
        debug("playing on", self.phone_name, "is on the desk:", self.now.title)

    def _leave(self):
        if self.owner:
            Gio.bus_unown_name(self.owner)
            self.owner = 0
        for registration in self.registrations:
            self.bus.unregister_object(registration)
        if self.registrations:
            debug("nothing plays on", self.phone_name)
        self.registrations = []

    def _changed(self, interface: str, changed: dict):
        if not changed or not self.bus:
            return
        self.bus.emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", (interface, changed, [])))

    def _emit_seeked(self, place: float):
        if self.bus:
            self.bus.emit_signal(None, PATH, PLAYER, "Seeked", GLib.Variant("(x)", (round(place * 1e6),)))

    def _get(self, _bus, _sender, _path, interface, name):
        return self._player_property(name) if interface == PLAYER else self._root_property(name)

    def _root_property(self, name: str) -> GLib.Variant | None:
        app = apps.find(self.phone_id, self.now.app) if self.now and self.now.app else None
        values = {
            "CanQuit": GLib.Variant("b", False),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            # The app's own entry, so the desk shows its icon and name, and its window.
            "Identity": GLib.Variant("s", app.title if app else self.phone_name),
            "DesktopEntry": GLib.Variant("s", app.id if app else "io.github.Officialbabs.Handset"),
            "SupportedUriSchemes": GLib.Variant("as", []),
            "SupportedMimeTypes": GLib.Variant("as", []),
        }
        return values.get(name)

    def _player_property(self, name: str) -> GLib.Variant | None:
        now = self.now or NowPlaying("", "")
        values = {
            "PlaybackStatus": GLib.Variant("s", "Playing" if now.playing else "Paused"),
            "Rate": GLib.Variant("d", now.rate or 1.0),
            "Metadata": GLib.Variant("a{sv}", self._metadata(now)),
            "Volume": GLib.Variant("d", 1.0),
            "Position": GLib.Variant("x", round(now.now() * 1e6)),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", now.can_next),
            "CanGoPrevious": GLib.Variant("b", now.can_previous),
            "CanPlay": GLib.Variant("b", True),
            "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", now.can_seek and now.duration > 0),
            "CanControl": GLib.Variant("b", True),
        }
        return values.get(name)

    def _metadata(self, now: NowPlaying) -> dict:
        found = {"mpris:trackid": GLib.Variant("o", f"{TRACKS}/{self.track}"),
                 "xesam:title": GLib.Variant("s", now.title)}
        if now.artist:
            found["xesam:artist"] = GLib.Variant("as", [now.artist])
        if now.album:
            found["xesam:album"] = GLib.Variant("s", now.album)
        if now.duration > 0:
            found["mpris:length"] = GLib.Variant("x", round(now.duration * 1e6))
        if now.art:
            found["mpris:artUrl"] = GLib.Variant("s", Gio.File.new_for_path(now.art).get_uri())
        return found

    def _call(self, _bus, _sender, _path, interface, method, parameters, invocation):
        now = self.now
        if interface == ROOT:
            if method == "Raise" and now:
                apps.open_window(self.phone_id, now.app if apps.find(self.phone_id, now.app) else None)
        elif now is None:
            pass
        elif method in ("PlayPause", "Play", "Pause", "Stop"):
            playing = {"PlayPause": not now.playing, "Play": True}.get(method, False)
            self.control({"PlayPause": "toggle", "Play": "play"}.get(method, "pause"), 0.0)
            # As the phone will have it in a moment: the desk's button turns at once.
            self.show(dataclasses.replace(now, playing=playing, position=now.now(), at=time.monotonic()))
        elif method in ("Next", "Previous"):
            self.control(method.lower(), 0.0)
        elif method in ("Seek", "SetPosition") and now.can_seek:
            if method == "Seek":
                place = now.now() + parameters.unpack()[0] / 1e6
            elif parameters.unpack()[0] == f"{TRACKS}/{self.track}":
                place = parameters.unpack()[1] / 1e6
            else:
                place = None  # a track that's gone
            if place is not None:
                place = max(0.0, min(place, now.duration or place))
                self.control("seek", place)
                self.now = dataclasses.replace(now, position=place, at=time.monotonic())
                self._emit_seeked(place)
        invocation.return_value(None)
