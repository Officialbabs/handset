"""What an Android phone plays, over adb. Read on the phone by Handset's own helper
(android/HandsetMedia.java, built into data/handset-media.jar and run by app_process as scrcpy's
server is): the media session's own words, length, where it is, its cover, and what it takes,
said as they change; and played, paused, skipped and moved in through it.

Where the helper can't run, the shell's view instead: the sessions from `dumpsys media_session`,
read by a loop on the phone that says something only when they change (every second while one
plays, now and then otherwise); a track's length and its words as the phone gives them to
Bluetooth devices (`dumpsys bluetooth_manager`, while Bluetooth is on); and play, pause, next and
previous sent as media keys (`cmd media_session dispatch`): no covers, no place to go to in a track.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import GLib

from .log import debug
from .media import NowPlaying

# PlaybackState's states (android.media.session.PlaybackState).
NONE, STOPPED, PAUSED, PLAYING, FAST_FORWARDING, REWINDING, BUFFERING, ERROR = range(8)
# Going on (moving, or about to): shown as playing.
GOING = {PLAYING, FAST_FORWARDING, REWINDING, BUFFERING, 8, 9, 10, 11}
MOVING = {PLAYING, FAST_FORWARDING, REWINDING}
SKIP_TO_PREVIOUS, SKIP_TO_NEXT, SEEK_TO = 16, 32, 256  # its actions
# Paused this long, it's left off the desk, as the phone itself drops it from its own controls.
PAUSED_FOR_S = 10 * 60
LOOK_PLAYING_S, LOOK_IDLE_S = 1, 4

# The phone-side helper, and how it's run there.
HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "handset-media.jar")
PHONE_HELPER = "/data/local/tmp/handset-media.jar"
RUN_HELPER = f"CLASSPATH={PHONE_HELPER} app_process / io.github.officialbabs.handset.HandsetMedia"
# A helper gone this soon, having said nothing, can't run on this phone: the shell's view instead.
HELPER_FAILS_WITHIN_S = 5
# Covers kept here for each phone, the latest few.
COVERS_KEPT = 12

# On the phone: the sessions' lines, said again only when they change.
WATCH = ("o=; while :; do d=$(dumpsys media_session | grep -E "
         "'userId=|package=|state=PlaybackState|metadata:'); "
         "if [ \"$d\" != \"$o\" ]; then cat /proc/uptime; echo \"$d\"; echo --; o=$d; fi; "
         f"case \"$d\" in *state=PLAYING*) sleep {LOOK_PLAYING_S};; *) sleep {LOOK_IDLE_S};; esac; done")
AVRCP = "dumpsys bluetooth_manager | sed -n '/Profile: AvrcpTargetService/,/BTAudio/p'"

HEADER = re.compile(r"^\s+(?P<tag>.+) (?P<package>[\w.]+)/(?P=tag)/(?P<id>\d+) \(userId=\d+\)\s*$")
BUTTON = re.compile(r"Media button session is (?P<package>[\w.]+)/(?P<tag>.+)/(?P<id>\d+) \(userId=\d+\)")
STATE = re.compile(r"state=PlaybackState \{state=\w+\((?P<state>\d+)\), position=(?P<position>-?\d+), "
                   r"buffered position=-?\d+, speed=(?P<speed>[-\d.eE]+), updated=(?P<updated>\d+), "
                   r"actions=(?P<actions>\d+)")
SONG = re.compile(r'Song: \{ mediaId=".*?" title="(?P<title>.*)" artist="(?P<artist>.*)" album="(?P<album>.*)" '
                  r'genre=".*" duration=(?P<duration>\d+)')


@dataclass
class Session:
    package: str
    tag: str
    id: str
    state: int = NONE
    position: int = 0  # ms, when it was `updated`
    speed: float = 1.0
    updated: int = 0  # the phone's uptime then, ms
    actions: int = 0
    description: str = ""  # "title, artist, album", as the session describes what it plays

    @property
    def key(self) -> tuple[str, str, str]:
        return self.package, self.tag, self.id


def parse_sessions(text: str) -> tuple[list[Session], tuple[str, str, str] | None]:
    """The sessions (most important first) and which one media keys go to."""
    sessions: list[Session] = []
    button = None
    current: Session | None = None
    for line in text.splitlines():
        found = BUTTON.search(line)
        if found:
            button = (found["package"], found["tag"], found["id"])
            current = None
            continue
        header = HEADER.match(line)
        if header and "session is" not in line:
            current = Session(header["package"], header["tag"], header["id"])
            sessions.append(current)
            continue
        if current is None:
            continue
        state = STATE.search(line)
        if state:
            current.state = int(state["state"])
            current.position = int(state["position"])
            current.speed = float(state["speed"])
            current.updated = int(state["updated"])
            current.actions = int(state["actions"])
        elif line.strip().startswith("metadata: size=") and "description=" in line:
            current.description = line.split("description=", 1)[1].strip()
    return sessions, button


def split_description(description: str) -> tuple[str, str, str]:
    """(title, artist, album) from a session's description: its three parts joined by ", " ("null"
    for one it hasn't), the artist's own commas kept."""
    parts = [part if part != "null" else "" for part in description.split(", ")]
    if len(parts) >= 3:
        return parts[0], ", ".join(parts[1:-1]), parts[-1]
    return (parts + ["", ""])[0], (parts + ["", ""])[1], ""


def parse_song(text: str) -> tuple[str, dict] | None:
    """The player Bluetooth devices are shown, and its song: (package, {title, artist, album,
    duration in seconds})."""
    active = re.search(r"<Active>\s+Media Player \d+: (?P<package>[\w.]+)", text)
    if not active:
        return None
    song = SONG.search(text, active.end())
    if not song:
        return None
    return active["package"], {"title": song["title"], "artist": song["artist"], "album": song["album"],
                               "duration": int(song["duration"]) / 1000}


def choose(sessions: list[Session], button: tuple[str, str, str] | None, uptime_ms: int) -> Session | None:
    """The session the desk shows: the one playing, else the one media keys go to while it has
    something paused (not for long), else none."""
    for session in sessions:
        if session.state in GOING and session.description:
            return session
    for session in sessions:
        if session.key == button and session.description and session.state == PAUSED \
                and uptime_ms - session.updated < PAUSED_FOR_S * 1000:
            return session
    return None


def now_playing(session: Session, uptime_ms: int, received: float, song: dict | None) -> NowPlaying:
    title, artist, album = split_description(session.description)
    duration = 0.0
    if song and title.startswith(song["title"]):
        title, artist, album, duration = song["title"], song["artist"], song["album"], song["duration"]
    moving = session.state in MOVING
    position = session.position + ((uptime_ms - session.updated) * session.speed if moving else 0)
    return NowPlaying(app=session.package, title=title, artist=artist, album=album,
                      playing=session.state in GOING, duration=duration, position=max(0.0, position / 1000),
                      at=received, rate=session.speed if moving and session.speed else 1.0,
                      can_next=bool(session.actions & SKIP_TO_NEXT),
                      can_previous=bool(session.actions & SKIP_TO_PREVIOUS))


def helped(event: dict, received: float, art: str = "") -> NowPlaying | None:
    """What the helper said, as the desk shows it (None: nothing to play)."""
    if event.get("event") != "media" or not event.get("title"):
        return None
    state, actions = int(event.get("state") or 0), int(event.get("actions") or 0)
    speed = float(event.get("speed") or 1.0)
    duration = max(0, int(event.get("duration") or 0)) / 1000
    moving = state in MOVING
    return NowPlaying(app=str(event.get("package") or ""), title=str(event["title"]),
                      artist=str(event.get("artist") or ""), album=str(event.get("album") or ""),
                      playing=state in GOING, duration=duration,
                      position=max(0.0, int(event.get("position_now") or 0) / 1000), at=received,
                      rate=speed if moving and speed else 1.0, can_next=bool(actions & SKIP_TO_NEXT),
                      can_previous=bool(actions & SKIP_TO_PREVIOUS),
                      can_seek=bool(actions & SEEK_TO) and duration > 0, art=art)


def covers_folder(serial: str) -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "art", hashlib.sha1(serial.encode()).hexdigest()[:12])


class MediaFollower:
    """One Android phone's now playing, followed while it's connected: `on_media(NowPlaying or
    None)` on GLib's main loop as it changes."""

    def __init__(self, serial: str, on_media: Callable[[NowPlaying | None], None]):
        self.serial = serial
        self.on_media = on_media
        self.process: subprocess.Popen | None = None
        self.stopped = False
        self.playing = False
        self.songs: dict[tuple[str, str], dict | None] = {}  # (package, description) → its Bluetooth song
        self.helper: subprocess.Popen | None = None  # the phone-side helper, while it runs
        self.helper_works: bool | None = None  # None: not tried yet
        self.covers: dict[str, str] = {}  # cover id → its file here

    def start(self):
        threading.Thread(target=self._follow, daemon=True).start()

    def stop(self):
        self.stopped = True
        for process in (self.process, self.helper):
            if process and process.poll() is None:
                process.kill()
                process.wait()
        self.process = self.helper = None

    def control(self, command: str, place: float = 0.0):
        """Play, pause, toggle, next, previous, or seek to `place` seconds: through the helper,
        or as the phone's media keys (no seeking then)."""
        key = {"toggle": "pause" if self.playing else "play"}.get(command, command)
        helper = self.helper
        if helper and helper.poll() is None and key in ("play", "pause", "next", "previous", "seek"):
            asked = {"op": key} if key != "seek" else {"op": "seek", "position": max(0, round(place * 1000))}
            try:
                helper.stdin.write(json.dumps(asked) + "\n")
                helper.stdin.flush()
                return
            except OSError:
                pass
        if key not in ("play", "pause", "next", "previous"):
            return
        from .android import shell

        def send():
            try:
                shell(self.serial, f"cmd media_session dispatch {key}", timeout=10)
            except (OSError, subprocess.SubprocessError) as error:
                debug("the phone didn't take", key, error)

        threading.Thread(target=send, daemon=True).start()

    # -- off the main loop

    def _follow(self):
        while not self.stopped:
            if self.helper_works is not False and os.path.exists(HELPER):
                started, said = time.monotonic(), self._run_helper()
                if self.stopped:
                    return
                if not said and time.monotonic() - started < HELPER_FAILS_WITHIN_S:
                    debug("the media helper doesn't run on", self.serial, ": the shell's view instead")
                    self.helper_works = False
                    continue
                time.sleep(3)  # the link dropped: followed again once it's back
                continue
            self._run_watch()
            if not self.stopped:
                time.sleep(3)

    def _run_helper(self) -> bool:
        """The helper on the phone until it goes: whether it said anything."""
        from .android import adb, adb_command

        try:
            adb(self.serial, "push", HELPER, PHONE_HELPER, timeout=30)
            process = subprocess.Popen(adb_command(self.serial, "shell", RUN_HELPER), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
        except (OSError, subprocess.SubprocessError) as error:
            debug("the media helper didn't start:", error)
            return False
        self.helper = process
        said = False
        with process.stdout:
            for line in process.stdout:
                if self.stopped:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "error":
                    debug("media helper:", event.get("message"))
                    continue
                said = True
                self._helped(event, time.monotonic())
        self.helper = None
        return said

    def _helped(self, event: dict, received: float):
        cover_id = str(event.get("cover_id") or "")
        if cover_id and event.get("cover"):
            self._keep_cover(cover_id, str(event["cover"]))
        now = helped(event, received, self.covers.get(cover_id, ""))
        self.playing = bool(now and now.playing)
        GLib.idle_add(self._show, now)

    def _keep_cover(self, cover_id: str, encoded: str):
        folder = covers_folder(self.serial)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{cover_id}.jpg")
        try:
            with open(path, "wb") as file:
                file.write(base64.b64decode(encoded))
        except (OSError, ValueError):
            return
        self.covers[cover_id] = path
        kept = sorted(os.listdir(folder), key=lambda name: os.path.getmtime(os.path.join(folder, name)))
        for name in kept[:-COVERS_KEPT]:
            os.remove(os.path.join(folder, name))
            self.covers = {key: value for key, value in self.covers.items() if not value.endswith(name)}

    def _run_watch(self):
        """The shell's view: the sessions' dump, said by the phone each time it changes."""
        from .android import adb_command

        if not self.stopped:
            try:
                process = subprocess.Popen(adb_command(self.serial, "shell", WATCH), stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True,
                                           errors="replace")
            except OSError as error:
                debug("what the phone plays can't be followed:", error)
                return
            self.process = process
            block: list[str] = []
            with process.stdout:
                for line in process.stdout:
                    if self.stopped:
                        break
                    if line.strip() == "--":
                        self._read(block, time.monotonic())
                        block = []
                    else:
                        block.append(line.rstrip("\n"))

    def _read(self, block: list[str], received: float):
        if not block:
            return
        try:
            uptime_ms = round(float(block[0].split()[0]) * 1000)
        except (ValueError, IndexError):
            return
        sessions, button = parse_sessions("\n".join(block[1:]))
        session = choose(sessions, button, uptime_ms)
        now = None
        if session:
            now = now_playing(session, uptime_ms, received, self._song(session))
        self.playing = bool(now and now.playing)
        GLib.idle_add(self._show, now)

    def _song(self, session: Session) -> dict | None:
        """The session's song as the phone gives it to Bluetooth devices: its length, and its
        words without the doubt commas leave. Read once a track."""
        key = (session.package, session.description)
        if key not in self.songs:
            from .android import shell

            try:
                found = parse_song(shell(self.serial, AVRCP, timeout=10))
            except (OSError, subprocess.SubprocessError):
                found = None
            self.songs = {key: found[1] if found and found[0] == session.package else None}
        return self.songs[key]

    def _show(self, now: NowPlaying | None):
        if not self.stopped:
            self.on_media(now)
        return GLib.SOURCE_REMOVE
