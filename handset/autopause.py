"""One thing playing at a time: whichever starts, the others give way, as headphones switching
between devices do, and come back when it's over. A phone starting to play gives way to it on the
other phones and the computer's own players (MPRIS: a browser's video, a music app); a player on the
computer starting, on the phones. The players standing for a phone over Bluetooth (BlueZ's
mpris-proxy) or through KDE Connect are left alone: they're that phone.

A sound on the computer first only lowers the phones' sound playing here (through the sound
server): a notification's chime, a message's tone, is over in a moment, and what was playing goes
on as it was. A phone's never lowers the phones' sound: that's its own. Still playing a few seconds
on, it's something to listen to: the others pause, and play again once it stops (if nothing else
was started meanwhile). A track shorter than SHORT_S is a sound whatever it does (a web page's
chime is one, its player left saying it plays after it's done, or two chimes one after the other):
it never pauses anything. The next song, or a moment's buffering, is the same thing going on, not
something starting. Off with `"auto_pause": false` in Handset's settings.
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
# A track this short (its player says how long it is) is a sound, never something to listen to: a
# notification's chime in a web page (WebKit and browsers show their pages' sounds as players).
SHORT_S = 20
LOWERED = "30%"  # how loud the phones' sound is while another plays a moment
# Playing again within this long of stopping, it's the same thing going on (the next song, a
# moment's buffering): nothing's lowered, and what paused for it stays paused. What paused for
# something comes back once it's been stopped this long, if it's within HELD_FOR_S.
BETWEEN_MS = 3000
HELD_FOR_S = 30 * 60
# The phones' sound as the sound server knows it: Handset's own stream (Play Sound Here on Android),
# or a phone's Bluetooth stream (an iPhone's).
HANDSET_SOUND = "Handset phone sound"
# The sound server listened to this long, and its listener ended by itself (the sound server
# started again): listened to again. One ending sooner (a sound server refusing it, not back yet)
# is tried again only RETRY_S on, as often as that is.
LISTENED_S = 10
RETRY_S = 30
# The volume the sound server remembers for a stream is set within this long of its being made:
# one owed its volume, listed this long and not lowered, was made as loud as it would have it.
REMEMBERED_S = 2


def wanted() -> bool:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config, "handset", "settings.json")) as file:
            return json.load(file).get("auto_pause", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def owed_path() -> str:
    """The phones' sound still owed its volume when the sync stopped, for the next one: the sound
    server keeps it lowered through a reboot."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "lowered.json")


def load_owed() -> dict[str, tuple[str, ...]]:
    try:
        with open(owed_path()) as file:
            kept = json.load(file)
    except (OSError, ValueError):
        return {}
    if not isinstance(kept, dict):
        return {}
    return {name: tuple(volume) for name, volume in kept.items()
            if isinstance(volume, list) and volume and all(isinstance(part, str) for part in volume)}


def phone_streams(listed: list[dict]) -> list[tuple[int, str, tuple[str, ...]]]:
    """The phones' sound among the sound server's streams (`pactl -f json list sink-inputs`):
    (index, its name, its volume as each channel's percentage, ("100%", "100%")). Its name is what
    the sound server remembers its volume by (WirePlumber's restore-stream), the same for the
    stream made again (its link started again) at another index: Handset's own, or the Bluetooth
    stream's node."""
    found = []
    for stream in listed:
        properties = stream.get("properties") or {}
        ours = properties.get("application.name") == HANDSET_SOUND
        bluetooth = properties.get("device.api") == "bluez5" or str(properties.get("node.name", "")).startswith("bluez")
        if ours or bluetooth:
            volume = tuple(channel.get("value_percent", "100%") for channel in (stream.get("volume") or {}).values())
            name = HANDSET_SOUND if ours else str(properties.get("node.name") or stream["index"])
            found.append((int(stream["index"]), name, volume or ("100%",)))
    return found


def fitted(volume: tuple[str, ...], channels: int) -> tuple[str, ...]:
    """A volume as a stream of so many channels takes it: each one's, or all as the first was (a
    stream made again with more or fewer: pactl refuses as many as it hasn't)."""
    return volume if len(volume) == channels else volume[:1]


class Lowered:
    """The phones' sound, lowered while another sound plays a moment, and as loud as it was after.

    Each change is made off the main loop, one at a time, and makes what's wanted by then: a
    restore asked while a lowering still lists the streams leaves them as they were. The sound
    server remembers a stream's volume by its name, a lowered one too, so a phone's stream made
    again (its link started again) comes back lowered: one made again while lowered is put back
    with the rest, by its name; one gone and made again only after is put back once it's there (or
    lowered again with the rest, keeping its own volume; made not lowered, it's owed nothing), the
    sound server listened to (`pactl
    subscribe`) until then, however long, and kept for the next sync if this one stops first
    (owed_path()): made again whenever, it's made lowered, and forgotten it would stay so."""

    def __init__(self):
        self.low = False  # whether the phones' sound is wanted lowered now
        self.was: dict[int, tuple[str, ...]] = {}  # stream → its volume before, each channel's
        self.names: dict[str, tuple[str, ...]] = {}  # the same by its name, for the stream made again
        # One gone while lowered, so made again lowered: its name → its volume before (a sync's
        # before this one's too).
        self.owed: dict[str, tuple[str, ...]] = load_owed()
        self.listed: dict[int, float] = {}  # the phones' streams listed last → when each was first
        self.listening: subprocess.Popen | None = None  # the sound server's news, while one's owed
        self.stopped = False
        self.lock = threading.Lock()  # one change at a time

    def lower(self):
        self.low = True
        threading.Thread(target=self._settle, daemon=True).start()

    def restore(self):
        self.low = False
        threading.Thread(target=self._settle, daemon=True).start()

    def stop(self):
        """As loud as it was, before Handset goes or starts again: made here, as a thread of its
        own wouldn't outlive it. One still owed its volume is kept for the next sync."""
        self.low, self.stopped = False, True
        self._settle()
        with self.lock:
            try:
                if self.owed:
                    os.makedirs(os.path.dirname(owed_path()), exist_ok=True)
                    GLib.file_set_contents(owed_path(), json.dumps(self.owed).encode())
                else:
                    os.unlink(owed_path())
            except (GLib.Error, OSError):
                pass

    def _settle(self, heard: subprocess.Popen | None = None):
        """The phones' streams as wanted now: lowered, or as loud as they were. On what the
        listener `heard`, nothing once it's no longer the one listening (Handset stopping, or
        nothing owed any more)."""
        with self.lock:
            if heard is not None and heard is not self.listening:
                return
            if self.low or self.was or self.owed:
                streams = self._streams()
                if streams is not None:
                    now = time.monotonic()
                    self.listed = {index: self.listed.get(index, now) for index, _name, _volume in streams}
                if not self.low:
                    self._restore(streams or [])  # not listed: the streams it lowered, as they were
                elif streams is not None:
                    self._lower(streams)
            if self.owed and not self.stopped:
                if self.listening is None:
                    self._listen()
            elif self.listening is not None:
                self._stop_listening()

    def _streams(self) -> list[tuple[int, str, tuple[str, ...]]] | None:
        if not shutil.which("pactl"):
            return None
        try:
            listed = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                                               text=True, timeout=3).stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return phone_streams(listed)

    def _lower(self, streams: list[tuple[int, str, tuple[str, ...]]]):
        # As loud as each was, known before this listing: a second phone's stream of the same name
        # set this low by the person isn't taken for one made again lowered.
        known = {**self.owed, **self.names}
        for index, name, volume in streams:
            if index in self.was:
                continue
            if set(volume) == {LOWERED}:  # made again lowered: as loud as the one before it was
                volume = fitted(known.get(name, volume), len(volume))
            self.owed.pop(name, None)
            self.was[index] = volume
            self.names.setdefault(name, volume)
            pactl("set-sink-input-volume", str(index), LOWERED)
        if self.was:
            debug("auto pause: the phones' sound lowered")

    def _restore(self, streams: list[tuple[int, str, tuple[str, ...]]]):
        put = {index for index, volume in self.was.items() if pactl("set-sink-input-volume", str(index), *volume)}
        # The names put back as loud as they were (not gone again before it): all of them when
        # every stream lowered was, listed now or not.
        back = set(self.names) if put == self.was.keys() else set()
        for index, name, volume in streams:
            if index in self.was:
                if index in put:
                    back.add(name)
            elif set(volume) == {LOWERED} and (name in self.names or name in self.owed):
                # Made again meanwhile, lowered as the sound server remembered it.
                want = fitted(self.names.get(name) or self.owed[name], len(volume))
                if pactl("set-sink-input-volume", str(index), *want):
                    back.add(name)
            elif name in self.owed and time.monotonic() - self.listed[index] > REMEMBERED_S:
                # Listed a while, not lowered: not made lowered, so nothing's owed (a volume set on
                # it since is the person's).
                back.add(name)
        # Gone meanwhile (or made again, and lowered by the sound server only a moment on): owed its
        # volume till it's there lowered, or lowered again with the rest.
        self.owed = {name: volume for name, volume in {**self.owed, **self.names}.items() if name not in back}
        self.was, self.names = {}, {}

    def _listen(self):
        """The sound server's news while a stream is owed its volume: a stream made, or a volume
        set just after (the sound server's remembered one, a moment after it's made), and they're
        settled again, and once more after that (one not lowered by then is owed nothing); not
        another stream's volume moved (a slider dragged), as often as that is."""
        try:
            # Its news untranslated: pactl says it in the user's language otherwise.
            process = subprocess.Popen(["pactl", "subscribe"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, env={**os.environ, "LC_ALL": "C"})
        except OSError:
            return
        self.listening = process

        def heard():
            began = time.monotonic()
            time.sleep(1)  # one made before pactl listened is never heard of: settled again once it does
            self._settle(process)
            self._look_again(process)
            made = float("-inf")  # when a stream was last made
            with process.stdout:
                for line in process.stdout:
                    if " on sink-input #" not in line:
                        continue
                    if "'new'" in line:
                        made = time.monotonic()
                        self._look_again(process)
                    elif "'change'" not in line or time.monotonic() - made > REMEMBERED_S:
                        continue  # not the volume the sound server remembered for one just made
                    self._settle(process)
            process.wait()
            if self.listening is not process:  # stopped, or owed nothing
                return
            # It ended by itself (the sound server started again, or refusing it): kept as the one
            # listening till it's tried again, so nothing else starts another meanwhile.
            time.sleep(2 if time.monotonic() - began > LISTENED_S else RETRY_S)  # refused at once: now and then
            with self.lock:
                if self.listening is not process:  # stopped, or owed nothing, meanwhile
                    return
                self.listening = None
            if not self.stopped:  # stopped meanwhile (the sync gone, auto-pause switched on): another's
                self._settle()  # what was made meanwhile put back, and listened to again

        threading.Thread(target=heard, daemon=True).start()

    def _look_again(self, process: subprocess.Popen):
        """The streams settled again once the sound server has set what it remembers for one just
        made: one owed and not lowered by then is forgotten (as the listener goes, its look does
        nothing)."""
        look = threading.Timer(REMEMBERED_S + 0.5, self._settle, (process,))
        look.daemon = True
        look.start()

    def _stop_listening(self):
        process, self.listening = self.listening, None
        if process.poll() is None:
            process.kill()
            process.wait()


def pactl(*args: str) -> bool:
    """Whether it was done (not for a stream gone meanwhile)."""
    try:
        return subprocess.run(["pactl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class AutoPause:
    """`phones()` gives the phones' players (phone id → media.PhonePlayer); `android(id)`, whether
    one showing nothing is an Android phone's with the session it showed only left off the desk for
    its age, paused all the same (androidmedia.MediaFollower.left_off)."""

    def __init__(self, phones: Callable[[], dict], bus: Gio.DBusConnection | None = None,
                 android: Callable[[str], bool] = lambda _phone_id: False):
        self.phones = phones
        self.android = android
        self.bus = bus
        self.watches: list[int] = []
        self.lowered = Lowered()
        self.waiting: dict[str, int] = {}  # what started (a phone's id, a player's bus name) → its timer
        self.desk_status: dict[str, str] = {}  # the computer's players, as they last said
        self.desk_length: dict[str, float] = {}  # their tracks' lengths, in seconds, when they say
        # Those whose track was last said to be a sound (under SHORT_S), still once they say no
        # length (a player clearing its metadata as it stops).
        self.desk_sound: set[str] = set()
        # Where one others gave way to was in its track when they did: (when, seconds in).
        self.desk_read: dict[str, tuple[float, float]] = {}
        # What paused for what: its key → {("phone", id) or ("desk", name)}, and when.
        self.held: dict[str, tuple[set[tuple[str, str]], float]] = {}
        self.stopped_at: dict[str, float] = {}  # what was playing and stopped: its key → when
        self.resuming: dict[str, int] = {}  # what stopped: its key → the timer that plays the rest again
        self.lowering: set[str] = set()  # what has the phones' sound lowered, while it's a moment's

    def start(self):
        if self.lowered.owed:  # owed its volume when the sync before stopped: put back once it's there
            self.lowered.restore()
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
        for timer in list(self.waiting.values()) + list(self.resuming.values()):
            GLib.source_remove(timer)
        self.waiting, self.resuming, self.lowering = {}, {}, set()
        self.lowered.stop()  # as loud as it was before this returns: the sync may start again at once

    # -- something started

    def phone_started(self, phone_id: str):
        """A phone started playing: the others give way (a moment on, if it's still playing).
        Started, it's the person's again: nothing that paused it plays it again."""
        for paused, _when in self.held.values():
            paused.discard(("phone", phone_id))
        self._started(f"phone:{phone_id}", lambda: self._phone_playing(phone_id),
                      lambda: self._phone_gives_way(phone_id), lower=False)

    def _changed(self, bus, sender, _path, _interface, _signal, parameters):
        _interface, changed, _gone = parameters.unpack()
        if sender == bus.get_unique_name():
            return
        if isinstance(changed.get("Metadata"), dict):
            self._remember_length(sender, changed["Metadata"])
        status = changed.get("PlaybackStatus")
        if status is None or self._a_phone(sender):
            return
        key = f"desk:{sender}"
        if key in self.resuming and status != "Playing" and (sender not in self.desk_status or (
                self._desk_listening(sender) and sender not in self.desk_sound)):
            # Heard from again once it was taken down, stopped (its player put back), or stopping
            # something long it was heard playing since (put back playing: WebKit puts a page's
            # back as it plays again): what paused for it plays again a moment on (its wait still
            # going or not), not when its track would be over guessed. One playing is most likely
            # the sound it was taken down for (a chime's player, put up in its place): a start of
            # its own, the track's end still waited for (and held on, if it's something long:
            # _held_over), and the guess kept as it stops.
            GLib.source_remove(self.resuming[key])
            self.resuming[key] = GLib.timeout_add(BETWEEN_MS, self._held_over, key)
        if status == "Playing":
            if "Metadata" not in changed:
                self._ask_length(sender)  # not said with it: asked, in time for the wait's end
            if key in self.held:  # held already (for a track taken down): where it is, read at once
                self._read_position(sender)  # (for one taken down in its turn, its wait not over)
            self._started(key, lambda: self._desk_listening(sender), lambda: self._desk_gives_way(sender))
        else:
            self._ended(key)
        self.desk_status[sender] = status

    def _remember_length(self, name: str, metadata: dict):
        length = metadata.get("mpris:length")
        if isinstance(length, int) and length > 0:
            if name in self.desk_read and self.desk_length.get(name) != length / 1_000_000:
                self.desk_read[name] = (time.monotonic(), 0.0)  # the next track, from its start
            self.desk_length[name] = length / 1_000_000
            if length / 1_000_000 < SHORT_S:
                self.desk_sound.add(name)
            else:
                self.desk_sound.discard(name)
        else:
            self.desk_length.pop(name, None)

    def _ask_length(self, name: str):
        def got(bus, result):
            try:
                metadata = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            if isinstance(metadata, dict):
                self._remember_length(name, metadata)

        self.bus.call(name, PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (PLAYER, "Metadata")), GLib.VariantType("(v)"),
                      Gio.DBusCallFlags.NONE, 2000, None, got)

    def _desk_listening(self, name: str) -> bool:
        """Whether a player on the computer plays something to listen to: playing, and not a
        sound (a track under SHORT_S)."""
        length = self.desk_length.get(name)
        return self.desk_status.get(name) == "Playing" and not (length and length < SHORT_S)

    def _gone(self, _bus, _sender, _path, _interface, _signal, parameters):
        name, old, new = parameters.unpack()
        if name.startswith(":") and not new:
            key = f"desk:{name}"
            if name not in self.desk_status and key in self.resuming:
                # Taken down playing on, its track's end guessed, and now its connection gone too
                # (its page closed, its program quit): it's over now.
                GLib.source_remove(self.resuming.pop(key))
            self.desk_status.pop(name, None)
            self.desk_length.pop(name, None)
            self.desk_sound.discard(name)
            self.desk_read.pop(name, None)
            self._ended(key)
            self.stopped_at.pop(key, None)  # gone: it won't be back
        elif name.startswith(MPRIS) and old and not new and old in self.desk_status:
            # Its player taken down, its connection kept (WebKit takes a page's down once another
            # sound in the page starts, its own playing on): it says nothing more, so it's forgotten.
            status, length = self.desk_status.pop(old), self.desk_length.pop(old, None)
            self.desk_sound.discard(old)
            key = f"desk:{old}"
            if status == "Playing" and length and length >= SHORT_S and key in self.held and key in self.waiting:
                # Something long on a connection held already (the next voice note, or the one
                # taken down put back playing), taken down in its turn before its wait's over:
                # playing on all the same, its end guessed too (from now, its start not read yet).
                GLib.source_remove(self.waiting.pop(key))
                self._unlower(key)
                self.desk_read.setdefault(old, (time.monotonic(), 0.0))
            if status == "Playing" and length and key in self.held and key not in self.waiting:
                # Playing on unheard: what paused for it plays again once its track would be over,
                # from where it was when they gave way (from its start, SOUND_S before, unread).
                # Over past HELD_FOR_S, nothing would: left held, till its connection goes.
                if key in self.resuming and old in self.desk_read and length >= SHORT_S:
                    # Something long it played since, taken down in its turn (the next voice note,
                    # read as it played): its end, not the one taken down before.
                    GLib.source_remove(self.resuming.pop(key))
                read, position = self.desk_read.pop(old, (self.held[key][1] - SOUND_S, 0.0))
                end = read + length - position
                if key not in self.resuming and end - self.held[key][1] < HELD_FOR_S:
                    wait = max(0, round((end - time.monotonic()) * 1000)) + BETWEEN_MS
                    self.resuming[key] = GLib.timeout_add(wait, self._held_over, key)
            else:
                self.desk_read.pop(old, None)
                self._ended(key)
                self.stopped_at.pop(key, None)

    def _started(self, key: str, still: Callable[[], bool], give_way: Callable[[], None], lower: bool = True):
        """Something started: the phones' sound lowered at once (`lower`: not when it's a phone's
        own); still playing SOUND_S on, the others pause (`give_way`); stopped before, the sound
        is as it was. Playing again just after it stopped, it's going on: as it was."""
        if key in self.waiting:
            return
        stopped = self.stopped_at.pop(key, None)
        if stopped is not None and time.monotonic() - stopped < BETWEEN_MS / 1000:
            timer = self.resuming.pop(key, None)
            if timer:
                GLib.source_remove(timer)  # what paused for it stays paused
            return
        if lower:
            self.lowering.add(key)
            self.lowered.lower()

        def later():
            self.waiting.pop(key, None)
            self._unlower(key)
            if still():
                give_way()
            return GLib.SOURCE_REMOVE

        self.waiting[key] = GLib.timeout_add_seconds(SOUND_S, later)

    def _ended(self, key: str):
        """It stopped: if it was a moment's sound, the sound is back as it was; if it had been
        playing, things paused for it play again once it's been stopped a moment (unless it's
        only between songs)."""
        timer = self.waiting.pop(key, None)
        if timer:
            GLib.source_remove(timer)
            self._unlower(key)
            return
        self.stopped_at[key] = time.monotonic()
        if key in self.held and key not in self.resuming:
            self.resuming[key] = GLib.timeout_add(BETWEEN_MS, self._held_over, key)

    def _unlower(self, key: str):
        """The phones' sound as it was, once nothing else has it lowered."""
        if key in self.lowering:
            self.lowering.discard(key)
            if not self.lowering:
                self.lowered.restore()

    def _held_over(self, key: str):
        self.resuming.pop(key, None)
        if key.startswith("desk:") and self._desk_listening(key[len("desk:"):]):
            # Its connection plays something long again (the track taken down, played again): what
            # paused for it is held till that stops, or its connection goes.
            return GLib.SOURCE_REMOVE
        held = self.held.pop(key, None)
        if held and time.monotonic() - held[1] < HELD_FOR_S:
            self._resume(held[0])
        return GLib.SOURCE_REMOVE

    def phone_stopped(self, phone_id: str):
        self._ended(f"phone:{phone_id}")

    # -- giving way, and coming back

    def _phone_playing(self, phone_id: str) -> bool:
        """Whether a phone plays something to listen to: playing, and not a sound (a track under
        SHORT_S, when the phone says how long it is)."""
        player = self.phones().get(phone_id)
        now = player.now if player else None
        return bool(now and now.playing and not (0 < now.duration < SHORT_S))

    def _phone_gives_way(self, phone_id: str):
        paused = set()
        for other, player in self.phones().items():
            if other != phone_id and player.now and player.now.playing:
                debug("auto pause: the phone", other)
                player.control("pause", 0.0)
                paused.add(("phone", other))
        for name in list(self.desk_status):
            if self._desk_listening(name) and not self._a_phone(name):
                debug("auto pause:", name)
                self._pause_desk(name)
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
        """A player Handset hadn't heard from paused, if it plays something to listen to (not a
        sound: one left saying it plays after its chime would only chime again when resumed).
        What it says is kept by its owner's unique name, the one its signals come from and that's
        forgotten when it goes, so it stays as the player says (not as it was before the Pause)."""
        def owned(bus, result):
            try:
                owner = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            if owner in self.desk_status:
                return  # heard from: given way already, if it plays
            bus.call(owner, PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (PLAYER,)),
                     GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, 2000, None, got, owner)

        def got(bus, result, owner: str):
            try:
                said = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            if isinstance(said.get("Metadata"), dict):
                self._remember_length(owner, said["Metadata"])
            self.desk_status[owner] = str(said.get("PlaybackStatus", ""))
            if self._desk_listening(owner) and not self._a_phone(owner):
                debug("auto pause:", name)
                self._pause_desk(owner)
                self._hold(f"phone:{phone_id}", {("desk", owner)})

        self.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                      GLib.Variant("(s)", (name,)), GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000, None, owned)

    def _desk_gives_way(self, sender: str):
        paused = set()
        for phone_id, player in self.phones().items():
            if player.now and player.now.playing:
                debug("auto pause: the phone", phone_id, "for", sender)
                player.control("pause", 0.0)
                paused.add(("phone", phone_id))
        self._hold(f"desk:{sender}", paused)
        if f"desk:{sender}" in self.held:  # held already too (for a track taken down): this one's read
            self._read_position(sender)

    def _read_position(self, name: str):
        """Where a player is in its track now, read once (players don't say as it moves on): from
        there, when its track would be over, if it's taken down playing on."""
        def got(bus, result):
            try:
                position = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            if isinstance(position, int) and position >= 0:
                self.desk_read[name] = (time.monotonic(), position / 1_000_000)

        self.bus.call(name, PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (PLAYER, "Position")), GLib.VariantType("(v)"),
                      Gio.DBusCallFlags.NONE, 2000, None, got)

    def _hold(self, key: str, paused: set[tuple[str, str]]):
        if paused:
            held, when = self.held.get(key, (set(), 0.0))
            if time.monotonic() - when >= HELD_FOR_S:
                held = set()  # held past it (not heard of again since): none of it would play again
            self.held[key] = (held | paused, time.monotonic())

    def _resume(self, paused: set[tuple[str, str]]):
        """What paused for something that's over plays again, unless something else plays now
        (the person started it) or it was started again meanwhile."""
        playing = any(self._phone_playing(phone_id) for phone_id in self.phones()) or \
            any(self._desk_listening(n) for n in self.desk_status if not self._a_phone(n))
        if playing:
            return GLib.SOURCE_REMOVE
        for kind, name in paused:
            if kind == "phone":
                player = self.phones().get(name)
                # Showing nothing, an Android phone's session may only be left off the desk, paused
                # longer than it's shown (androidmedia.PAUSED_FOR_S): played all the same.
                if player and (player.now or self.android(name)) and not (player.now and player.now.playing):
                    debug("auto pause: the phone", name, "plays again")
                    player.control("play", 0.0)
            elif self.desk_status.get(name) in ("Paused", None):
                debug("auto pause:", name, "plays again")
                self._desk_call(name, "Play")
                self.desk_status.pop(name, None)  # asked again, if it doesn't say so itself
        return GLib.SOURCE_REMOVE

    def _pause_desk(self, name: str):
        """A player on the computer paused, and kept as the Pause leaves it (its own signal says
        so a moment on, if it sends one): not taken for one still playing meanwhile."""
        self._desk_call(name, "Pause")
        self.desk_status[name] = "Paused"

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
