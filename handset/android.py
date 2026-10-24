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
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from queue import Queue

from gi.repository import GLib  # noqa: E402

from .decoder import Decoder  # noqa: E402
from .gestures import Gesture  # noqa: E402

from .log import DEBUG, debug  # noqa: E402

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
# Enough for a whole phone screen at 60 frames a second without blocky moving text.
BIT_RATE = 12_000_000


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
        self.decoder = Decoder("h264", on_frame, self._decoder_broken)
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.forward_port: int | None = None
        self.edge = MAX_EDGE  # the video's longest edge (0: the screen's own size)
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
                              f"--bit-rate {BIT_RATE} -")
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
                   f"video_bit_rate={BIT_RATE} max_fps=60")
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
                 edge: int | None = None):
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
