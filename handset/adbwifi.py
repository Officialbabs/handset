"""Android phones back over Wi-Fi when they're wanted. A phone whose adb listens on Wi-Fi (turned on
by its cable: Use Over Wi-Fi Too, `adb tcpip`) can lose adb's link to it (adb's server started
again, the network dropped a moment) and nothing dials it again: the system's phone service may
not know its address. Handset remembers where each phone was found on Wi-Fi (find_serial), and
hears where a phone's adb says it listens (_adb._tcp on the network, named
"adb-<serial>-<letters>"), and connects it there:

- at once, when it's wanted: its window or one of its apps opened, its files searched
  (connect_now), as the iPhone's helper reaches its phone when it's wanted;
- by itself, an Android phone Handset uses that isn't connected (the sync's WifiReconnect): as
  soon as it says on the network where it listens, and otherwise now and then (a phone quiet on
  the network, asleep, is asked rarely).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable

from gi.repository import GLib

from .log import debug

SERVICE = "_adb._tcp"
AGAIN_S = 30  # a phone isn't connected to again sooner than this
QUIET_AGAIN_S = 5 * 60  # nor, when it isn't saying where it listens, sooner than this
_lock = threading.Lock()


def serial_of(name: str) -> str | None:
    """The phone's serial in its announcement's name ("adb-<serial>-<letters>")."""
    found = re.fullmatch(r"adb-(.+)-[A-Za-z0-9]+", name)
    return found.group(1) if found else None


def remembered_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "adb-wifi.json")


def _load() -> dict[str, list]:
    try:
        with open(remembered_path()) as file:
            places = json.load(file)
        return places if isinstance(places, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(serial: str, address: str, port: int):
    """Where a phone was found on Wi-Fi, for next time."""
    with _lock:
        places = _load()
        if places.get(serial) == [address, port]:
            return
        places[serial] = [address, port]
        try:
            os.makedirs(os.path.dirname(remembered_path()), exist_ok=True)
            with open(remembered_path() + ".new", "w") as file:
                json.dump(places, file)
            os.replace(remembered_path() + ".new", remembered_path())
        except OSError as error:
            debug("where the phone is on Wi-Fi wasn't kept:", error)


def remembered(serial: str) -> tuple[str, int] | None:
    place = _load().get(serial)
    if isinstance(place, list) and len(place) == 2:
        return str(place[0]), int(place[1])
    return None


def connect(serial: str, address: str, port: int) -> bool:
    """adb connected to the phone there; whether it is."""
    from .android import adb

    try:
        said = adb(None, "connect", f"{address}:{port}", timeout=15).strip()
    except (OSError, subprocess.SubprocessError) as error:
        said = str(error)
    debug("adb over Wi-Fi to", serial, "at", f"{address}:{port}:", said)
    return said.startswith(("connected", "already connected"))


def connect_now(serial: str) -> bool:
    """The phone wanted now: connected where it was last on Wi-Fi, if it was. Blocks a moment."""
    place = remembered(serial)
    return bool(place) and connect(serial, *place)


class WifiReconnect:
    """`wanted()` gives the serials of the Android phones to connect again: those Handset uses
    that aren't connected now. Call check() when that may have changed, and now() when they're
    wanted at once (their files searched)."""

    def __init__(self, wanted: Callable[[], set[str]], browser=None):
        from .avahi import Browser

        self.wanted = wanted
        self.seen: dict[str, tuple[str, int]] = {}  # serial → where its adb says it listens
        self.tried: dict[str, float] = {}
        self.browser = browser or Browser(SERVICE, self._found, self._lost)
        self.timer = 0

    def start(self):
        self.browser.start()
        self.timer = GLib.timeout_add_seconds(AGAIN_S, self._again)

    def stop(self):
        self.browser.stop()
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = 0

    def _found(self, name: str, address: str, port: int):
        serial = serial_of(name)
        if serial:
            self.seen[serial] = (address, port)
            remember(serial, address, port)
            self.check()

    def _lost(self, name: str):
        serial = serial_of(name)
        if serial:
            self.seen.pop(serial, None)

    def _again(self):
        self.check()
        return GLib.SOURCE_CONTINUE

    def check(self, at_once: bool = False):
        now = time.monotonic()
        for serial in self.wanted():
            place = self.seen.get(serial)
            wait = AGAIN_S if place or at_once else QUIET_AGAIN_S
            place = place or remembered(serial)
            if not place or now - self.tried.get(serial, -QUIET_AGAIN_S) < wait:
                continue
            self.tried[serial] = now
            threading.Thread(target=connect, args=(serial, *place), daemon=True).start()

    def now(self):
        """The phones wanted at once (their files searched): asked now, not in a while."""
        self.check(at_once=True)
