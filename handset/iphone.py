"""An iPhone, the ways Ada's phone service reaches one: by its cable and over Wi-Fi, both tried at
once, with the pairings the phone and the desktop already made and Apple's developer services. A small helper (handset-ios, in ios/) does the
talking to the phone; Handset runs it and speaks to it in lines:

- its standard output is the phone's screen as video, as the helper's first status line says
  (H.265 from the phone);
- its standard error is status lines, one JSON object each: {"event":"status","text":…},
  {"event":"video","codec":"h265"}, {"event":"input","ready":true}, {"event":"error",
  "message":…}; anything else is the helper's log;
- its standard input takes commands, one JSON object each: {"op":"down"|"move","x","y"} with x
  and y from 0 to 65535 across the screen, {"op":"up"}, {"op":"button","name":…} (home, lock,
  volume_up, volume_down), {"op":"key","name":…} (enter, delete, tab, escape, the arrows),
  {"op":"text","text":…} and {"op":"launch","bundle":…} (an app to the front, said back as
  {"event":"launch","ok":…}).

`handset-ios apps` lists the phone's apps instead (list_apps here). An iPhone shows one app at
a time on its one screen, so an iPhone app "in a window of its own" is the phone's screen with
that app opened in front.

The phone takes touches only while its screen is being sent, which Handset keeps doing anyway.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from dataclasses import dataclass

from gi.repository import GLib

from .gestures import Gesture
from .log import DEBUG, debug

if TYPE_CHECKING:
    from .decoder import Decoder


def _decoder(*args, **kwargs) -> Decoder:
    """A video decoder: GStreamer is loaded only once a screen shows (not to list apps, say)."""
    from .decoder import Decoder

    return Decoder(*args, **kwargs)


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The helper, as install.sh puts it, or as `cargo build --release` leaves it in the checkout.
HELPER_PLACES = (os.path.expanduser("~/.local/lib/handset/handset-ios"),
                 os.path.join(HERE, "ios", "target", "release", "handset-ios"))
# The pairing Ada's desktop made for each iPhone, by its UDID.
PAIRINGS = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ada", "phones")
# The digitizer's range, edge to edge, whatever the screen's size.
SPAN = 65535
# An iPhone 12's screen until the first picture says otherwise (its pixels, upright).
FIRST_SHOWN = (1170, 2532)
# A lit iPhone sends frames even of a still screen (a few a second); a dark one sends none. This
# long without one, its screen is off.
DARK_AFTER_S = 3.0
# iPhones' screens in pixels, upright. The phone's stream pads its frames out to whole 16-pixel
# blocks with black (an iPhone 12's 1170×2532 comes as 1184×2576), and says nothing of the
# screen's own size: Handset keeps the frames' top left this size.
SCREENS = {
    "iPhone SE": (750, 1334),
    "iPhone 11": (828, 1792), "iPhone XR": (828, 1792),
    "iPhone 11 Pro": (1125, 2436), "iPhone XS": (1125, 2436),
    "iPhone 11 Pro Max": (1242, 2688), "iPhone XS Max": (1242, 2688),
    "iPhone 12 mini": (1080, 2340), "iPhone 13 mini": (1080, 2340),
    "iPhone 12": (1170, 2532), "iPhone 12 Pro": (1170, 2532), "iPhone 13": (1170, 2532),
    "iPhone 13 Pro": (1170, 2532), "iPhone 14": (1170, 2532), "iPhone 16e": (1170, 2532),
    "iPhone 12 Pro Max": (1284, 2778), "iPhone 13 Pro Max": (1284, 2778), "iPhone 14 Plus": (1284, 2778),
    "iPhone 14 Pro": (1179, 2556), "iPhone 15": (1179, 2556), "iPhone 15 Pro": (1179, 2556),
    "iPhone 16": (1179, 2556),
    "iPhone 14 Pro Max": (1290, 2796), "iPhone 15 Plus": (1290, 2796), "iPhone 15 Pro Max": (1290, 2796),
    "iPhone 16 Plus": (1290, 2796),
    "iPhone 16 Pro": (1206, 2622), "iPhone 17": (1206, 2622), "iPhone 17 Pro": (1206, 2622),
    "iPhone 16 Pro Max": (1320, 2868), "iPhone 17 Pro Max": (1320, 2868),
    "iPhone Air": (1260, 2736),
}
# Padding is never more than this: a bigger difference means the model's size isn't the
# stream's, and nothing is cut.
MOST_PADDING = 64


def screen_crop(model: str, frame: tuple[int, int]) -> tuple[int, int] | None:
    """The part of a frame of size `frame` that's the screen of an iPhone `model`, or None when
    it isn't known or doesn't fit (the phone turned, say, or a model Handset doesn't know)."""
    size = SCREENS.get(model.strip())
    if not size:
        return None
    for w, h in (size, size[::-1]):  # upright, or turned
        if 0 <= frame[0] - w <= MOST_PADDING and 0 <= frame[1] - h <= MOST_PADDING:
            return w, h
    return None


def helper_binary() -> str | None:
    """handset-ios, or HANDSET_IOS_HELPER (the tests' stand-in)."""
    wanted = os.environ.get("HANDSET_IOS_HELPER")
    if wanted:
        return wanted
    for place in HELPER_PLACES:
        if os.access(place, os.X_OK):
            return place
    return shutil.which("handset-ios")


def pairing_file(udid: str) -> str | None:
    path = os.path.join(PAIRINGS, f"{udid}.rppairing")
    return path if os.path.isfile(path) else None


def normalized(x: float, y: float, shown: tuple[int, int]) -> tuple[int, int]:
    """A point on the screen as it shows, in the digitizer's 0–65535 on each axis."""
    w, h = shown
    nx = round(min(max(x / max(w - 1, 1), 0.0), 1.0) * SPAN)
    ny = round(min(max(y / max(h - 1, 1), 0.0), 1.0) * SPAN)
    return nx, ny


# The phone's buttons, and the moves iPhones use where Android has a button. Swipes are
# (from, to, ms, hold ms) in fractions of the screen.
BUTTONS = {"home": "home", "power": "lock", "lock": "lock", "volume_up": "volume_up",
           "volume_down": "volume_down"}
KEYS = {"enter", "delete", "tab", "escape", "up", "down", "left", "right", "forward_delete"}
SWIPES = {
    # In from the left edge, quick, as a thumb does: a touch that starts on the screen's very
    # first pixel column, or moves slowly, isn't taken for the edge swipe.
    "back": ((0.004, 0.5), (0.72, 0.5), 220, 0),
    "recents": ((0.5, 0.995), (0.5, 0.55), 350, 600),  # up from the bottom, and hold
    "notifications": ((0.3, 0.0), (0.3, 0.61), 350, 0),  # down from the top left
    "quick_settings": ((0.88, 0.0), (0.88, 0.61), 350, 0),  # down from the top right
}


@dataclass
class Link:
    """What Handset needs to start the helper for an iPhone."""

    udid: str
    pairing: str
    address: str | None
    helper: str
    model: str = ""


def open_link(udid: str, address: str | None, model: str = "") -> Link | str:
    """The helper and the phone's pairing, or why the iPhone can't be shown, in words."""
    helper = helper_binary()
    if not helper:
        return "Handset's iPhone helper isn't built yet: run ./install.sh in Handset's folder."
    pairing = pairing_file(udid)
    if not pairing:
        return "Set the iPhone up with Ada first (Settings › Phones), with Wi-Fi on."
    return Link(udid, pairing, address, helper, model)


class IPhone:
    """One iPhone in the window, through the helper: its screen, touches, buttons and typing.
    Positions are the screen's pixels as it shows (`shown`), which the first picture gives.
    Callbacks as Android's: on_frame, on_turned, on_touch(live), on_ended(why),
    on_awake(on) (frames stopping means the screen went dark; one coming, that it's lit)."""

    def __init__(self, link: Link, on_frame, on_turned, on_touch, on_ended, on_awake=None,
                 on_status: Callable[[str], None] | None = None):
        self.link = link
        self.on_frame = on_frame
        self.on_turned = on_turned
        self.on_touch = on_touch
        self.on_ended = on_ended
        self.on_awake = on_awake
        self.on_status = on_status
        self.last_frame = 0.0
        self.dark_timer = 0
        # Whether the picture showing is dark (the window's screen.is_dark): frames stopping
        # alone may be a still screen, which must keep taking taps.
        self.looks_dark: Callable[[], bool] | None = None
        self.shown_at: tuple[int, int] | None = None
        self.shown = FIRST_SHOWN
        self.awake = True
        self.live_touch = False
        self.pressed = False
        self.stopped = False
        self.decoder: Decoder | None = None
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.error: str | None = None
        self.launching: str | None = None  # an app to open once the phone takes input
        self.on_launched: Callable[[bool, str], None] | None = None  # (opened, why not)

    def launch(self, bundle: str):
        """Opens an app on the phone, in front: now, or once the phone takes input."""
        if self.live_touch:
            self._send({"op": "launch", "bundle": bundle})
        else:
            self.launching = bundle

    def start(self):
        command = [self.link.helper, "stream", "--pairing", self.link.pairing, "--udid", self.link.udid]
        if self.link.address:
            command += ["--address", self.link.address]
        if DEBUG:
            command.append("--verbose")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
        except OSError as error:
            GLib.idle_add(self._ended, f"The iPhone helper didn't start: {error}")
            return
        threading.Thread(target=self._read_video, daemon=True).start()
        threading.Thread(target=self._read_status, daemon=True).start()
        self.dark_timer = GLib.timeout_add(1000, self._check_dark)

    def stop(self):
        self.stopped = True
        if self.dark_timer:
            GLib.source_remove(self.dark_timer)
            self.dark_timer = 0
        if self.pressed:
            self.release()
        process, self.process = self.process, None
        if process:
            try:
                process.stdin.close()  # the helper stops the phone's stream and goes
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        if self.decoder:
            self.decoder.stop()

    def show_at(self, width: int, height: int):
        """The screen shows this big in the window (the monitor's pixels)."""
        self.shown_at = (width, height)
        if self.decoder:
            self.decoder.show_at(width, height)

    def wake(self):
        """Home lights a dark iPhone (and on a lit one only goes home; the side button would lock
        it), so it's what a click on a dark screen presses. It counts as awake straight away, so
        the next press is a touch."""
        self._send({"op": "button", "name": "home"})
        self.last_frame = time.monotonic()
        if not self.awake:
            self.awake = True
            if self.on_awake:
                self.on_awake(True)

    # A finger on the glass, live: down, moves, up.
    def press(self, x: int, y: int):
        if self.live_touch:
            self.pressed = True
            self._send({"op": "down", **self._point(x, y)})

    def drag(self, x: int, y: int):
        if self.live_touch and self.pressed:
            self._send({"op": "move", **self._point(x, y)})

    def release(self):
        if self.pressed:
            self.pressed = False
            self._send({"op": "up"})

    def gesture(self, gesture: Gesture):
        """A whole gesture at once (a scroll, or anything before touches are ready)."""
        if not self.live_touch:
            return
        threading.Thread(target=self._play, args=(gesture,), daemon=True).start()

    def key(self, name: str):
        if name in BUTTONS:
            self._send({"op": "button", "name": BUTTONS[name]})
        elif name in KEYS:
            self._send({"op": "key", "name": name})
        elif name in SWIPES:
            (fx, fy), (tx, ty), ms, hold = SWIPES[name]
            w, h = self.shown
            threading.Thread(target=self._play, daemon=True, args=(
                Gesture("swipe", (round(fx * (w - 1)), round(fy * (h - 1))),
                        (round(tx * (w - 1)), round(ty * (h - 1))), ms), hold)).start()

    def type(self, text: str) -> int:
        """Types `text` on the phone's keyboard; answers how many characters it couldn't (the
        helper types what a US keyboard has)."""
        kept = "".join(c for c in text if 32 <= ord(c) < 127 or c in "\n\t")
        if kept:
            self._send({"op": "text", "text": kept})
        return len(text) - len(kept)

    def _point(self, x: int, y: int) -> dict:
        nx, ny = normalized(x, y, self.shown)
        return {"x": nx, "y": ny}

    def _play(self, gesture: Gesture, hold: int = 0):
        (x0, y0) = gesture.start
        if gesture.kind == "tap":
            self._send({"op": "down", **self._point(x0, y0)})
            time.sleep(0.05)
            self._send({"op": "up"})
            return
        if gesture.kind == "press":
            self._send({"op": "down", **self._point(x0, y0)})
            time.sleep(max(gesture.ms, 600) / 1000)
            self._send({"op": "up"})
            return
        (x1, y1) = gesture.end
        steps = max(4, gesture.ms // 16)
        self._send({"op": "down", **self._point(x0, y0)})
        for i in range(1, steps + 1):
            time.sleep(gesture.ms / 1000 / steps)
            self._send({"op": "move", **self._point(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)})
        if hold:
            time.sleep(hold / 1000)
        self._send({"op": "up"})

    def _send(self, command: dict):
        if command.get("op") != "move":
            debug("to the iPhone:", command)
        with self.lock:
            process = self.process
            if not process or not process.stdin:
                return
            try:
                process.stdin.write((json.dumps(command) + "\n").encode())
                process.stdin.flush()
            except OSError:
                pass

    def _read_video(self):
        process = self.process
        while not self.stopped:
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            decoder = self.decoder
            if decoder:
                decoder.push(chunk)
        process.wait()
        if not self.stopped:
            GLib.idle_add(self._ended, self.error or "The iPhone's screen stopped.")

    def _read_status(self):
        process = self.process
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                debug("iphone helper:", line)
                continue
            GLib.idle_add(self._event, event)

    def _event(self, event: dict):
        kind = event.get("event")
        debug("iphone helper says", event)
        if kind == "video" and self.decoder is None and not self.stopped:
            self.decoder = _decoder(str(event.get("codec") or "h265"), self._frame,
                                   crop=lambda size: screen_crop(self.link.model, size))
            if self.shown_at:
                self.decoder.show_at(*self.shown_at)
            if not self.decoder.start():
                self.error = "Install gstreamer1.0-libav to show the iPhone's screen."
        elif kind == "input":
            self.live_touch = bool(event.get("ready"))
            self.on_touch(self.live_touch)
            if self.live_touch and self.launching:
                self._send({"op": "launch", "bundle": self.launching})
                self.launching = None
        elif kind == "launch" and self.on_launched:
            opened = bool(event.get("ok"))
            self.on_launched(opened, "" if opened else launch_trouble(str(event.get("message") or "")))
        elif kind == "status" and self.on_status:
            self.on_status(str(event.get("text") or ""))
        elif kind == "error":
            self.error = str(event.get("message") or "The iPhone helper stopped.")
        return GLib.SOURCE_REMOVE

    def _check_dark(self):
        if self.stopped:
            self.dark_timer = 0
            return GLib.SOURCE_REMOVE
        quiet = self.last_frame and time.monotonic() - self.last_frame > DARK_AFTER_S
        if self.awake and quiet and self.looks_dark and self.looks_dark():
            self.awake = False
            if self.pressed:
                self.release()
            if self.on_awake:
                self.on_awake(False)
        return GLib.SOURCE_CONTINUE

    def _frame(self, texture):
        self.last_frame = time.monotonic()
        if not self.awake:
            self.awake = True
            if self.on_awake:
                self.on_awake(True)
        shown = (texture.get_width(), texture.get_height())
        if shown != self.shown:
            self.shown = shown
            if self.pressed:
                self.release()
            self.on_turned()
        self.on_frame(texture)

    def _ended(self, why: str):
        if not self.stopped:
            self.on_ended(plainly(why))
        return GLib.SOURCE_REMOVE


def launch_trouble(why: str) -> str:
    """Why an app didn't open, in words: mostly a locked phone."""
    if "lock" in why.lower() or "FBSOpenApplicationErrorDomain" in why:
        return "Unlock the iPhone to open it."
    return why or "The iPhone didn't open it."


def list_apps(link: Link, icons: str) -> list[tuple[str, str, str | None]] | str:
    """The phone's apps as its home screen has them, (name, bundle id, icon file or None), with
    their icons saved in the folder `icons`; or why not, in words. Slow: run it off the main loop."""
    command = [link.helper, "apps", "--pairing", link.pairing, "--udid", link.udid, "--icons", icons]
    if link.address:
        command += ["--address", link.address]
    os.makedirs(icons, exist_ok=True)
    try:
        result = subprocess.run(command, capture_output=True, timeout=90, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return f"The iPhone helper didn't list its apps ({error})."
    apps = []
    for line in result.stdout.decode(errors="replace").splitlines():
        try:
            app = json.loads(line)
        except ValueError:
            continue
        if isinstance(app, dict) and app.get("bundle"):
            apps.append((str(app.get("name") or app["bundle"]), str(app["bundle"]), app.get("icon") or None))
    if not apps and result.returncode:
        for line in result.stderr.decode(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "error":
                return plainly(str(event.get("message") or ""))
        return "The iPhone didn't list its apps."
    return apps


def plainly(why: str) -> str:
    """The helper's reasons, in the words a person needs: most mean the phone isn't reachable."""
    gone = ("No route to host", "adapter closed", "Connection refused", "timed out", "Network is unreachable",
            "went to sleep", "device socket io failed", "unplugged")
    if any(word in why for word in gone):
        return "It isn't plugged in, and it's asleep or off Wi-Fi. Plug it in or unlock it, and it shows here again."
    return why
