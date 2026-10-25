"""An Android phone over adb, the way Ada's phone service reaches it: the same adb server and the
same "Allow this computer" the person already gave. Handset takes the screen as live video and
gives the phone touches through a virtual touchscreen of its own, which the phone, and Ada's
service, take for a finger on the glass.

Everything that waits on the phone runs off GLib's main loop; results come back through
GLib.idle_add.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from queue import Queue

import gi
from gi.repository import GLib  # noqa: E402

from .gestures import Gesture  # noqa: E402
from .log import DEBUG, debug  # noqa: E402

if TYPE_CHECKING:
    from .decoder import Decoder


def _decoder(*args, **kwargs) -> Decoder:
    """A video decoder: GStreamer is loaded only once a screen shows (not to list apps, say)."""
    from .decoder import Decoder

    return Decoder(*args, **kwargs)


# Where the phone-side tools' own complaints go: nowhere, or Handset's stderr when debugging.
TOOL_ERRORS = None if DEBUG else subprocess.DEVNULL

# The scrcpy package's server: live video with no time limit that stays steady through
# rotations. Without it, Android's own screenrecord.
SCRCPY_SERVER = "/usr/share/scrcpy/scrcpy-server"
# Where it goes on the phone: Handset's own name, so a running scrcpy's copy is never touched.
PHONE_SERVER = "/data/local/tmp/handset-scrcpy-server.jar"
# The video's longest edge, in the phone's pixels, when the window doesn't say (0: the screen's
# own size). The window asks for what its monitor can show (stream_edge).
MAX_EDGE = 0
# Enough for a whole phone screen at 60 frames a second without blocky moving text, over Wi-Fi;
# a cable carries twice that, for moving text as sharp as still.
BIT_RATE = 12_000_000
CABLE_BIT_RATE = 24_000_000
# Frames a second asked of the phone when the window doesn't say (it asks for its monitor's).
FRAME_RATE = 60


def bit_rate(serial: str) -> int:
    """What the phone's video may use: more over a cable than over Wi-Fi (an adb serial over
    Wi-Fi is an address and port, or the phone's wireless debugging name)."""
    return BIT_RATE if ":" in serial or "_adb-tls-connect" in serial else CABLE_BIT_RATE


def stream_edge(monitor_pixels: int, natural: tuple[int, int]) -> int:
    """The video's longest edge for a monitor whose shorter side is `monitor_pixels` (its own
    pixels; an upright phone's length runs along it): twice that, so the window, drawing it
    smaller with mipmaps, stays sharp even at full screen; never more than the phone's screen; a
    multiple of 8, as encoders like. 0: the screen's own size."""
    edge = monitor_pixels * 2 // 8 * 8
    return 0 if edge <= 0 or edge >= max(natural) else edge
# How often the screen's rotation, and whether it's on, are checked while it shows.
ROTATION_POLL_S = 0.75
# After Handset wakes the phone, a reading that it's still off is believed only this much later:
# waking takes a moment, and meanwhile the person's next press must reach the phone.
WAKING_S = 2.0
# One look at the screen in one adb call: its rotation, and whether it's on.
SCREEN_STATE = ("dumpsys input | grep -m1 'Viewport INTERNAL'; dumpsys power | grep -m1 'mWakefulness='; "
                "dumpsys window | grep -m1 'isKeyguardShowing='")


def adb_binary() -> str | None:
    """adb, or HANDSET_ADB (the tests' stand-in)."""
    return os.environ.get("HANDSET_ADB") or shutil.which("adb")


def adb_command(serial: str | None, *args: str) -> list[str]:
    binary = adb_binary()
    if not binary:
        raise FileNotFoundError("adb isn't installed")
    return [binary] + (["-s", serial] if serial else []) + list(args)


def adb(serial: str | None, *args: str, timeout: float = 10) -> str:
    """One adb command's output; raises OSError or subprocess.SubprocessError when it fails."""
    result = subprocess.run(adb_command(serial, *args), capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise subprocess.SubprocessError(result.stderr.decode(errors="replace").strip() or "adb failed")
    return result.stdout.decode(errors="replace")


def shell(serial: str, command: str, timeout: float = 10) -> str:
    return adb(serial, "shell", command, timeout=timeout)


def parse_devices(text: str) -> list[str]:
    """The serials `adb devices` lists as ready, leaving out offline and unauthorized ones."""
    serials = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def find_serial(phone_id: str, address: str | None = None, port: int | None = None) -> str | None:
    """The adb serial of the phone Ada's service calls `phone_id`, by cable or over Wi-Fi, from
    one look at everything adb has. By cable the serial is the id, and it's taken first; over
    Wi-Fi adb lists the phone by address, or by the name wireless debugging gives it (which
    holds the id). Failing those, every other phone is asked its serial number, all at once."""
    try:
        serials = parse_devices(adb(None, "devices", timeout=5))
    except (OSError, subprocess.SubprocessError):
        return None
    if phone_id in serials:
        return phone_id
    if address and f"{address}:{port or 5555}" in serials:
        return f"{address}:{port or 5555}"
    named = [serial for serial in serials if serial.startswith(f"adb-{phone_id}-")]
    if named:
        return named[0]

    def number(serial: str) -> str | None:
        try:
            return shell(serial, "getprop ro.serialno", timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    if not serials:
        return None
    pool = ThreadPoolExecutor(max_workers=len(serials))
    try:
        asked = {pool.submit(number, serial): serial for serial in serials}
        return next((asked[done] for done in as_completed(asked) if done.result() == phone_id), None)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def parse_wm_size(text: str) -> tuple[int, int] | None:
    """The screen's upright size from `wm size` ("Physical size: 1080x2400")."""
    match = re.search(r"Physical size:\s*(\d+)\s*x\s*(\d+)", text)
    return (int(match[1]), int(match[2])) if match else None


_ROTATIONS = {"0": 0, "1": 90, "2": 180, "3": 270, "90": 90, "180": 180, "270": 270}


def parse_rotation(text: str) -> int | None:
    """The built-in screen's rotation in degrees, from the input system's viewport line
    (`dumpsys input`: "Viewport INTERNAL: … orientation=1 …", or "orientation=ROTATION_90")."""
    for line in text.splitlines():
        if "Viewport INTERNAL" in line:
            match = re.search(r"orientation=(?:ROTATION_)?(\d+)", line)
            if match and match[1] in _ROTATIONS:
                return _ROTATIONS[match[1]]
    return None


def parse_locked(text: str) -> bool | None:
    """Whether the lock screen shows, from `dumpsys window` ("isKeyguardShowing=true")."""
    match = re.search(r"isKeyguardShowing=(true|false)", text)
    return match[1] == "true" if match else None


def parse_awake(text: str) -> bool | None:
    """Whether the screen is on, from `dumpsys power` ("mWakefulness=Awake"). Asleep and Dozing
    are off (a dozing phone shows at most its clock, and sends no picture); Dreaming is a screen
    saver, on."""
    match = re.search(r"mWakefulness=(\w+)", text)
    if not match:
        return None
    return match[1] in ("Awake", "Dreaming")


def shown_size(natural: tuple[int, int], rotation: int) -> tuple[int, int]:
    """The screen's size as it shows now, turned `rotation` degrees from upright."""
    w, h = natural
    return (h, w) if rotation in (90, 270) else (w, h)


def to_natural(x: int, y: int, natural: tuple[int, int], rotation: int) -> tuple[int, int]:
    """A point on the screen as it shows now, in the upright screen's pixels, which is how a
    touchscreen counts (Android turns its touches with the screen)."""
    w, h = natural
    if rotation == 90:
        return w - 1 - y, x
    if rotation == 180:
        return w - 1 - x, h - 1 - y
    if rotation == 270:
        return y, h - 1 - x
    return x, y


def even_size(natural: tuple[int, int], rotation: int, edge: int = MAX_EDGE) -> tuple[int, int]:
    """The video size for screenrecord: the screen as it shows, its longest edge at most `edge`
    (0: its own size), both sides even (encoders refuse odd ones)."""
    w, h = shown_size(natural, rotation)
    scale = min(1.0, edge / max(w, h)) if edge else 1.0
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


# ---- Touches -------------------------------------------------------------------------------

# uinput hands each configuration entry's type to the kernel as the ioctl's number, so a type
# its labels lack still goes through: 110 is UI_SET_PROPBIT. INPUT_PROP_DIRECT makes the device
# a touchscreen; without it Android takes it for a touchpad and shows a pointer.
UI_SET_PROPBIT = 110
INPUT_PROP_DIRECT = 1
# pid.codes' test vendor and product: no device file on the phone matches them.
TOUCH_VID, TOUCH_PID = 0x1209, 0x0001
TOUCH_NAME = "Handset touch"


def _axis(code: str, maximum: int) -> dict:
    return {"code": code, "info": {"value": 0, "minimum": 0, "maximum": maximum,
                                   "fuzz": 0, "flat": 0, "resolution": 0}}


def touch_device(natural: tuple[int, int]) -> dict:
    """uinput's "register" for a one-finger touchscreen the size of the upright screen."""
    w, h = natural
    return {
        "id": 1, "command": "register", "name": TOUCH_NAME, "vid": TOUCH_VID, "pid": TOUCH_PID,
        "bus": "usb",
        "configuration": [
            {"type": "UI_SET_EVBIT", "data": ["EV_KEY", "EV_ABS"]},
            {"type": "UI_SET_KEYBIT", "data": ["BTN_TOUCH"]},
            {"type": "UI_SET_ABSBIT", "data": ["ABS_MT_SLOT", "ABS_MT_TRACKING_ID",
                                               "ABS_MT_POSITION_X", "ABS_MT_POSITION_Y"]},
            {"type": UI_SET_PROPBIT, "data": [INPUT_PROP_DIRECT]},
        ],
        "abs_info": [_axis("ABS_MT_SLOT", 9), _axis("ABS_MT_TRACKING_ID", 65535),
                     _axis("ABS_MT_POSITION_X", w - 1), _axis("ABS_MT_POSITION_Y", h - 1)],
    }


def down_events(tracking: int, x: int, y: int) -> list:
    return ["EV_ABS", "ABS_MT_SLOT", 0, "EV_ABS", "ABS_MT_TRACKING_ID", tracking,
            "EV_ABS", "ABS_MT_POSITION_X", x, "EV_ABS", "ABS_MT_POSITION_Y", y,
            "EV_KEY", "BTN_TOUCH", 1, "EV_SYN", "SYN_REPORT", 0]


def move_events(x: int, y: int) -> list:
    return ["EV_ABS", "ABS_MT_POSITION_X", x, "EV_ABS", "ABS_MT_POSITION_Y", y,
            "EV_SYN", "SYN_REPORT", 0]


def up_events() -> list:
    return ["EV_ABS", "ABS_MT_TRACKING_ID", -1, "EV_KEY", "BTN_TOUCH", 0, "EV_SYN", "SYN_REPORT", 0]


class Touch:
    """A virtual touchscreen on the phone, made with Android's own `uinput` tool over adb and kept
    while Handset shows the phone; it goes when its uinput ends. Positions are the upright
    screen's pixels. `on_ready()` once the phone has it; `on_failed()` if it can't be made."""

    READY_AFTER_SYNC_S = 0.4  # uinput answers before Android has added the device
    GIVE_UP_S = 8.0

    def __init__(self, serial: str, natural: tuple[int, int], on_ready: Callable[[], None],
                 on_failed: Callable[[], None]):
        self.serial = serial
        self.natural = natural
        self.on_ready = on_ready
        self.on_failed = on_failed
        self.process: subprocess.Popen | None = None
        self.ready = False
        self.ended = False
        self.tracking = 0
        self.lock = threading.Lock()

    def start(self):
        try:
            self.process = subprocess.Popen(adb_command(self.serial, "shell", "-T", "uinput", "-"),
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=TOOL_ERRORS)
        except OSError:
            GLib.idle_add(self._failed)
            return
        self._send(touch_device(self.natural))
        self._send({"id": 1, "command": "sync", "syncToken": "handset-ready"})
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._deadline, daemon=True).start()

    def stop(self):
        self.ended = True
        process, self.process = self.process, None
        if process:
            try:
                process.stdin.close()  # uinput takes its device away at the end of its input
            except OSError:
                pass
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.kill()

    def down(self, x: int, y: int):
        self.tracking = (self.tracking + 1) % 65536
        self._inject(down_events(self.tracking, x, y))

    def move(self, x: int, y: int):
        self._inject(move_events(x, y))

    def up(self):
        self._inject(up_events())

    def _inject(self, events: list):
        if self.ready:
            self._send({"id": 1, "command": "inject", "events": events})

    def _send(self, command: dict):
        with self.lock:
            if not self.process or not self.process.stdin:
                return
            try:
                self.process.stdin.write((json.dumps(command) + "\n").encode())
                self.process.stdin.flush()
            except OSError:
                pass

    def _listen(self):
        process = self.process
        for line in iter(process.stdout.readline, b""):
            debug("uinput says", line.decode(errors="replace").strip())
            if b"handset-ready" in line and not self.ready:
                time.sleep(self.READY_AFTER_SYNC_S)
                GLib.idle_add(self._became_ready)
        if not self.ended:
            GLib.idle_add(self._failed)

    def _deadline(self):
        time.sleep(self.GIVE_UP_S)
        if not self.ready and not self.ended:
            # No answer to the sync, yet uinput still runs: some versions name it differently.
            if self.process and self.process.poll() is None:
                GLib.idle_add(self._became_ready)

    def _became_ready(self):
        if not self.ready and not self.ended:
            debug("virtual touchscreen ready", self.natural)
            self.ready = True
            self.on_ready()
        return GLib.SOURCE_REMOVE

    def _failed(self):
        self.ready = False
        if not self.ended:
            debug("virtual touchscreen failed")
            self.ended = True
            self.on_failed()
        return GLib.SOURCE_REMOVE


# ---- Keys, text and taps by command -------------------------------------------------------

KEYS = {
    "back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "recents": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER", "delete": "KEYCODE_DEL", "forward_delete": "KEYCODE_FORWARD_DEL",
    "tab": "KEYCODE_TAB", "escape": "KEYCODE_ESCAPE", "space": "KEYCODE_SPACE",
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT", "move_home": "KEYCODE_MOVE_HOME", "move_end": "KEYCODE_MOVE_END",
    "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
    "power": "KEYCODE_POWER", "wake": "KEYCODE_WAKEUP",
}


def key_command(name: str) -> str | None:
    if name == "notifications":
        return "cmd statusbar expand-notifications"
    if name == "quick_settings":
        return "cmd statusbar expand-settings"
    code = KEYS.get(name)
    return f"input keyevent {code}" if code else None


def text_command(text: str) -> tuple[str | None, int]:
    """`input text` for what it can type (plain ASCII: it has no way to type other characters),
    and how many characters were left out."""
    kept = "".join(c for c in text if 32 <= ord(c) < 127)
    if not kept:
        return None, len(text)
    # input text reads %s as a space; the device's shell gets it single-quoted.
    words = kept.replace("%", "").replace(" ", "%s")
    quoted = "'" + words.replace("'", "'\\''") + "'"
    return f"input text {quoted}", len(text) - len(kept)


def gesture_command(gesture: Gesture) -> str:
    """A gesture as `input` does it, in the screen's pixels as it shows now."""
    x, y = gesture.start
    if gesture.kind == "tap":
        return f"input tap {x} {y}"
    if gesture.kind == "press":
        return f"input swipe {x} {y} {x} {y} {max(gesture.ms, 600)}"
    ex, ey = gesture.end
    return f"input swipe {x} {y} {ex} {ey} {gesture.ms}"


class Commands:
    """Shell commands for the phone, one at a time and in order, off the main thread."""

    def __init__(self, serial: str):
        self.serial = serial
        self.queue: Queue[str | None] = Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def run(self, command: str | None):
        if command:
            self.queue.put(command)

    def stop(self):
        self.queue.put(None)

    def _work(self):
        while (command := self.queue.get()) is not None:
            try:
                shell(self.serial, command, timeout=15)
            except (OSError, subprocess.SubprocessError) as error:
                debug("command failed:", command, error)


# ---- Video ---------------------------------------------------------------------------------

def scrcpy_version() -> str | None:
    """The scrcpy package's version ("3.3.4"), which its server insists on hearing back."""
    if not os.path.exists(SCRCPY_SERVER):
        return None
    try:
        result = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "scrcpy"], capture_output=True,
                                timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"(?:\d+:)?(\d+(?:\.\d+)+)", result.stdout.decode())
    return match[1] if match else None


def read_exactly(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("the phone's video ended")
        data += chunk
    return data


class Video:
    """The phone's screen as live video: H.264 from the phone, decoded here, every frame handed to
    `on_frame(texture)` on GLib's main loop (a slow window just gets the newest). `on_ended(why)`
    when it stops for good. scrcpy's server when the scrcpy package is installed; otherwise
    Android's screenrecord, started again on its own when it ends or the screen turns."""

    def __init__(self, serial: str, natural: tuple[int, int], rotation: int,
                 on_frame: Callable[[object], None], on_ended: Callable[[str], None]):
        self.serial = serial
        self.natural = natural
        self.rotation = rotation
        self.on_frame = on_frame
        self.on_ended = on_ended
        self.version = scrcpy_version()
        self.kind = "scrcpy" if self.version else "screenrecord"
        self.decoder = _decoder("h264", on_frame, self._decoder_broken)
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.forward_port: int | None = None
        self.edge = MAX_EDGE  # the video's longest edge (0: the screen's own size)
        self.fps = FRAME_RATE
        self.stopped = False
        self.session = 0  # each start of the phone's side; a stale reader stops quietly
        self.time_limit = True  # screenrecord's "--time-limit 0"; older Androids refuse it

    def start(self):
        if not self.decoder.start():
            GLib.idle_add(self.on_ended, "Install gstreamer1.0-libav to show the phone's screen.")
            return
        self._start_phone()

    def stop(self):
        self.stopped = True
        self._stop_phone()
        self.decoder.stop()

    def _decoder_broken(self):
        if not self.stopped:
            self._stop_phone()
            self._start_phone()

    def turned(self, rotation: int):
        """The screen turned. screenrecord keeps its first size, so it starts again in the new
        one; scrcpy's server follows by itself."""
        if rotation == self.rotation:
            return
        self.rotation = rotation
        if self.kind == "screenrecord" and not self.stopped:
            self._stop_phone()
            self._start_phone()

    def _start_phone(self):
        self.session += 1
        target = self._scrcpy if self.kind == "scrcpy" else self._screenrecord
        debug("video from", self.kind, "rotation", self.rotation)
        threading.Thread(target=target, args=(self.session,), daemon=True).start()

    def _stop_phone(self):
        self.session += 1
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
        sock, self.sock = self.sock, None
        if sock:
            sock.close()
        port, self.forward_port = self.forward_port, None
        if port:
            try:
                adb(self.serial, "forward", "--remove", f"tcp:{port}", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass

    def _push(self, data: bytes):
        self.decoder.push(data)

    def _screenrecord(self, session: int):
        w, h = even_size(self.natural, self.rotation, self.edge)
        limit = "--time-limit 0 " if self.time_limit else ""
        command = adb_command(self.serial, "exec-out",
                              f"screenrecord --output-format=h264 {limit}--size {w}x{h} "
                              f"--bit-rate {bit_rate(self.serial)} -")
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=TOOL_ERRORS)
        except OSError as error:
            GLib.idle_add(self._ended, session, str(error))
            return
        self.process = process
        received = 0
        while session == self.session:
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            received += len(chunk)
            self._push(chunk)
        process.wait()
        if session != self.session:
            return
        if received == 0 and self.time_limit and time.monotonic() - started < 5:
            self.time_limit = False  # an Android older than 14: the three-minute limit it is
            GLib.idle_add(self._again, session)
        elif time.monotonic() - started > 5:
            GLib.idle_add(self._again, session)  # its time ran out, or it was interrupted
        else:
            GLib.idle_add(self._ended, session, "The phone's screen recorder stopped.")

    def _scrcpy(self, session: int):
        scid = f"{secrets.randbits(31):08x}"
        try:
            adb(self.serial, "push", SCRCPY_SERVER, PHONE_SERVER, timeout=30)
            port = int(adb(self.serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}").strip())
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            GLib.idle_add(self._fall_back, session, str(error))
            return
        self.forward_port = port
        options = (f"scid={scid} log_level=warn tunnel_forward=true audio=false control=false "
                   "cleanup=false send_device_meta=false send_frame_meta=false send_dummy_byte=true "
                   f"send_codec_meta=true video_codec=h264 max_size={self.edge} "
                   f"video_bit_rate={bit_rate(self.serial)} max_fps={self.fps}")
        command = adb_command(self.serial, "shell",
                              f"CLASSPATH={PHONE_SERVER} app_process / com.genymobile.scrcpy.Server "
                              f"{self.version} {options}")
        try:
            self.process = subprocess.Popen(command, stdout=TOOL_ERRORS, stderr=TOOL_ERRORS)
        except OSError as error:
            GLib.idle_add(self._fall_back, session, str(error))
            return
        # The server listens a moment after it starts: until then adb's forward closes at once,
        # before the byte the server sends first.
        sock = None
        for _ in range(60):
            if session != self.session or (self.process and self.process.poll() is not None):
                break
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                if sock.recv(1):
                    break
            except OSError:
                pass
            if sock:
                sock.close()
                sock = None
            time.sleep(0.1)
        if not sock:
            if session == self.session:
                GLib.idle_add(self._fall_back, session, "scrcpy's server didn't answer")
            return
        self.sock = sock
        try:
            sock.settimeout(None)
            codec, width, height = struct.unpack(">III", read_exactly(sock, 12))
            debug("scrcpy video", hex(codec), width, height)
            if codec != 0x68323634:  # "h264"
                raise ConnectionError("the phone can't send H.264 video")
            while session == self.session:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                self._push(chunk)
        except OSError:
            pass
        if session == self.session:
            GLib.idle_add(self._ended, session, "The phone's video stopped.")

    def _again(self, session: int):
        if session == self.session and not self.stopped:
            self._stop_phone()
            self._start_phone()
        return GLib.SOURCE_REMOVE

    def _fall_back(self, session: int, why: str):
        """scrcpy's server didn't start: screenrecord instead."""
        debug("scrcpy didn't start:", why)
        if session == self.session and not self.stopped:
            self._stop_phone()
            self.kind = "screenrecord"
            self._start_phone()
        return GLib.SOURCE_REMOVE

    def _ended(self, session: int, why: str):
        debug("video ended:", why)
        if session == self.session and not self.stopped:
            self.on_ended(why)
        return GLib.SOURCE_REMOVE

# ---- The phone as a whole ------------------------------------------------------------------

@dataclass
class Link:
    """What Handset found out opening the phone."""

    serial: str
    natural: tuple[int, int]
    rotation: int
    awake: bool = True


def open_link(phone_id: str, address: str | None, port: int | None) -> Link | str:
    """Find the phone on adb and its screen's size and rotation (slow: run it off the main
    loop). A Link, or why it can't be reached, in words."""
    if not adb_binary():
        return "adb isn't installed."
    serial = find_serial(phone_id, address, port)
    if not serial:
        return "Plug it in, or connect it over Wi-Fi (Settings › Phones)."
    try:
        natural = parse_wm_size(shell(serial, "wm size", timeout=8)) or (1080, 2400)
        state = shell(serial, SCREEN_STATE, timeout=8)
    except (OSError, subprocess.SubprocessError) as error:
        return f"The phone didn't answer: {error}"
    rotation = parse_rotation(state) or 0
    awake = parse_awake(state) is not False
    debug("phone", serial, "screen", natural, "rotation", rotation, "on" if awake else "off")
    return Link(serial, natural, rotation, awake)


class Android:
    """One Android phone in the window: its video, its touchscreen, its keys. Positions are the
    screen's pixels as it shows now (`shown`). `on_frame(texture)`, `on_turned()` (the shown
    size changed), `on_touch(live)` (whether touches go through the virtual touchscreen),
    `on_awake(on)` (the screen went on or off) and `on_ended(why)` run on GLib's main loop."""

    def __init__(self, link: Link, on_frame, on_turned, on_touch, on_ended, on_awake=None,
                 edge: int | None = None, fps: int = FRAME_RATE):
        self.link = link
        self.on_turned = on_turned
        self.on_touch = on_touch
        self.on_awake = on_awake
        self.rotation = link.rotation
        self.awake = link.awake
        self.woken_at = 0.0
        self.locked = False  # the lock screen shows (its PIN pad is hidden from screen sharing)
        self.video = Video(link.serial, link.natural, link.rotation, on_frame, on_ended)
        self.video.edge = MAX_EDGE if edge is None else edge
        self.video.fps = fps
        self.touch = Touch(link.serial, link.natural, self._touch_ready, self._touch_failed)
        self.commands = Commands(link.serial)
        self.live_touch = False
        self.pressed = False
        self.stopped = False

    @property
    def shown(self) -> tuple[int, int]:
        return shown_size(self.link.natural, self.rotation)

    def start(self):
        # Opening the phone in the window is wanting to see it: an asleep screen sends no picture.
        if not self.awake:
            self.wake()
        self.video.start()
        self.touch.start()
        threading.Thread(target=self._watch_screen, daemon=True).start()

    def wake(self):
        """Wakes the screen, and counts it awake straight away, so the next press is a touch."""
        self.commands.run("input keyevent KEYCODE_WAKEUP")
        self.woken_at = time.monotonic()
        if not self.awake:
            self._awake_changed(True)

    def show_at(self, width: int, height: int):
        """The screen shows this big in the window (the monitor's pixels)."""
        self.video.decoder.show_at(width, height)

    def stop(self):
        self.stopped = True
        if self.pressed:
            self.release()
        self.video.stop()
        self.touch.stop()
        self.commands.stop()

    # A finger on the glass, live: down, moves, up.
    def press(self, x: int, y: int):
        if self.live_touch:
            self.pressed = True
            self.touch.down(*to_natural(x, y, self.link.natural, self.rotation))

    def drag(self, x: int, y: int):
        if self.live_touch and self.pressed:
            self.touch.move(*to_natural(x, y, self.link.natural, self.rotation))

    def release(self):
        if self.pressed:
            self.pressed = False
            self.touch.up()

    def gesture(self, gesture: Gesture):
        """A whole gesture at once (a scroll, or anything when there's no live touch)."""
        if not self.live_touch:
            self.commands.run(gesture_command(gesture))
        elif gesture.kind == "swipe":
            threading.Thread(target=self._swipe, args=(gesture,), daemon=True).start()
        else:
            self.press(*gesture.start)
            GLib.timeout_add(max(gesture.ms, 40), self._lift)

    def key(self, name: str):
        self.commands.run(key_command(name))

    def type(self, text: str) -> int:
        """Types `text`; answers how many characters it couldn't."""
        command, left_out = text_command(text)
        self.commands.run(command)
        return left_out

    def _lift(self):
        self.release()
        return GLib.SOURCE_REMOVE

    def _swipe(self, gesture: Gesture):
        (x0, y0), (x1, y1) = gesture.start, gesture.end
        steps = max(4, gesture.ms // 16)
        natural, rotation = self.link.natural, self.rotation
        self.touch.down(*to_natural(x0, y0, natural, rotation))
        for i in range(1, steps + 1):
            time.sleep(gesture.ms / 1000 / steps)
            x, y = x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps
            self.touch.move(*to_natural(x, y, natural, rotation))
        self.touch.up()

    def _touch_ready(self):
        self.live_touch = True
        self.on_touch(True)

    def _touch_failed(self):
        self.live_touch = False
        self.pressed = False
        self.on_touch(False)

    def _watch_screen(self):
        while not self.stopped:
            time.sleep(ROTATION_POLL_S)
            try:
                state = shell(self.link.serial, SCREEN_STATE, timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            if self.stopped:
                return
            rotation, awake = parse_rotation(state), parse_awake(state)
            locked = parse_locked(state)
            if locked is not None:
                self.locked = locked
            if rotation is not None and rotation != self.rotation:
                GLib.idle_add(self._turned, rotation)
            if awake is False and time.monotonic() - self.woken_at < WAKING_S:
                continue  # still waking
            if awake is not None and awake != self.awake:
                GLib.idle_add(self._awake_changed, awake)

    def _awake_changed(self, awake: bool):
        if awake != self.awake and not self.stopped:
            debug("screen", "on" if awake else "off")
            self.awake = awake
            if not awake and self.pressed:
                self.release()
            if self.on_awake:
                self.on_awake(awake)
        return GLib.SOURCE_REMOVE

    def _turned(self, rotation: int):
        debug("screen turned to", rotation)
        if self.pressed:
            self.release()
        self.rotation = rotation
        self.video.turned(rotation)
        self.on_turned()
        return GLib.SOURCE_REMOVE


# ---- One app on a screen of its own -----------------------------------------------------------

def parse_app_list(text: str) -> list[tuple[str, str]]:
    """(name, package) for each app scrcpy's server lists (`list_apps=true`): after "List of
    apps:", a line per app, " * " (the system's) or " - " (the person's), the name padded to 30
    and the package; a name that long puts the package on the next line."""
    apps, waiting = [], None
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if "List of apps:" in line), len(lines))
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[:2] in ("* ", "- "):
            rest = stripped[2:].rstrip()
            name, _, package = rest.rpartition(" ")
            if name.strip() and re.fullmatch(r"[A-Za-z][\w.]*\.[\w.]+", package):
                apps.append((name.strip(), package))
                waiting = None
            else:
                waiting = rest.strip()
        elif waiting and re.fullmatch(r"[A-Za-z][\w.]*\.[\w.]+", stripped):
            apps.append((waiting, stripped))
            waiting = None
    return apps


def parse_drawer(text: str) -> set[str]:
    """The packages in `cmd package query-activities --brief --components` lines (pkg/activity)."""
    return {line.strip().split("/", 1)[0] for line in text.splitlines() if "/" in line}


def drawer(serial: str) -> set[str] | None:
    """The packages the phone's app drawer shows (a launcher activity each); None: not known."""
    try:
        found = parse_drawer(shell(serial, "cmd package query-activities --brief --components "
                                           "-a android.intent.action.MAIN -c android.intent.category.LAUNCHER",
                                   timeout=15))
    except (OSError, subprocess.SubprocessError):
        return None
    return found or None


def list_apps(serial: str) -> list[tuple[str, str]] | str:
    """The phone's apps as its app drawer has them, (name, package) by name; or why not, in
    words. Through scrcpy's server, which asks the phone for each app's name; the system's parts
    that open but have no place in the drawer (accessibility tools, say) are left out."""
    version = scrcpy_version()
    if not version:
        return "Install the scrcpy package to open the phone's apps on the desk."
    try:
        adb(serial, "push", SCRCPY_SERVER, PHONE_SERVER, timeout=30)
        text = adb(serial, "shell", f"CLASSPATH={PHONE_SERVER} app_process / com.genymobile.scrcpy.Server "
                                    f"{version} log_level=info cleanup=false list_apps=true", timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"The phone didn't list its apps ({error})."
    listed = parse_app_list(text)
    shown = drawer(serial)
    if shown:
        listed = [(name, package) for name, package in listed if package in shown]
    return sorted(listed, key=lambda app: app[0].casefold())


def parse_versions(text: str) -> dict[str, str]:
    """{package: versionCode} from `pm list packages --show-versioncode`."""
    found = {}
    for line in text.splitlines():
        match = re.match(r"package:(\S+) versionCode:(\d+)", line.strip())
        if match:
            found[match[1]] = match[2]
    return found


def app_versions(serial: str) -> dict[str, str]:
    """Every app's version, in one look: an icon kept from before is read again once it's changed."""
    try:
        return parse_versions(shell(serial, "pm list packages --show-versioncode", timeout=15))
    except (OSError, subprocess.SubprocessError):
        return {}


# scrcpy's control socket takes these (big-endian): keys, text, touches, and an app to start.
INJECT_KEYCODE, INJECT_TEXT, INJECT_TOUCH, START_APP, RESET_VIDEO = 0, 1, 2, 16, 17
DOWN, UP, MOVE = 0, 1, 2
FINGER = -2  # scrcpy's "generic finger" pointer
KEYCODES = {
    "back": 4, "enter": 66, "delete": 67, "forward_delete": 112, "tab": 61, "escape": 111,
    "space": 62, "up": 19, "down": 20, "left": 21, "right": 22, "move_home": 122, "move_end": 123,
    "volume_up": 24, "volume_down": 25,
}
MOST_TEXT = 300  # bytes of text in one message


def touch_message(action: int, x: int, y: int, size: tuple[int, int]) -> bytes:
    """A finger down, moving or up at (x, y) of a video `size` big: the server drops one whose
    size isn't the video's as it is now."""
    width, height = size
    pressure = 0 if action == UP else 0xFFFF
    return struct.pack(">BBqiiHHHii", INJECT_TOUCH, action, FINGER, x, y, width, height, pressure, 0, 0)


def key_messages(code: int) -> bytes:
    return b"".join(struct.pack(">BBiii", INJECT_KEYCODE, action, code, 0, 0) for action in (DOWN, UP))


def text_messages(text: str) -> bytes:
    """Text in pieces the server takes, never splitting a character."""
    out, piece = b"", b""
    for char in text:
        data = char.encode()
        if len(piece) + len(data) > MOST_TEXT:
            out += struct.pack(">BI", INJECT_TEXT, len(piece)) + piece
            piece = b""
        piece += data
    if piece:
        out += struct.pack(">BI", INJECT_TEXT, len(piece)) + piece
    return out


def start_app_message(package: str) -> bytes:
    name = package.encode()[:255]
    return struct.pack(">BB", START_APP, len(name)) + name


# The server's word on the display it made: "New display: 1080x2400/425 (id=121)".
DISPLAY_MADE = re.compile(r"New display: .*\(id=(\d+)\)")


def display_size(natural: tuple[int, int]) -> tuple[int, int]:
    """An app's own screen: the phone's, upright, in whole blocks of 8 (as the video is made)."""
    width, height = sorted(natural)
    return max(8, width // 8 * 8), max(8, height // 8 * 8)


class AppScreen:
    """One app of an Android phone on a screen of its own: a virtual display scrcpy's server
    makes on the phone, as big as the phone's own, that only that app shows on. Its picture
    comes here, and touches, keys and text go back to it through scrcpy's control socket; the
    phone's own screen carries on as it was (an app open there moves over). Closing it closes
    the app. The same calls and callbacks as Android; positions are the video's pixels.

    The screen goes on and off with the phone's own (Android puts it in the phone's display
    group), and shows nothing while the phone is locked: so it wakes the phone when it opens,
    and watches it, as Android does (`awake`, `locked`)."""

    def __init__(self, link: Link, package: str, on_frame, on_turned, on_touch, on_ended, on_awake=None,
                 edge: int | None = None, fps: int = FRAME_RATE, task: int | None = None, on_display=None):
        self.link = link
        self.package = package
        self.task = task  # a task of the app's already open (another app's window's), moved here
        self.on_display = on_display  # called with the phone's id for this screen, once it's made
        self.display: int | None = None
        self.fps = fps
        self.on_frame = on_frame
        self.on_turned = on_turned
        self.on_touch = on_touch
        self.on_ended = on_ended
        self.size = display_size(link.natural)
        self.shown = self.size
        self.decoder = _decoder("h264", self._frame, self._decoder_broken)
        self.version = scrcpy_version()
        self.process: subprocess.Popen | None = None
        self.video: socket.socket | None = None
        self.control: socket.socket | None = None
        self.forward_port: int | None = None
        self.lock = threading.Lock()  # one control message at a time
        self.on_awake = on_awake
        self.awake = link.awake
        self.woken_at = 0.0
        self.locked = False
        self.live_touch = False
        self.pressed = False
        self.finger = (0, 0)  # where the finger is (or was last)
        self.stopped = False
        self.problem: str | None = None  # what the server said went wrong

    def start(self):
        if not self.version:
            GLib.idle_add(self._ended, "Install the scrcpy package to open the phone's apps on the desk.")
            return
        if not self.decoder.start():
            GLib.idle_add(self._ended, "Install gstreamer1.0-libav to show the phone's screen.")
            return
        if not self.awake:
            self.wake()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watch_screen, daemon=True).start()

    def stop(self):
        self.stopped = True
        if self.pressed:
            self.release()
        for sock in (self.control, self.video):
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        self.control = self.video = None
        threading.Thread(target=self._end_server, args=(self.process, self.forward_port), daemon=True).start()
        self.process = None
        self.decoder.stop()

    def _end_server(self, process: subprocess.Popen | None, port: int | None):
        """The server ends by itself once its sockets close, giving its display back (and the app
        with it); one that doesn't is stopped."""
        if process:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        if port:
            try:
                adb(self.link.serial, "forward", "--remove", f"tcp:{port}", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass

    def wake(self):
        """Wakes the phone (its screen and this one with it), counted awake straight away."""
        def press():
            try:
                shell(self.link.serial, "input keyevent KEYCODE_WAKEUP", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass

        threading.Thread(target=press, daemon=True).start()
        self.woken_at = time.monotonic()
        if not self.awake:
            self._awake_changed(True)

    def show_at(self, width: int, height: int):
        self.decoder.show_at(width, height)

    def _watch_screen(self):
        """Whether the phone is awake and locked, as Android._watch_screen watches it."""
        while not self.stopped:
            time.sleep(ROTATION_POLL_S)
            try:
                state = shell(self.link.serial, SCREEN_STATE, timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            if self.stopped:
                return
            awake, locked = parse_awake(state), parse_locked(state)
            if locked is not None:
                self.locked = locked
            if awake is False and time.monotonic() - self.woken_at < WAKING_S:
                continue  # still waking
            if awake is not None and awake != self.awake:
                GLib.idle_add(self._awake_changed, awake)

    def _awake_changed(self, awake: bool):
        if awake != self.awake and not self.stopped:
            debug("phone", "awake" if awake else "asleep", "under an app's screen")
            self.awake = awake
            if not awake and self.pressed:
                self.release()
            if self.on_awake:
                self.on_awake(awake)
        return GLib.SOURCE_REMOVE

    # ---- Its server --------------------------------------------------------------------------

    def _run(self):
        scid = f"{secrets.randbits(31):08x}"
        serial = self.link.serial
        try:
            adb(serial, "push", SCRCPY_SERVER, PHONE_SERVER, timeout=30)
            self.forward_port = int(adb(serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}").strip())
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            GLib.idle_add(self._ended, f"The phone's video didn't start ({error}).")
            return
        width, height = self.size
        options = (f"scid={scid} log_level=info tunnel_forward=true audio=false control=true cleanup=false "
                   "clipboard_autosync=false send_device_meta=false send_frame_meta=false "
                   "send_dummy_byte=true send_codec_meta=true video_codec=h264 max_size=0 "
                   f"video_bit_rate={bit_rate(serial)} max_fps={self.fps} new_display={width}x{height} "
                   "vd_system_decorations=false display_ime_policy=hide")
        command = adb_command(serial, "shell", f"CLASSPATH={PHONE_SERVER} app_process / "
                                               f"com.genymobile.scrcpy.Server {self.version} {options}")
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as error:
            GLib.idle_add(self._ended, str(error))
            return
        threading.Thread(target=self._read_log, args=(self.process,), daemon=True).start()
        video = self._connect(self.process, first=True)
        control = video and self._connect(self.process, first=False)
        if not (video and control):
            if video:
                video.close()
            if not self.stopped:
                GLib.idle_add(self._ended, self.problem or "scrcpy's server didn't answer.")
            return
        if self.stopped:
            video.close()
            control.close()
            return
        self.video, self.control = video, control
        try:
            codec, width, height = struct.unpack(">III", read_exactly(video, 12))
            debug("app video", hex(codec), width, height)
            if codec != 0x68323634:  # "h264"
                raise ConnectionError("the phone can't send H.264 video")
            self.shown = (width, height)
            # The server knows its display only once the video starts; the app goes on it then.
            if self.task is None:
                self._send(start_app_message(self.package))
            elif not self._take_task():
                return
            GLib.idle_add(self._touch_ready)
            while not self.stopped:
                chunk = video.recv(65536)
                if not chunk:
                    break
                self.decoder.push(chunk)
        except OSError:
            pass
        if not self.stopped:
            GLib.idle_add(self._ended, self.problem or "The app's video stopped.")

    def _connect(self, process: subprocess.Popen, first: bool) -> socket.socket | None:
        """The video socket (the server's first, which sends one byte when it's there), then the
        control one."""
        for _ in range(60):
            if self.stopped or process.poll() is not None:
                return None
            try:
                sock = socket.create_connection(("127.0.0.1", self.forward_port), timeout=2)
            except OSError:
                time.sleep(0.1)
                continue
            if not first:
                sock.settimeout(None)
                return sock
            try:
                if sock.recv(1):
                    sock.settimeout(None)
                    return sock
            except OSError:
                pass
            sock.close()
            time.sleep(0.1)
        return None

    def _take_task(self) -> bool:
        """The app's task, open on another app's screen, moved to this one (as it is: the video
        the link opened, say, rather than the app's start)."""
        for _ in range(40):
            if self.display is not None or self.stopped:
                break
            time.sleep(0.05)
        if self.display is None:
            GLib.idle_add(self._ended, "The phone didn't say which screen it made.")
            return False
        try:
            said = shell(self.link.serial, f"am display move-stack {self.task} {self.display}", timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            said = f"Exception: {error}"
        if "Exception" not in said:
            return True
        debug("the task didn't move:", said.strip()[:300])
        GLib.idle_add(self._ended, "The app closed before it got here.")
        return False

    def _read_log(self, process: subprocess.Popen):
        """What the server says: its trouble (an app that isn't there) in words for the window,
        and the display it made."""
        for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip()
            debug("scrcpy:", line)
            made = DISPLAY_MADE.search(line)
            if made:
                self.display = int(made.group(1))
                if self.on_display:
                    GLib.idle_add(self._display_made)
            if "No app found" in line:
                self.problem = "That app isn't on the phone any more."
                GLib.idle_add(self._ended, self.problem)
            elif "ERROR" in line and not self.problem:
                self.problem = line.split("ERROR:", 1)[-1].strip() or line
        process.stdout.close()

    def _display_made(self):
        if not self.stopped and self.display is not None:
            self.on_display(self.display)
        return GLib.SOURCE_REMOVE

    def _send(self, message: bytes):
        control = self.control
        if not control or not message:
            return
        try:
            with self.lock:
                control.sendall(message)
        except OSError:
            pass

    def _touch_ready(self):
        if not self.stopped:
            self.live_touch = True
            self.on_touch(True)
        return GLib.SOURCE_REMOVE

    def _frame(self, texture):
        native = self.decoder.native
        if native and native != self.shown:
            # The app turned its screen (a video, a game): the same screen, sideways.
            self.shown = native
            if self.pressed:
                self.release()
            self.on_turned()
        self.on_frame(texture)

    def _decoder_broken(self):
        """The decoder started afresh (copying its frames, say, where the GPU's weren't taken):
        the phone starts its video afresh too, so there's a picture to begin from. Starting the
        server again would close the app."""
        if self.stopped:
            return
        if self.control:
            self._send(bytes([RESET_VIDEO]))
        else:
            self._ended("The app's video broke off.")

    def _ended(self, why: str):
        debug("app screen ended:", why)
        if not self.stopped:
            self.on_ended(why)
        return GLib.SOURCE_REMOVE

    # ---- Touches, keys and text ------------------------------------------------------------

    def _at(self, x: int, y: int) -> tuple[int, int]:
        width, height = self.shown
        return min(max(int(x), 0), width - 1), min(max(int(y), 0), height - 1)

    def press(self, x: int, y: int):
        if self.live_touch:
            self.pressed = True
            self.finger = self._at(x, y)
            self._send(touch_message(DOWN, *self.finger, self.shown))

    def drag(self, x: int, y: int):
        if self.live_touch and self.pressed:
            self.finger = self._at(x, y)
            self._send(touch_message(MOVE, *self.finger, self.shown))

    def release(self):
        if self.pressed:
            self.pressed = False
            self._send(touch_message(UP, *self.finger, self.shown))

    def gesture(self, gesture: Gesture):
        if gesture.kind == "swipe":
            threading.Thread(target=self._swipe, args=(gesture,), daemon=True).start()
        else:
            self.press(*gesture.start)
            GLib.timeout_add(max(gesture.ms, 40), self._lift)

    def _lift(self):
        self.release()
        return GLib.SOURCE_REMOVE

    def _swipe(self, gesture: Gesture):
        (x0, y0), (x1, y1) = gesture.start, gesture.end
        steps = max(4, gesture.ms // 16)
        self._send(touch_message(DOWN, *self._at(x0, y0), self.shown))
        for i in range(1, steps + 1):
            time.sleep(gesture.ms / 1000 / steps)
            x, y = x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps
            self._send(touch_message(MOVE, *self._at(x, y), self.shown))
        self._send(touch_message(UP, *self._at(x1, y1), self.shown))

    def key(self, name: str):
        """The keys an app takes; the phone's own (home, recents, its shade) aren't on this screen."""
        code = KEYCODES.get(name)
        if code:
            self._send(key_messages(code))

    def type(self, text: str) -> int:
        self._send(text_messages(text))
        return 0


# ---- The phone's clipboard ---------------------------------------------------------------------

SET_CLIPBOARD = 9
DEVICE_CLIPBOARD, DEVICE_ACK = 0, 1
MOST_CLIPBOARD = 262130  # bytes: more than the phone's binder takes in one go


def clipboard_message(text: str) -> bytes:
    """SET_CLIPBOARD: no acknowledgement asked, nothing pasted; cut to what the phone takes,
    never splitting a character."""
    data = text.encode()
    if len(data) > MOST_CLIPBOARD:
        data = data[:MOST_CLIPBOARD].decode(errors="ignore").encode()
    return struct.pack(">BQBI", SET_CLIPBOARD, 0, 0, len(data)) + data


class ClipboardLink:
    """The phone's clipboard, both ways, through scrcpy's server run for that alone (no video, no
    display): `on_text(text)` on GLib's main loop whenever something is copied on the phone (by any
    app; nothing while it's locked), and set(text) to put text on it. Text only. Android says on
    the phone when its clipboard is read ("Shell pasted…") and opens its clipboard editor when
    it's set. It starts again by itself if the link drops."""

    def __init__(self, serial: str, on_text: Callable[[str], None]):
        self.serial = serial
        self.on_text = on_text
        self.control: socket.socket | None = None
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.stopped = False
        self.last = None  # the text last seen either way, so neither side hears its own echo
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stopped = True
        control, self.control = self.control, None
        if control:
            try:
                control.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            control.close()
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if self.port:
            try:
                adb(self.serial, "forward", "--remove", f"tcp:{self.port}", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass

    def set(self, text: str):
        if not text or text == self.last:
            return
        self.last = text
        control = self.control
        if control:
            try:
                with self.lock:
                    control.sendall(clipboard_message(text))
            except OSError:
                pass

    def _run(self):
        delay = 2
        while not self.stopped:
            started = time.monotonic()
            self._session()
            if self.stopped:
                return
            delay = 2 if time.monotonic() - started > 60 else min(delay * 2, 60)
            time.sleep(delay)

    def _session(self):
        version = scrcpy_version()
        if not version:
            return
        scid = f"{secrets.randbits(31):08x}"
        try:
            adb(self.serial, "push", SCRCPY_SERVER, PHONE_SERVER, timeout=30)
            self.port = int(adb(self.serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}").strip())
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            debug("the phone's clipboard didn't open:", error)
            return
        options = (f"scid={scid} log_level=warn tunnel_forward=true video=false audio=false control=true "
                   "clipboard_autosync=true power_on=false cleanup=false send_device_meta=false send_dummy_byte=true")
        command = adb_command(self.serial, "shell", f"CLASSPATH={PHONE_SERVER} app_process / "
                                                    f"com.genymobile.scrcpy.Server {version} {options}")
        try:
            self.process = subprocess.Popen(command, stdout=TOOL_ERRORS, stderr=TOOL_ERRORS)
        except OSError:
            return
        control = None
        for _ in range(60):  # the control socket is the first here: it sends the one byte
            if self.stopped or self.process.poll() is not None:
                break
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                if sock.recv(1) == b"\0":
                    control = sock
                    break
                sock.close()
            except OSError:
                pass
            time.sleep(0.1)
        if not control:
            return
        control.settimeout(None)
        self.control = control
        debug("the phone's clipboard is linked")
        try:
            while not self.stopped:
                kind = read_exactly(control, 1)[0]
                if kind == DEVICE_CLIPBOARD:
                    (length,) = struct.unpack(">I", read_exactly(control, 4))
                    text = read_exactly(control, length).decode(errors="replace")
                    if text and text != self.last:
                        self.last = text
                        GLib.idle_add(self.on_text, text)
                elif kind == DEVICE_ACK:
                    read_exactly(control, 8)
                else:
                    break  # something this doesn't know: start afresh
        except (OSError, ConnectionError):
            pass
        self.control = None
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
            process.wait()


# ---- Files on the phone ----------------------------------------------------------------------

DOWNLOADS = "/sdcard/Download"


@dataclass
class PhoneFile:
    """One thing in a folder on the phone."""

    name: str
    path: str
    folder: bool
    size: int
    modified: float  # seconds since the epoch


def parse_listing(data: bytes, folder: str) -> list[PhoneFile]:
    """`find -printf '%M\\t%s\\t%T@\\t%f\\0'` output: folders first, then by name; hidden ones left out."""
    found = []
    for entry in data.split(b"\0"):
        parts = entry.decode(errors="surrogateescape").split("\t", 3)
        if len(parts) != 4 or not parts[3] or parts[3].startswith("."):
            continue
        mode, size, when, name = parts
        try:
            found.append(PhoneFile(name, f"{folder.rstrip('/')}/{name}", mode.startswith("d"), int(size), float(when)))
        except ValueError:
            continue
    return sorted(found, key=lambda f: (not f.folder, f.name.casefold()))


# A search gives at most this many of what it finds.
MOST_FOUND = 300


def parse_found(data: bytes) -> list[PhoneFile]:
    """`find -printf '%M\\t%s\\t%T@\\t%p\\0'` output (whole paths): hidden ones and what's in
    hidden folders left out, at most MOST_FOUND, folders first, then by name."""
    found = []
    for entry in data.split(b"\0"):
        parts = entry.decode(errors="surrogateescape").split("\t", 3)
        if len(parts) != 4 or not parts[3]:
            continue
        mode, size, when, path = parts
        if any(part.startswith(".") for part in path.split("/") if part):
            continue
        try:
            found.append(PhoneFile(path.rsplit("/", 1)[-1], path, mode.startswith("d"), int(size), float(when)))
        except ValueError:
            continue
        if len(found) >= MOST_FOUND:
            break
    return sorted(found, key=lambda f: (not f.folder, f.name.casefold()))


def find_files(serial: str, folder: str, text: str) -> list[PhoneFile] | str:
    """What's named like `text` (any case) in a folder on the phone and the folders in it."""
    pattern = "*" + "".join("\\" + c if c in "*?[]\\" else c for c in text) + "*"
    # Hidden folders and apps' own data (Android/data, Android/obb) aren't looked through: slow, and
    # nothing a person keeps there.
    command = (f"find {shlex.quote(folder.rstrip('/') + '/')} -mindepth 1 "
               "\\( -name '.*' -o -path '*/Android/data' -o -path '*/Android/obb' \\) -prune -o "
               f"-iname {shlex.quote(pattern)} -printf '%M\\t%s\\t%T@\\t%p\\0' 2>/dev/null")
    try:
        result = subprocess.run(adb_command(serial, "exec-out", command), capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return f"The phone didn't answer ({error})."
    return parse_found(result.stdout)


def list_folder(serial: str, folder: str) -> list[PhoneFile] | str:
    """What's in a folder on the phone, or why not, in words."""
    quoted = shlex.quote(folder.rstrip("/") + "/")
    try:
        result = subprocess.run(adb_command(serial, "exec-out", f"find {quoted} -mindepth 1 -maxdepth 1 "
                                                               "-printf '%M\\t%s\\t%T@\\t%f\\0' 2>/dev/null"),
                                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"The phone didn't answer ({error})."
    if result.returncode:
        return "The phone didn't list that folder."
    return parse_listing(result.stdout, folder)


def free_name(serial: str, folder: str, name: str) -> str:
    """A name in the phone's folder that isn't taken: the name, or "name (2).ext" and on."""
    stem, dot, ext = name.rpartition(".") if "." in name.strip(".") else (name, "", "")
    listed = list_folder(serial, folder)
    taken = {f.name for f in listed} if isinstance(listed, list) else set()
    candidate, number = name, 2
    while candidate in taken:
        candidate = f"{stem} ({number}){dot}{ext}"
        number += 1
    return candidate


def send_files(serial: str, paths: list[str], folder: str = DOWNLOADS) -> tuple[list[str], str | None]:
    """Files (or folders) from this computer to a folder on the phone, under names not taken
    there; then the phone's media index learns each file, so its apps show it now. The paths
    they got on the phone, and why the rest didn't go."""
    sent, trouble = [], None
    for path in paths:
        target = f"{folder.rstrip('/')}/{free_name(serial, folder, os.path.basename(path.rstrip('/')))}"
        try:
            result = subprocess.run(adb_command(serial, "push", path, target), capture_output=True, timeout=3600)
        except (OSError, subprocess.SubprocessError) as error:
            trouble = str(error)
            continue
        if result.returncode:
            said = result.stderr.decode(errors="replace").strip().splitlines()
            trouble = said[-1] if said else "adb push failed"
            continue
        sent.append(target)
        if os.path.isfile(path):
            try:
                shell(serial, f"content call --uri content://media --method scan_file --arg {shlex.quote(target)}",
                      timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
    return sent, trouble


def fetch_file(serial: str, path: str, folder: str) -> str | None:
    """A file from the phone into a folder here, under a name not taken there (its date kept):
    where it went, or None."""
    name = os.path.basename(path)
    stem, dot, ext = name.rpartition(".") if "." in name.strip(".") else (name, "", "")
    target, number = os.path.join(folder, name), 2
    while os.path.exists(target):
        target = os.path.join(folder, f"{stem} ({number}){dot}{ext}")
        number += 1
    os.makedirs(folder, exist_ok=True)
    try:
        result = subprocess.run(adb_command(serial, "pull", "-a", path, target), capture_output=True, timeout=3600)
    except (OSError, subprocess.SubprocessError):
        return None
    return target if result.returncode == 0 and os.path.exists(target) else None


class AdbFiles:
    """An Android phone's files, as the Files dialog and dropped files use them (as
    iphone.FilesSession): its storage, a folder at a time."""

    top = "/sdcard"
    downloads = DOWNLOADS

    def __init__(self, serial: str):
        self.serial = serial

    def list_folder(self, folder: str) -> list[PhoneFile] | str:
        return list_folder(self.serial, folder)

    def fetch_file(self, path: str, folder: str) -> str | None:
        return fetch_file(self.serial, path, folder)

    def find_files(self, folder: str, text: str) -> list[PhoneFile] | str:
        return find_files(self.serial, folder, text)

    def drop_folder(self, _paths: list[str]) -> tuple[str, str]:
        """Where files dropped on the phone go, and how a person finds them there."""
        return self.downloads, "Download folder"

    def deliver(self, paths: list[str]) -> tuple[list[str], str, str | None]:
        """Files dropped on the phone, into its Download folder: what went, where, why not."""
        sent, trouble = self.send_files(paths, self.downloads)
        return sent, "Download folder", trouble

    def send_files(self, paths: list[str], folder: str) -> tuple[list[str], str | None]:
        return send_files(self.serial, paths, folder)

    def stop(self):
        pass


# ---- The phone's sound, played here -----------------------------------------------------------

OPUS = 0x6F707573  # "opus"
AUDIO_DISABLED, AUDIO_FAILED = 0, 1  # the codec ids the server sends when it can't capture
PACKET_CONFIG = 1 << 63


class SoundLink:
    """The phone's sound played on this computer: scrcpy's server capturing the phone's whole
    audio output (which silences the phone itself while it does) as Opus, decoded here and played
    through the computer's speakers. Stopped, the phone plays its own sound again. Android 11 and
    later. `on_ended(why)` on GLib's main loop if it can't go on."""

    def __init__(self, serial: str, on_ended: Callable[[str], None] | None = None):
        self.serial = serial
        self.on_ended = on_ended
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.port: int | None = None
        self.pipeline = None
        self.source = None
        self.stopped = False
        self.packets = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stopped = True
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if self.port:
            try:
                adb(self.serial, "forward", "--remove", f"tcp:{self.port}", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        pipeline, self.pipeline, self.source = self.pipeline, None, None
        if pipeline:
            from gi.repository import Gst

            pipeline.set_state(Gst.State.NULL)

    def _ended(self, why: str):
        debug("the phone's sound here stopped:", why)
        if not self.stopped and self.on_ended:
            self.on_ended(why)
        return GLib.SOURCE_REMOVE

    def _player(self):
        """Opus packets in, the computer's speakers out (the desktop's own choice of them)."""
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        pipeline = Gst.parse_launch(
            "appsrc name=source is-live=true do-timestamp=true format=time "
            'caps="audio/x-opus,channel-mapping-family=(int)0,rate=(int)48000,channels=(int)2" ! '
            "queue max-size-time=200000000 leaky=downstream ! opusdec plc=true ! audioconvert ! audioresample ! "
            "autoaudiosink sync=false")
        pipeline.set_state(Gst.State.PLAYING)
        return pipeline, pipeline.get_by_name("source")

    def _run(self):
        from gi.repository import Gst

        version = scrcpy_version()
        if not version:
            GLib.idle_add(self._ended, "Install the scrcpy package to play the phone's sound here.")
            return
        scid = f"{secrets.randbits(31):08x}"
        try:
            adb(self.serial, "push", SCRCPY_SERVER, PHONE_SERVER, timeout=30)
            self.port = int(adb(self.serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}").strip())
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            GLib.idle_add(self._ended, f"The phone's sound didn't start ({error}).")
            return
        options = (f"scid={scid} log_level=warn tunnel_forward=true video=false audio=true control=false "
                   "audio_codec=opus audio_source=output power_on=false cleanup=false send_device_meta=false "
                   "send_dummy_byte=true send_codec_meta=true send_frame_meta=true")
        command = adb_command(self.serial, "shell", f"CLASSPATH={PHONE_SERVER} app_process / "
                                                    f"com.genymobile.scrcpy.Server {version} {options}")
        try:
            self.process = subprocess.Popen(command, stdout=TOOL_ERRORS, stderr=TOOL_ERRORS)
        except OSError as error:
            GLib.idle_add(self._ended, str(error))
            return
        sock = None
        for _ in range(60):  # the audio socket is the first here: it sends the one byte
            if self.stopped or self.process.poll() is not None:
                break
            try:
                candidate = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                if candidate.recv(1) == b"\0":
                    sock = candidate
                    break
                candidate.close()
            except OSError:
                pass
            time.sleep(0.1)
        if not sock:
            if not self.stopped:
                GLib.idle_add(self._ended, "scrcpy's server didn't answer.")
            return
        sock.settimeout(None)
        self.sock = sock
        try:
            (codec,) = struct.unpack(">I", read_exactly(sock, 4))
            if codec in (AUDIO_DISABLED, AUDIO_FAILED) or codec != OPUS:
                GLib.idle_add(self._ended, "The phone didn't let its sound be shared (it needs Android 11 or later).")
                return
            self.pipeline, self.source = self._player()
            debug("the phone's sound plays here")
            while not self.stopped:
                pts_flags, size = struct.unpack(">QI", read_exactly(sock, 12))
                data = read_exactly(sock, size)
                if pts_flags & PACKET_CONFIG:
                    continue  # the Opus header: the caps say as much
                source = self.source
                if source:
                    source.emit("push-buffer", Gst.Buffer.new_wrapped(data))
                    self.packets += 1
        except (OSError, ConnectionError):
            pass
        if not self.stopped:
            GLib.idle_add(self._ended, "The phone's sound stopped.")
