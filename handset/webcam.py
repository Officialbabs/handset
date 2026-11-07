"""An Android phone's camera as this computer's webcam: a camera of its own on the desktop's
sound-and-video server (PipeWire), named for the phone ("Pixel 7 camera"), that video calls and
camera apps list beside the computer's own.

The phone's camera runs only while an app is watching: the camera is offered all along, and the
phone is asked for its picture (scrcpy's server, its camera as the video source) once an app
starts reading it, and let go a little after the last one stops. Meanwhile the camera shows
black, so an app that opens it never waits on a phone.

`handset --webcam PHONE_ID [--front]` runs it (the phone's window starts it so, as a service of
its own, to outlive the window); it ends when told to (SIGTERM), or when the phone has gone.
Apps that open cameras through PipeWire see it (Firefox and its kin, OBS, GNOME's camera,
anything going through the camera portal); apps that only open V4L2 devices see it when started
through PipeWire's `pw-v4l2`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

from . import APP_ID  # noqa: E402
from .log import debug  # noqa: E402

SIZE = (1280, 720)
FPS = 30
BIT_RATE = 6_000_000
# Nobody has watched for this long: the phone's camera goes off (an app switching cameras, or
# a call's preview reopening it, comes back within it without the phone starting again).
LINGER_S = 20.0
LOOK_EVERY_S = 1.0  # how often the camera is looked at, for an app watching it
IDLE_FPS = 5  # black frames while the phone's camera is off
# The phone's camera didn't start this many times running, with an app watching: it's left off.
TRIES = 3
FACINGS = ("back", "front")


def node_name(phone_id: str) -> str:
    """The camera's name on PipeWire, for this phone."""
    return "handset-camera-" + re.sub(r"[^A-Za-z0-9_.-]", "_", phone_id).lower()


def unit_name(phone_id: str) -> str:
    """The service it runs as, for this phone."""
    return node_name(phone_id) + ".service"


def server_options(scid: str, facing: str, size: tuple[int, int] = SIZE, fps: int = FPS,
                   bit_rate: int = BIT_RATE) -> str:
    """scrcpy's server, the phone's camera its video: H.264, no sound, no control, landscape
    16:9 at most `size`."""
    return (f"scid={scid} log_level=warn tunnel_forward=true audio=false control=false "
            "cleanup=false send_device_meta=false send_frame_meta=false send_dummy_byte=true "
            f"send_codec_meta=true video_codec=h264 video_source=camera camera_facing={facing} "
            f"camera_ar=16:9 max_size={max(size)} camera_fps={fps} video_bit_rate={bit_rate}")


def state_path(phone_id: str) -> str:
    """Where the running webcam says what it is (its phone, which camera, its process)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or GLib.get_user_runtime_dir()
    return os.path.join(runtime, "handset", node_name(phone_id) + ".json")


def running(phone_id: str) -> str | None:
    """Which of the phone's cameras is its webcam now ("back", "front"), or None."""
    try:
        with open(state_path(phone_id)) as file:
            state = json.load(file)
        os.kill(int(state["pid"]), 0)
        return state["facing"] if state.get("facing") in FACINGS else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def available() -> str | None:
    """Why a phone's camera can't be a webcam here, or None when it can."""
    Gst.init(None)
    if not Gst.ElementFactory.find("pipewiresink"):
        return "Install gstreamer1.0-pipewire to use a phone's camera as a webcam."
    from .decoder import decoder

    if not decoder("h264"):
        return "Install gstreamer1.0-libav to use a phone's camera as a webcam."
    return None


def node_state(name: str) -> str | None:
    """The PipeWire node's state ("running" while an app reads it), or None when it isn't there."""
    try:
        dumped = subprocess.run(["pw-dump", "--no-colors", name], capture_output=True, text=True,
                                timeout=5).stdout
        for item in json.loads(dumped or "[]"):
            info = item.get("info") or {}
            if (info.get("props") or {}).get("node.name") == name:
                return info.get("state")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


class Webcam:
    """One phone's camera, offered on PipeWire; started on the phone while an app watches."""

    def __init__(self, phone_id: str, phone_name: str, facing: str = "back",
                 find_serial=None, look=node_state):
        self.phone_id = phone_id
        self.phone_name = phone_name
        self.facing = facing if facing in FACINGS else "back"
        self.name = node_name(phone_id)
        self.find_serial = find_serial  # phone id → adb serial, or None (android.find_serial)
        self.look = look
        self.out: Gst.Pipeline | None = None
        self.src = None
        self.decode: Gst.Pipeline | None = None
        self.phone_on = False
        self.session = 0
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.forward: tuple[str, int] | None = None
        self.last_frame = 0.0
        self.watched_at = 0.0
        self.failed = 0
        self.timers: list[int] = []
        self.black = bytes([16, 128] * (SIZE[0] * SIZE[1]))  # YUY2's black, narrow range
        self.stopped = False

    # -- the camera on PipeWire

    def start(self) -> bool:
        Gst.init(None)
        self.out = Gst.parse_launch(
            f"appsrc name=src is-live=true format=time do-timestamp=true block=false "
            f"caps=video/x-raw,format=YUY2,width={SIZE[0]},height={SIZE[1]},framerate={FPS}/1 "
            "! queue leaky=downstream max-size-buffers=2 "
            "! pipewiresink name=sink mode=provide sync=false")
        sink = self.out.get_by_name("sink")
        props = Gst.Structure.new_empty("props")
        for key, value in (("media.class", "Video/Source"), ("media.role", "Camera"),
                           ("node.name", self.name), ("node.description", f"{self.phone_name} camera"),
                           ("node.nick", f"{self.phone_name} camera"), ("media.name", f"{self.phone_name} camera")):
            props.set_value(key, value)
        sink.set_property("stream-properties", props)
        sink.set_property("client-name", "Handset")
        self.src = self.out.get_by_name("src")
        if self.out.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return False
        self._write_state()
        self.timers.append(GLib.timeout_add(int(1000 / IDLE_FPS), self._idle_frame))
        self.timers.append(GLib.timeout_add(int(LOOK_EVERY_S * 1000), self._look))
        debug("webcam:", self.name, "offered,", self.facing, "camera")
        return True

    def stop(self):
        self.stopped = True
        for timer in self.timers:
            GLib.source_remove(timer)
        self.timers = []
        self._phone_off()
        if self.out:
            self.out.set_state(Gst.State.NULL)
            self.out = None
        try:
            os.unlink(state_path(self.phone_id))
        except OSError:
            pass

    def _write_state(self):
        path = state_path(self.phone_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w") as file:
            json.dump({"phone": self.phone_id, "facing": self.facing, "pid": os.getpid()}, file)
        os.replace(path + ".new", path)

    def _push(self, data: bytes):
        if self.src:
            self.src.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    def _idle_frame(self):
        """Black while the phone's picture isn't coming, so a watching app never stalls."""
        if time.monotonic() - self.last_frame > 0.5:
            self._push(self.black)
        return GLib.SOURCE_CONTINUE

    def _look(self):
        """An app watching: the phone's camera on; none for a while: off."""
        watched = self.look(self.name) == "running"
        now = time.monotonic()
        if watched:
            self.watched_at = now
            if not self.phone_on and self.failed < TRIES:
                self._phone_on()
        elif self.phone_on and now - self.watched_at > LINGER_S:
            debug("webcam: nobody watching; the phone's camera goes off")
            self._phone_off()
        if not watched and now - self.watched_at > LINGER_S:
            self.failed = 0  # the next app to watch tries again
        return GLib.SOURCE_CONTINUE

    # -- the phone's side

    def _phone_on(self):
        self.phone_on = True
        self.session += 1
        Gst.init(None)
        from .decoder import decoder

        self.decode = Gst.parse_launch(
            "appsrc name=h264 is-live=true format=time do-timestamp=true "
            "caps=video/x-h264,stream-format=byte-stream "
            f"! h264parse ! {decoder('h264')} ! videoconvert ! videoscale add-borders=true "
            f"! video/x-raw,format=YUY2,width={SIZE[0]},height={SIZE[1]},pixel-aspect-ratio=1/1 "
            "! appsink name=frames emit-signals=true sync=false max-buffers=2 drop=true")
        self.decode.get_by_name("frames").connect("new-sample", self._frame)
        self.decode.set_state(Gst.State.PLAYING)
        threading.Thread(target=self._stream, args=(self.session, self.decode.get_by_name("h264")),
                         daemon=True).start()

    def _phone_off(self):
        self.phone_on = False
        self.session += 1
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
        if self.sock:
            self.sock.close()
            self.sock = None
        forward, self.forward = self.forward, None
        if forward:
            from .android import adb

            try:
                adb(forward[0], "forward", "--remove", f"tcp:{forward[1]}", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        if self.decode:
            self.decode.set_state(Gst.State.NULL)
            self.decode = None

    def _frame(self, appsink):
        sample = appsink.emit("pull-sample")
        if sample:
            buffer = sample.get_buffer().copy_deep()
            buffer.pts = buffer.dts = Gst.CLOCK_TIME_NONE  # timed as it reaches the camera
            self.last_frame = time.monotonic()
            if self.src:
                self.src.emit("push-buffer", buffer)
        return Gst.FlowReturn.OK

    def _stream(self, session: int, h264src):
        """The phone's camera, through scrcpy's server, into the decoder (off the main loop)."""
        from . import android

        serial = self.find_serial(self.phone_id) if self.find_serial else None
        why = None
        if not serial:
            why = f"{self.phone_name} isn't connected"
        else:
            try:
                why = self._scrcpy(session, serial, h264src)
            except (OSError, subprocess.SubprocessError, ValueError, struct.error) as error:
                why = str(error) or type(error).__name__
        if session == self.session and not self.stopped:
            GLib.idle_add(self._phone_stopped, session, why or "the phone's camera stopped")
        del android

    def _scrcpy(self, session: int, serial: str, h264src) -> str | None:
        from .android import PHONE_SERVER, adb, adb_command, push_scrcpy_server, read_exactly, scrcpy_version

        version = scrcpy_version()
        if not version:
            return "scrcpy's server isn't on this computer"
        scid = f"{secrets.randbits(31):08x}"
        push_scrcpy_server(serial)
        port = int(adb(serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}").strip())
        self.forward = (serial, port)
        command = adb_command(serial, "shell", f"CLASSPATH={PHONE_SERVER} app_process / "
                              f"com.genymobile.scrcpy.Server {version} {server_options(scid, self.facing)}")
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sock = None
        for _ in range(80):
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
            said = b""
            if self.process and self.process.poll() is not None and self.process.stderr:
                said = self.process.stderr.read()[-400:]
            return said.decode(errors="replace").strip().splitlines()[-1] if said.strip() else \
                "the phone's camera didn't start"
        self.sock = sock
        sock.settimeout(None)
        codec, width, height = struct.unpack(">III", read_exactly(sock, 12))
        debug("webcam: phone camera", hex(codec), width, height)
        if codec != 0x68323634:  # "h264"
            return "the phone can't send H.264 video"
        self.failed = 0
        while session == self.session:
            chunk = sock.recv(65536)
            if not chunk:
                break
            h264src.emit("push-buffer", Gst.Buffer.new_wrapped(chunk))
        return None

    def _phone_stopped(self, session: int, why: str):
        if session != self.session or self.stopped:
            return GLib.SOURCE_REMOVE
        debug("webcam: the phone's camera stopped:", why)
        self._phone_off()
        self.failed += 1
        if self.failed == TRIES:
            tell(f"{self.phone_name}'s camera didn't start", why[:200])
        return GLib.SOURCE_REMOVE


def tell(summary: str, body: str):
    """A desktop notification, Handset's."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                      "org.freedesktop.Notifications", "Notify",
                      GLib.Variant("(susssasa{sv}i)", ("Handset", 0, APP_ID, summary, body, [],
                                                        {"desktop-entry": GLib.Variant("s", APP_ID)}, 8000)),
                      None, Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error as error:
        debug("webcam: no notification:", error)


# ---- Started and stopped from the phone's window ------------------------------------------------

def switch(phone_id: str, facing: str | None):
    """The phone's camera as the webcam (`facing`: "back" or "front"), or not (None): a service of
    its own, so it outlives the window."""
    unit = unit_name(phone_id)
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=10)
    if facing not in FACINGS:
        return
    from .apps import launcher

    args = [launcher(), "--webcam", phone_id] + (["--front"] if facing == "front" else [])
    try:
        subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
                        "--description=A phone's camera as a webcam (Handset)"] + args,
                       capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def main(argv: list[str]) -> int:
    """`handset --webcam PHONE_ID [--front]`."""
    rest = [a for a in argv if not a.startswith("--")]
    if not rest:
        print("handset --webcam PHONE_ID [--front]")
        return 2
    phone_id = rest[0]
    why = available()
    if why:
        print(why)
        tell("The phone's camera can't be a webcam", why)
        return 1
    from .android import find_serial
    from .service import DISCONNECTED, watch

    # The phone as Handset knows it: its name, where it is; switched off in Handset, the webcam ends.
    known: dict[str, object] = {}
    loop = GLib.MainLoop()

    def heard(phones):
        phone = next((p for p in phones if p.id == phone_id), None)
        known["phone"] = phone
        if phone and phone.problem == DISCONNECTED and "webcam" in known:
            debug("webcam:", phone.name, "was switched off in Handset")
            loop.quit()

    phones = watch(heard)
    phones.start()
    context = GLib.MainContext.default()
    end = time.monotonic() + 5
    while "phone" not in known and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.02)
    phone = known.get("phone")
    if phone and phone.platform != "android":
        print("Only an Android phone's camera can be a webcam.")
        return 1
    webcam = Webcam(phone_id, phone.name if phone else "Phone", "front" if "--front" in argv else "back",
                    find_serial=lambda pid: find_serial(pid, getattr(known.get("phone"), "address", None)))
    known["webcam"] = webcam
    for number in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, loop.quit)
    if not webcam.start():
        print("The camera couldn't be offered (PipeWire).")
        return 1
    loop.run()
    webcam.stop()
    phones.stop()
    return 0
