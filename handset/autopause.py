"""One thing playing at a time: whichever starts, the others give way, as headphones switching
between devices do, and come back when it's over. A phone starting to play gives way to it on the
other phones and the computer's own players (MPRIS: a browser's video, a music app); a player on the
computer starting, on the phones. The players standing for a phone over Bluetooth (BlueZ's
mpris-proxy) or through KDE Connect are left alone: they're that phone.

A sound first only lowers what's playing (the phones' sound playing here, through the sound
server): a notification's chime, a message's tone, is over in a moment, and what was playing goes
on as it was. Still playing a few seconds on, it's something to listen to: the others pause, and
play again once it stops (if nothing else was started meanwhile). Off with `"auto_pause": false` in
Handset's settings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import debug

MPRIS = "org.mpris.MediaPlayer2."
PLAYER = "org.mpris.MediaPlayer2.Player"
PATH = "/org/mpris/MediaPlayer2"
OURS = MPRIS + "Handset."  # the phones' own players (media.PhonePlayer)
# Programs whose players stand for a phone, not for anything on this computer: Handset's own,
# BlueZ's for a phone over Bluetooth, and KDE Connect's for what a phone plays (its multimedia
# plugin). Pausing one of those would pause the phone itself.
PHONE_PROGRAMS = ("mpris-proxy", "handset", "kdeconnectd")
# Shorter than this, a sound is a notification's: the others are only lowered meanwhile.
SOUND_S = 3
LOWERED = "30%"  # how loud the phones' sound is while another plays a moment
# What paused for something comes back once it stops, a moment after (in case it's only between
# tracks), if it's within this long.
RESUME_AFTER_MS = 1200
HELD_FOR_S = 30 * 60
# The phones' sound as the sound server knows it: Handset's own stream (Play Sound Here on Android),
# or a phone's Bluetooth stream (an iPhone's).
HANDSET_SOUND = "Handset phone sound"


def wanted() -> bool:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config, "handset", "settings.json")) as file:
            return json.load(file).get("auto_pause", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def phone_streams(listed: list[dict]) -> list[tuple[int, str]]:
    """The phones' sound among the sound server's streams (`pactl -f json list sink-inputs`):
    (index, its volume as a percentage, "100%")."""
    found = []
    for stream in listed:
        properties = stream.get("properties") or {}
        ours = properties.get("application.name") == HANDSET_SOUND
        bluetooth = properties.get("device.api") == "bluez5" or str(properties.get("node.name", "")).startswith("bluez")
        if ours or bluetooth:
            volume = next(iter((stream.get("volume") or {}).values()), {}).get("value_percent", "100%")
            found.append((int(stream["index"]), volume))
    return found


class Lowered:
    """The phones' sound, lowered while another sound plays a moment, and as loud as it was after."""

    def __init__(self):
        self.was: dict[int, str] = {}  # stream → its volume before
        self.lock = threading.Lock()

    def lower(self):
        threading.Thread(target=self._lower, daemon=True).start()

    def restore(self):
        threading.Thread(target=self._restore, daemon=True).start()

    def _lower(self):
        if not shutil.which("pactl"):
            return
        try:
            listed = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                                               text=True, timeout=3).stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return
        with self.lock:
            for index, volume in phone_streams(listed):
                if index not in self.was:
                    self.was[index] = volume
                    pactl("set-sink-input-volume", str(index), LOWERED)
            if self.was:
                debug("auto pause: the phones' sound lowered")

    def _restore(self):
        with self.lock:
            was, self.was = self.was, {}
            for index, volume in was.items():
                pactl("set-sink-input-volume", str(index), volume)


def pactl(*args: str):
    try:
        subprocess.run(["pactl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


class AutoPause:
    """`phones()` gives the phones' players (phone id → media.PhonePlayer)."""

    def __init__(self, phones: Callable[[], dict], bus: Gio.DBusConnection | None = None):
        self.phones = phones
        self.bus = bus
        self.watches: list[int] = []
        self.lowered = Lowered()
        self.waiting: dict[str, int] = {}  # what started (a phone's id, a player's bus name) → its timer
        self.desk_status: dict[str, str] = {}  # the computer's players, as they last said
        # What paused for what: its key → {("phone", id) or ("desk", name)}, and when.
        self.held: dict[str, tuple[set[tuple[str, str]], float]] = {}

    def start(self):
        try:
            self.bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error:
            return
        self.watches = [
            self.bus.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged", PATH, PLAYER,
                                      Gio.DBusSignalFlags.NONE, self._changed),
            self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                      "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE, self._gone),
        ]

    def stop(self):
        if self.bus:
            for watch in self.watches:
                self.bus.signal_unsubscribe(watch)
        self.watches = []
        for timer in self.waiting.values():
            GLib.source_remove(timer)
        self.waiting = {}
        self.lowered.restore()

    # -- something started

    def phone_started(self, phone_id: str):
        """A phone started playing: the others give way (a moment on, if it's still playing)."""
        self._started(f"phone:{phone_id}", lambda: self._phone_playing(phone_id), lambda: self._phone_gives_way(phone_id))

    def _changed(self, bus, sender, _path, _interface, _signal, parameters):
        _interface, changed, _gone = parameters.unpack()
        status = changed.get("PlaybackStatus")
        if status is None or sender == bus.get_unique_name() or self._a_phone(sender):
            return
        key = f"desk:{sender}"
        if status == "Playing":
            self._started(key, lambda: self.desk_status.get(sender) == "Playing", lambda: self._desk_gives_way(sender))
        else:
            self._ended(key)
        self.desk_status[sender] = status

    def _gone(self, _bus, _sender, _path, _interface, _signal, parameters):
        name, _old, new = parameters.unpack()
        if name.startswith(":") and not new:
            self.desk_status.pop(name, None)
            self._ended(f"desk:{name}")

    def _started(self, key: str, still: Callable[[], bool], give_way: Callable[[], None]):
        """Something started: the phones' sound lowered at once; still playing SOUND_S on, the
        others pause (`give_way`); stopped before, the sound is as it was."""
        if key in self.waiting:
            return
        self.lowered.lower()

        def later():
            self.waiting.pop(key, None)
            self.lowered.restore()
            if still():
                give_way()
            return GLib.SOURCE_REMOVE

        self.waiting[key] = GLib.timeout_add_seconds(SOUND_S, later)

    def _ended(self, key: str):
        """It stopped: if it was a moment's sound, the sound is back as it was; if things paused
        for it, they play again, a moment on."""
        timer = self.waiting.pop(key, None)
        if timer:
            GLib.source_remove(timer)
            self.lowered.restore()
        held = self.held.pop(key, None)
        if held and time.monotonic() - held[1] < HELD_FOR_S:
            GLib.timeout_add(RESUME_AFTER_MS, self._resume, held[0])

    def phone_stopped(self, phone_id: str):
        self._ended(f"phone:{phone_id}")

    # -- giving way, and coming back

    def _phone_playing(self, phone_id: str) -> bool:
        player = self.phones().get(phone_id)
        return bool(player and player.now and player.now.playing)

    def _phone_gives_way(self, phone_id: str):
        paused = set()
        for other, player in self.phones().items():
            if other != phone_id and player.now and player.now.playing:
                debug("auto pause: the phone", other)
                player.control("pause", 0.0)
                paused.add(("phone", other))
        for name, status in list(self.desk_status.items()):
            if status == "Playing" and not self._a_phone(name):
                debug("auto pause:", name)
                self._desk_call(name, "Pause")
                paused.add(("desk", name))
        self._hold(f"phone:{phone_id}", paused)
        if self.bus:  # players that were playing before Handset heard of them
            self.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", None,
                          GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 3000, None, self._listed, phone_id)

    def _listed(self, bus, result, phone_id: str):
        try:
            names = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        for name in names:
            if name.startswith(MPRIS) and not name.startswith(OURS):
                self._pause_if_playing(name, phone_id)

    def _pause_if_playing(self, name: str, phone_id: str):
        def got(bus, result):
            try:
                status = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            if status == "Playing" and not self._a_phone(name):
                debug("auto pause:", name)
                self._desk_call(name, "Pause")
                self._hold(f"phone:{phone_id}", {("desk", name)})

        self.bus.call(name, PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (PLAYER, "PlaybackStatus")), GLib.VariantType("(v)"),
                      Gio.DBusCallFlags.NONE, 2000, None, got)

    def _desk_gives_way(self, sender: str):
        paused = set()
        for phone_id, player in self.phones().items():
            if player.now and player.now.playing:
                debug("auto pause: the phone", phone_id, "for", sender)
                player.control("pause", 0.0)
                paused.add(("phone", phone_id))
        self._hold(f"desk:{sender}", paused)

    def _hold(self, key: str, paused: set[tuple[str, str]]):
        if paused:
            held = self.held.get(key, (set(), 0.0))[0]
            self.held[key] = (held | paused, time.monotonic())

    def _resume(self, paused: set[tuple[str, str]]):
        """What paused for something that's over plays again, unless something else plays now
        (the person started it) or it was started again meanwhile."""
        playing = any(p.now and p.now.playing for p in self.phones().values()) or \
            any(s == "Playing" for n, s in self.desk_status.items() if not self._a_phone(n))
        if playing:
            return GLib.SOURCE_REMOVE
        for kind, name in paused:
            if kind == "phone":
                player = self.phones().get(name)
                if player and player.now and not player.now.playing:
                    debug("auto pause: the phone", name, "plays again")
                    player.control("play", 0.0)
            elif self.desk_status.get(name) in ("Paused", None):
                debug("auto pause:", name, "plays again")
                self._desk_call(name, "Play")
        return GLib.SOURCE_REMOVE

    def _desk_call(self, name: str, method: str):
        self.bus.call(name, PATH, PLAYER, method, None, None, Gio.DBusCallFlags.NONE, 2000, None, None)

    def _a_phone(self, name: str) -> bool:
        """Whether a player stands for a phone: Handset's own, BlueZ's for a phone over
        Bluetooth (mpris-proxy), or KDE Connect's (PHONE_PROGRAMS)."""
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
        return program in PHONE_PROGRAMS
