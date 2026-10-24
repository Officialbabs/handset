"""What an Android phone plays, over adb: its media sessions (`dumpsys media_session`), read by a
loop on the phone that says something only when they change (every second while one plays, now
and then otherwise); a track's length and its words as the phone gives them to Bluetooth devices
(`dumpsys bluetooth_manager`, while Bluetooth is on); and play, pause, next and previous sent as
media keys (`cmd media_session dispatch`). The shell has no covers and no place to go to in a
track.
"""

from __future__ import annotations

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
SKIP_TO_PREVIOUS, SKIP_TO_NEXT = 16, 32  # its actions
# Paused this long, it's left off the desk, as the phone itself drops it from its own controls.
PAUSED_FOR_S = 10 * 60
LOOK_PLAYING_S, LOOK_IDLE_S = 1, 4

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

    def start(self):
        threading.Thread(target=self._follow, daemon=True).start()

    def stop(self):
        self.stopped = True
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
            process.wait()

    def control(self, command: str, _place: float = 0.0):
        """Play, pause, toggle, next or previous, as the phone's media keys."""
        key = {"toggle": "pause" if self.playing else "play"}.get(command, command)
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
        from .android import adb_command

        while not self.stopped:
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
            if not self.stopped:
                time.sleep(3)  # the link dropped: followed again once it's back

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
