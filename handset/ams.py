"""What the iPhone plays, over Bluetooth: Apple Media Service, on the same link as its
notifications (ancs). The iPhone tells what its player app is, whether it plays and how far in,
and the track's title, artist, album and length, each as it changes; the computer sends it play,
pause, next and previous. It has no covers and no place to go to in a track.

Apple's spec: developer.apple.com/library/archive/documentation/CoreBluetooth/Reference/
AppleMediaService_Reference. Each value is text ("1,1.0,42.3" for how it plays: playing, at
normal speed, 42.3 seconds in, as the iPhone sent it).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import debug
from .media import NowPlaying

AMS = "89d3502b-0f36-433a-8ef4-c502ad55f8dc"
REMOTE_COMMAND = "9b3c81d8-57b1-4a8a-b8df-0e56f7ca51c2"
ENTITY_UPDATE = "2f7cabce-808d-411f-9a0c-bb92ba96c102"
ENTITY_ATTRIBUTE = "c6b2f38c-23ab-46d8-a6ab-a3a870bbd5d7"

PLAYER, QUEUE, TRACK = 0, 1, 2
NAME, PLAYBACK = 0, 1  # the player's
ARTIST, ALBUM, TITLE, DURATION = 0, 1, 2, 3  # the track's
TRUNCATED = 1
COMMANDS = {"play": 0, "pause": 1, "toggle": 2, "next": 3, "previous": 4}
PLAYING, REWINDING, FAST_FORWARDING = 1, 2, 3
# The pieces of a change come one by one (a new track's title, then its artist…): shown together.
SETTLE_MS = 150

GATT = "org.bluez.GattCharacteristic1"


def parse_entity(data: bytes) -> tuple[int, int, bool, str] | None:
    """(entity, attribute, truncated, value) from an Entity Update notification."""
    if len(data) < 3:
        return None
    return data[0], data[1], bool(data[2] & TRUNCATED), data[3:].decode(errors="replace")


def parse_playback(text: str) -> tuple[int, float, float] | None:
    """(state, rate, elapsed seconds) from the player's PlaybackInfo."""
    parts = text.split(",")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), float(parts[1] or 0), float(parts[2] or 0)
    except ValueError:
        return None


def number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


class Writes:
    """Writes to GATT characteristics, one at a time for each (BlueZ refuses a second while one
    is going), `then(ok)` once each is done."""

    def __init__(self, bus: Gio.DBusConnection):
        self.bus = bus
        self.pending: dict[str, list[tuple[bytes, Callable[[bool], None] | None]]] = {}

    def write(self, path: str, value: bytes, then: Callable[[bool], None] | None = None):
        queue = self.pending.setdefault(path, [])
        queue.append((value, then))
        if len(queue) == 1:
            self._send(path)

    def clear(self):
        self.pending = {}

    def _send(self, path: str):
        value, _then = self.pending[path][0]
        self.bus.call("org.bluez", path, GATT, "WriteValue",
                      GLib.Variant("(aya{sv})", (value, {"type": GLib.Variant("s", "request")})), None,
                      Gio.DBusCallFlags.NONE, 10000, None, self._done, path)

    def _done(self, bus, result, path: str):
        ok = True
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            ok = False
            debug("a Bluetooth write didn't go:", error.message)
        queue = self.pending.get(path) or []
        then = queue.pop(0)[1] if queue else None
        if queue:
            self._send(path)
        if then:
            then(ok)


class Ams:
    """The connected iPhone's now playing: `on_media(NowPlaying or None)` on GLib's main loop as
    it changes. `app_of(name)` is the bundle id of the iPhone's app with that name (the service
    names its player app by its name only), or ""."""

    def __init__(self, bus: Gio.DBusConnection, writes: Writes, on_media: Callable[[NowPlaying | None], None],
                 app_of: Callable[[str], str]):
        self.bus = bus
        self.writes = writes
        self.on_media = on_media
        self.app_of = app_of
        self.chars: dict[str, str] = {}  # uuid → characteristic path, while the iPhone offers it
        self.values: dict[tuple[int, int], str] = {}
        self.received = 0.0  # when the playback info came
        self.commands: set[int] | None = None  # what the player takes now; None: not said yet
        self.settle = 0
        self.shown: NowPlaying | None = None

    def attach(self, chars: dict[str, str]):
        """The service's characteristics (on each connection, and when the iPhone offers it
        again): its updates asked for afresh."""
        if chars == self.chars:
            return
        self.detach()
        self.chars = chars
        debug("the iPhone's now playing follows")
        for uuid in (REMOTE_COMMAND, ENTITY_UPDATE):
            self.bus.call("org.bluez", chars[uuid], GATT, "StartNotify", None, None, Gio.DBusCallFlags.NONE, 10000,
                          None, self._started, uuid)

    def detach(self):
        if not self.chars:
            return
        self.chars, self.values, self.commands = {}, {}, None
        self._show(None)

    def owns(self, path: str) -> bool:
        return path in self.chars.values()

    def changed(self, path: str, value: bytes):
        """A characteristic's value as the iPhone sent it."""
        if path == self.chars.get(REMOTE_COMMAND):
            self.commands = set(value)
            self._later()
        elif path == self.chars.get(ENTITY_UPDATE):
            parsed = parse_entity(value)
            if not parsed:
                return
            entity, attribute, truncated, text = parsed
            self.values[(entity, attribute)] = text
            if (entity, attribute) == (PLAYER, PLAYBACK):
                self.received = time.monotonic()
            if truncated and entity == TRACK:
                self._read_whole(entity, attribute)
            self._later()

    def control(self, command: str, _place: float = 0.0):
        """Play, pause, toggle, next or previous, on the iPhone's player."""
        code = COMMANDS.get(command)
        path = self.chars.get(REMOTE_COMMAND)
        if code is None or not path:
            return
        if self.commands is not None and code not in self.commands and command != "toggle":
            code = COMMANDS["toggle"] if command in ("play", "pause") else code
        self.writes.write(path, bytes([code]))

    # -- the service's flow

    def _started(self, bus, result, uuid: str):
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            if "InProgress" not in error.message:  # BlueZ's "subscribed already" is as good as done
                debug("the iPhone's now playing didn't start:", error.message)
                return
        path = self.chars.get(ENTITY_UPDATE)
        if uuid == ENTITY_UPDATE and path:
            # Asked for once its updates are on (it refuses before): its values come at once, then
            # each change.
            self.writes.write(path, bytes([PLAYER, NAME, PLAYBACK]))
            self.writes.write(path, bytes([TRACK, ARTIST, ALBUM, TITLE, DURATION]))

    def _read_whole(self, entity: int, attribute: int):
        """A value too long for its update, read whole."""
        path = self.chars.get(ENTITY_ATTRIBUTE)
        if not path:
            return

        def read(ok: bool):
            if ok and self.chars.get(ENTITY_ATTRIBUTE) == path:
                self.bus.call("org.bluez", path, GATT, "ReadValue", GLib.Variant("(a{sv})", ({},)), None,
                              Gio.DBusCallFlags.NONE, 10000, None, self._read, (entity, attribute))

        self.writes.write(path, bytes([entity, attribute]), read)

    def _read(self, bus, result, key):
        try:
            value = bytes(bus.call_finish(result).unpack()[0])
        except GLib.Error:
            return
        self.values[key] = value.decode(errors="replace")
        self._later()

    def _later(self):
        if self.settle:
            GLib.source_remove(self.settle)
        self.settle = GLib.timeout_add(SETTLE_MS, self._settled)

    def _settled(self):
        self.settle = 0
        self._show(self.now_playing())
        return GLib.SOURCE_REMOVE

    def now_playing(self) -> NowPlaying | None:
        """What the values say, as it is now: nothing without a title."""
        title = self.values.get((TRACK, TITLE), "")
        if not title:
            return None
        state, rate, elapsed = parse_playback(self.values.get((PLAYER, PLAYBACK), "")) or (0, 0.0, 0.0)
        playing = state in (PLAYING, REWINDING, FAST_FORWARDING)
        commands = self.commands
        name = self.values.get((PLAYER, NAME), "")
        return NowPlaying(app=self.app_of(name) if name else "", title=title,
                          artist=self.values.get((TRACK, ARTIST), ""), album=self.values.get((TRACK, ALBUM), ""),
                          playing=playing, duration=number(self.values.get((TRACK, DURATION), "")),
                          position=elapsed, at=self.received, rate=rate if playing and rate else 1.0,
                          can_next=commands is None or COMMANDS["next"] in commands,
                          can_previous=commands is None or COMMANDS["previous"] in commands)

    def _show(self, now: NowPlaying | None):
        if now != self.shown:
            self.shown = now
            self.on_media(now)
