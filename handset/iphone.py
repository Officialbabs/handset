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
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from dataclasses import dataclass

from gi.repository import Gio, GLib

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
# The pairings that let an iPhone be reached over Wi-Fi, by its UDID: the ones Ada's desktop
# made, then Handset's own.
CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
PAIRINGS = (os.path.join(CONFIG, "ada", "phones"), os.path.join(CONFIG, "handset", "pairings"))
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
# A paste waits this long at most for the phone to say its clipboard is set.
SET_WAIT_MS = 2500
# Images on the clipboard, by their type here and on the phone.
UTIS = {"image/png": "public.png", "image/jpeg": "public.jpeg"}


def clip_folder() -> str:
    """Where images on the clipboard wait between the computer and the phones (only this person's)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/handset-{os.getuid()}"
    folder = os.path.join(runtime, "handset-clipboard")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder
# A helper told to stop has this long to end its streams on the phone (over Wi-Fi too) and go.
STOP_FOR_S = 5
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
    """The iPhone's Wi-Fi pairing, if it has one (without, it's reached by its cable only)."""
    for folder in PAIRINGS:
        path = os.path.join(folder, f"{udid}.rppairing")
        if os.path.isfile(path):
            return path
    return None


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
    # Along the bottom edge to the right: the app used before this one.
    "previous_app": ((0.2, 0.994), (0.8, 0.994), 180, 0),
    "notifications": ((0.3, 0.0), (0.3, 0.61), 350, 0),  # down from the top left
    "quick_settings": ((0.88, 0.0), (0.88, 0.61), 350, 0),  # down from the top right
}


UNSAID = "?"  # FilesSession.was when the phone didn't say which app it was in

@dataclass
class Link:
    """What Handset needs to start the helper for an iPhone."""

    udid: str
    pairing: str | None  # for Wi-Fi; by cable the phone's trust in this computer is enough
    address: str | None
    helper: str
    model: str = ""

    def args(self, command: str) -> list[str]:
        """The helper's command line for `command` on this phone."""
        return [self.helper, command, "--udid", self.udid] + (["--pairing", self.pairing] if self.pairing else [])


def open_link(udid: str, address: str | None, model: str = "") -> Link | str:
    """The helper and the phone's pairing, or why the iPhone can't be shown, in words."""
    helper = helper_binary()
    if not helper:
        return "Handset's iPhone helper isn't built yet: run ./install.sh in Handset's folder."
    from .service import choices

    if udid in choices()[1]:
        return Link(udid, None, None, helper, model)  # kept to its cable in Handset: no Wi-Fi
    # The helper takes the phone only at the address it's given: one from another network (the
    # Wi-Fi it was last seen on) would turn it away here, so the helper looks for it unhelped.
    if address and not on_this_network(address):
        debug("the iPhone's last address isn't on this computer's networks:", address)
        address = None
    return Link(udid, pairing_file(udid), address, helper, model)


def local_networks() -> list:
    """This computer's IPv4 networks now (192.168.18.0/24, say), from `ip`."""
    import ipaddress

    try:
        listed = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"], capture_output=True,
                                text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    networks = []
    for found in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", listed):
        networks.append(ipaddress.ip_interface(found.group(1)).network)
    return networks


def on_this_network(address: str) -> bool:
    """Whether an address is on one of this computer's networks; yes when that can't be told (an
    IPv6 address, no `ip`)."""
    import ipaddress

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    networks = local_networks()
    if ip.version != 4 or not networks:
        return True
    return any(ip in network for network in networks)


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
        # Whether someone else shares the phone's screen now (Ada working on it): if not, streams
        # left on the phone from before are stopped when this one starts.
        self.shared: Callable[[], bool] = lambda: False
        self.launching: str | None = None  # an app to open once the phone takes input
        self.on_launched: Callable[[bool, str], None] | None = None  # (opened, why not)

    def fingers(self, points: list[tuple[int, int]]):
        """Several fingers at once, as the screen shows (a pinch); none: all up."""
        if self.live_touch:
            self._send({"op": "fingers", "points": [self._point(x, y) for x, y in points]})

    def shortcut(self, letter: str, shift: bool = False):
        """Command(+Shift)+letter: select all, copy, cut, paste, undo, redo."""
        self._send({"op": "shortcut", "key": letter, "shift": shift})

    def launch(self, bundle: str):
        """Opens an app on the phone, in front: now, or once the phone takes input."""
        if self.live_touch:
            self._send({"op": "launch", "bundle": bundle})
        else:
            self.launching = bundle

    def start(self):
        from .pairing import ddi_folder
        from .service import has_service

        command = self.link.args("stream")
        images = None if has_service() else ddi_folder()
        if images:
            command += ["--images", images]  # a phone restarted has lost it: put back
        if not self.shared():
            command.append("--alone")
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
                process.stdin.close()  # the helper ends its streams on the phone, and goes
                process.wait(timeout=STOP_FOR_S)
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
        if name == "recents":
            # The app switcher: home pressed twice, as on an iPhone with a home button. (Its
            # gesture, a swipe up held halfway, is too often taken for going home.)
            self._send({"op": "button", "name": "home"})
            self._send({"op": "button", "name": "home"})
        elif name in BUTTONS:
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
    command = link.args("apps") + ["--icons", icons]
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
    if '"code": Integer(9022)' in why or "call is currently in progress" in why:
        return "It's on a call: its screen shows here again when the call ends."
    said = re.search(r'"NSLocalizedDescription": String\("((?:[^"\\]|\\.)*)"\)', why)
    if said:
        why = said.group(1)  # the phone's own words, not the whole reply
    gone = ("No route to host", "adapter closed", "Connection refused", "timed out", "Network is unreachable",
            "went to sleep", "device socket io failed", "unplugged")
    if any(word in why for word in gone):
        return "It isn't plugged in, and it's asleep or off Wi-Fi. Plug it in or unlock it, and it shows here again."
    return why


class ClipboardLink:
    """The iPhone's clipboard, both ways, through the helper (`handset-ios clipboard`): `on_text`
    on GLib's main loop for each text copied on the phone, set(text) to put text on it. As
    android.ClipboardLink: it starts again by itself if the phone sleeps or goes."""

    def __init__(self, link: Link, on_text: Callable[[str], None],
                 on_image: Callable[[str, str], None] | None = None):
        self.link = link
        self.on_text = on_text
        self.on_image = on_image  # (file, mime): an image copied on the phone
        self.process: subprocess.Popen | None = None
        self.stopped = False
        self.last: str | None = None
        self.lock = threading.Lock()
        self.sequence = 0
        self.waiting: dict[int, Callable[[], None]] = {}  # a set's number → the paste waiting for it

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stopped = True
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
            process.wait()

    def set(self, text: str, then: Callable[[], None] | None = None):
        """`text` on the phone's clipboard; `then` once it's there (the helper says so, or it was)."""
        process = self.process
        if not text or text == self.last or not process or process.poll() is not None:
            if then:
                then()
            return
        self.last = text
        self._ask({"op": "set", "text": text}, then, SET_WAIT_MS)

    def set_image(self, path: str, mime: str, then: Callable[[], None] | None = None):
        """An image on the phone's clipboard (in memory there: never in Photos or Files); `then`
        once it's there."""
        process = self.process
        if not process or process.poll() is not None:
            if then:
                then()
            return
        self.last = None  # text copied next is news again
        self._ask({"op": "set-image", "path": path, "type": UTIS.get(mime, "public.png")}, then, SET_WAIT_MS * 4)

    def _ask(self, command: dict, then: Callable[[], None] | None, wait_ms: int):
        """A set, numbered: the helper says that number back once it's on the phone, and the
        paste waiting for it (`then`) goes then (or after `wait_ms`, if it never says)."""
        self.sequence += 1
        if then:
            self.waiting[self.sequence] = then
            GLib.timeout_add(wait_ms, self._set_done, self.sequence)
        try:
            with self.lock:
                self.process.stdin.write((json.dumps({**command, "id": self.sequence}) + "\n").encode())
                self.process.stdin.flush()
        except (OSError, AttributeError):
            self._set_done(self.sequence)

    def _set_done(self, number: int):
        then = self.waiting.pop(number, None)
        if then:
            then()
        return GLib.SOURCE_REMOVE

    def _run(self):
        delay = 3
        while not self.stopped:
            started = time.monotonic()
            command = self.link.args("clipboard") + ["--clip-dir", clip_folder()]
            if self.link.address:
                command += ["--address", self.link.address]
            try:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL)
            except OSError:
                return
            for raw in self.process.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event") == "clipboard" and event.get("set"):
                    GLib.idle_add(self._set_done, event.get("id"))
                    continue
                if isinstance(event, dict) and event.get("event") == "clipboard" and event.get("image"):
                    if self.on_image:
                        GLib.idle_add(self.on_image, event["image"], event.get("type") or "image/png")
                    continue
                text = event.get("text") if isinstance(event, dict) and event.get("event") == "clipboard" else None
                if text and text != self.last:
                    self.last = text
                    GLib.idle_add(self.on_text, text)
            if self.process:
                self.process.wait()
            if self.stopped:
                return
            delay = 3 if time.monotonic() - started > 60 else min(delay * 2, 60)
            time.sleep(delay)


class FilesSession:
    """An iPhone's files, through the helper's `files` session: its media folders (photos,
    recordings…), and under "On My iPhone" the apps that share their Documents (VLC, LocalSend…),
    which the iPhone's Files app shows under On My iPhone too. One helper while it's open, a
    command at a time. Slow calls: off the main loop. The same calls as android.AdbFiles."""

    top = "/"
    downloads = "/Downloads"  # iTunes' own, which nothing on the iPhone shows: for nothing new
    apps_root = "/On My iPhone"
    # Which apps share their files is asked once a day (asking each app takes a while).
    SHARING_FOR_S = 24 * 3600
    # Where dropped files go, the first of these that shares its files: videos and sound into VLC
    # (they show in its library), the rest into a file-transfer app's, a browser's at least.
    FOR_MEDIA = ("org.videolan.vlc-ios",)
    FOR_FILES = ("org.localsend.localsendApp", "com.google.chrome.ios")
    # Photos and videos go into Photos, into its album Handset: put in a folder of their own (in a
    # file-sharing app's files), then saved there by a shortcut on the iPhone, "Handset Import"
    # (its two steps: Get Contents of Folder, that folder; Save to Photo Album, Handset), run by its
    # URL. Once Photos has them, the copies go, and the window takes the phone back to the app it
    # was in (`opened_shortcuts`). (Shortcuts' x-callback-url no longer calls back since iOS 26.5.)
    PHOTOS_FOLDER = "Handset"
    PHOTOS_SHORTCUT = "Handset Import"
    PHOTOS_URL = "shortcuts://run-shortcut?name=Handset%20Import"
    PHOTOS_WAIT_S = 60

    def __init__(self, link: Link):
        self.link = link
        self.opened_shortcuts = False  # Shortcuts came in front for a drop: the phone goes back after
        # The app Shortcuts came in front of, as the phone said: its bundle id; None, the home
        # screen; UNSAID when it didn't say.
        self.was: str | None = UNSAID
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self._sharing: dict[str, str] | None = None

    def sharing(self) -> dict[str, str]:
        """The iPhone's apps that share their Documents: bundle id → name. Kept for a day."""
        if self._sharing is not None:
            return self._sharing
        from . import apps

        names = {app.app: app.name for app in apps.entries(self.link.udid)}
        kept = os.path.join(apps.cache_dir(self.link.udid), "sharing.json")
        try:
            with open(kept) as file:
                saved = json.load(file)
            if time.time() - saved.get("time", 0) < self.SHARING_FOR_S:
                self._sharing = {b: names.get(b, b) for b in saved.get("bundles", [])}
                return self._sharing
        except (OSError, ValueError, AttributeError):
            pass
        answer = self._ask({"op": "sharing", "bundles": sorted(names)}) if names else {"bundles": []}
        if isinstance(answer, str):
            return {}
        bundles = [b for b in answer.get("bundles", []) if isinstance(b, str)]
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        with open(kept, "w") as file:
            json.dump({"time": time.time(), "bundles": bundles}, file)
        self._sharing = {b: names.get(b, b) for b in bundles}
        return self._sharing

    def display(self, path: str) -> list[str]:
        """A path's folders as a person reads them: an app's by its name."""
        parts = [part for part in path.split("/") if part]
        if parts[:1] == [self.apps_root.strip("/")] and len(parts) >= 2:
            parts[1] = self.sharing().get(parts[1], parts[1])
        return parts

    def drop_folder(self, paths: list[str]) -> tuple[str, str]:
        """Where files dropped on the iPhone go, and how a person finds them there: an app's
        files the Files app shows (VLC for videos and sound), else its media Downloads (seen
        nowhere on the iPhone: said so)."""
        sharing = self.sharing()
        kinds = [Gio.content_type_guess(path, None)[0] or "" for path in paths]
        media = bool(kinds) and all(k.startswith(("video/", "audio/")) for k in kinds)
        wanted = (self.FOR_MEDIA if media else ()) + self.FOR_FILES + self.FOR_MEDIA
        bundle = next((b for b in wanted if b in sharing), next(iter(sharing), None))
        if bundle is None:
            return self.downloads, "media Downloads (no app on it shares its files, so nothing on it shows them)"
        return f"{self.apps_root}/{bundle}", f"Files › On My iPhone › {sharing[bundle]}"

    def deliver(self, paths: list[str]) -> tuple[list[str], str, str | None]:
        """Files dropped on the iPhone, where it shows them: photos and videos in Photos (its
        album Handset), the rest in an app's files. What went, where (in words), and why some
        didn't; where is "set up" when the shortcut that puts them in Photos isn't there."""
        media = [p for p in paths if (Gio.content_type_guess(p, None)[0] or "").startswith(("image/", "video/"))]
        others = [p for p in paths if p not in media]
        sent, places, trouble = [], [], None
        if media:
            folder = self.photos_folder()
            if folder is None:
                others = paths
            else:
                went, trouble = self.send_files(media, folder)
                sent += went
                if went:
                    imported = self.to_photos(folder, len(went))
                    places.append("Photos › Albums › Handset" if imported is True else imported)
        if others:
            folder, where = self.drop_folder(others)
            went, why = self.send_files(others, folder)
            sent += went
            trouble = trouble or why
            if went:
                places.append(where)
        return sent, " and ".join(places), trouble

    def photos_folder(self) -> str | None:
        """The folder photos wait in for the shortcut (made if need be), or None: no app on the
        iPhone shares its files."""
        sharing = self.sharing()
        bundle = next((b for b in self.FOR_FILES + self.FOR_MEDIA if b in sharing), next(iter(sharing), None))
        if bundle is None:
            return None
        folder = f"{self.apps_root}/{bundle}/{self.PHOTOS_FOLDER}"
        self._ask({"op": "mkdir", "path": folder})  # there already: fine
        return folder

    def to_photos(self, folder: str, count: int) -> bool | str:
        """The folder's photos into Photos, by the shortcut: True once Photos has them (and
        they're gone from the folder); otherwise where they are, and what to do, in words."""
        app = self.display(folder)[1] if len(self.display(folder)) > 1 else "an app"
        waiting = f"Files › On My iPhone › {app} › {self.PHOTOS_FOLDER}"
        before = self.photo_count()
        names = self._ask({"op": "names", "path": folder})
        there = [n for n in names.get("names", []) if not n.startswith(".")] if isinstance(names, dict) else []
        opened = self._ask({"op": "open-url", "bundle": "com.apple.shortcuts", "url": self.PHOTOS_URL, "front": True})
        if isinstance(opened, str):
            return f"{waiting} (unlock the iPhone to put them in Photos)"
        self.opened_shortcuts = True
        self.was = opened.get("was", UNSAID)
        deadline = time.monotonic() + self.PHOTOS_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(1.5)
            if before is not None and (self.photo_count() or 0) >= before + count:
                for name in there:
                    self._ask({"op": "remove", "path": f"{folder}/{name}"})
                return True
        return "set up"

    def go_back(self, app: str | None = None) -> bool:
        """The iPhone back in the app it was in before Shortcuts came in front (or `app`): whether
        it went. Not for the home screen (no app to open), nor when the phone didn't say."""
        bundle = app or self.was
        if not bundle or bundle == UNSAID:
            return False
        answer = self._ask({"op": "launch", "bundle": bundle})
        return isinstance(answer, dict) and answer.get("event") == "launched"

    def photo_count(self) -> int | None:
        """How many files Photos keeps in its newest folders (DCIM/1xxAPPLE: a new one each
        thousand or so), to see new ones come; None if it can't be read."""
        top = self._ask({"op": "names", "path": "/DCIM"})
        if isinstance(top, str):
            return None
        folders = sorted(n for n in top.get("names", []) if n[:3].isdigit())[-2:]
        total = 0
        for name in folders:
            inside = self._ask({"op": "names", "path": f"/DCIM/{name}"})
            if isinstance(inside, dict):
                total += sum(1 for n in inside.get("names", []) if not n.startswith("."))
        return total

    def _ask(self, command: dict) -> dict | str:
        """One command's answer, or why there's none, in words."""
        with self.lock:
            if not self.process or self.process.poll() is not None:
                started = self.link.args("files")
                if self.link.address:
                    started += ["--address", self.link.address]
                try:
                    self.process = subprocess.Popen(started, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE)
                except OSError as error:
                    return str(error)
            try:
                self.process.stdin.write((json.dumps(command) + "\n").encode())
                self.process.stdin.flush()
                answer = None
                while answer is None:
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    try:
                        answer = json.loads(line)
                    except ValueError:
                        # Not an answer: a word from the crate under the helper (on the same output).
                        debug("iphone files helper:", line.decode(errors="replace").strip())
            except OSError as error:
                return str(error)
            if answer is None:
                why = self.process.stderr.read().decode(errors="replace") if self.process.poll() is not None else ""
                message = next((json.loads(l).get("message") for l in why.splitlines()
                                if l.startswith("{") and '"error"' in l), None)
                self.process = None
                return plainly(message or "The iPhone didn't answer.")
            if answer.get("event") == "error":
                return str(answer.get("message") or "It didn't work.")
            return answer

    def list_folder(self, folder: str, hidden: bool = False) -> list | str:
        from .android import PhoneFile

        if folder.rstrip("/") == self.apps_root:
            return sorted((PhoneFile(name, f"{self.apps_root}/{bundle}", True, 0, time.time())
                           for bundle, name in self.sharing().items()), key=lambda f: f.name.casefold())
        answer = self._ask({"op": "list", "path": folder})
        if isinstance(answer, str):
            return answer
        found = [PhoneFile(e["name"], f"{folder.rstrip('/')}/{e['name']}", bool(e.get("folder")),
                           int(e.get("size") or 0), float(e.get("modified") or 0))
                 for e in answer.get("entries", []) if e.get("name") and (hidden or not e["name"].startswith("."))]
        found = sorted(found, key=lambda f: (not f.folder, f.name.casefold()))
        if folder.rstrip("/") == "" and self.sharing():
            found.insert(0, PhoneFile("On My iPhone", self.apps_root, True, 0, time.time()))
        return found

    def find_files(self, folder: str, text: str) -> list | str:
        """What's named like `text` (any case) in the folder and the folders in it."""
        from .android import PhoneFile

        answer = self._ask({"op": "find", "path": folder, "text": text})
        if isinstance(answer, str):
            return answer
        found = [PhoneFile(e["name"], e["path"], bool(e.get("folder")), int(e.get("size") or 0),
                           float(e.get("modified") or 0))
                 for e in answer.get("entries", []) if e.get("name") and e.get("path")]
        return sorted(found, key=lambda f: (not f.folder, f.name.casefold()))

    def fetch_file(self, path: str, folder: str) -> str | None:
        name = os.path.basename(path)
        stem, dot, ext = name.rpartition(".") if "." in name.strip(".") else (name, "", "")
        target, number = os.path.join(folder, name), 2
        while os.path.exists(target):
            target = os.path.join(folder, f"{stem} ({number}){dot}{ext}")
            number += 1
        os.makedirs(folder, exist_ok=True)
        answer = self._ask({"op": "fetch", "path": path, "to": target})
        return target if isinstance(answer, dict) and os.path.exists(target) else None

    def send_files(self, paths: list[str], folder: str) -> tuple[list[str], str | None]:
        listed = self.list_folder(folder)
        taken = {f.name for f in listed} if isinstance(listed, list) else set()
        sent, trouble = [], None
        for path in paths:
            if not os.path.isfile(path):
                trouble = "folders don't go to an iPhone"
                continue
            name = os.path.basename(path)
            stem, dot, ext = name.rpartition(".") if "." in name.strip(".") else (name, "", "")
            candidate, number = name, 2
            while candidate in taken:
                candidate = f"{stem} ({number}){dot}{ext}"
                number += 1
            taken.add(candidate)
            target = f"{folder.rstrip('/')}/{candidate}"
            answer = self._ask({"op": "send", "from": path, "path": target})
            if isinstance(answer, str):
                trouble = answer
            else:
                sent.append(target)
        return sent, trouble

    def stop(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        if process:
            for pipe in (process.stdout, process.stderr):
                pipe.close()
