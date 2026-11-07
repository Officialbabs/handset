"""A phone's camera as the computer's webcam: what's asked of scrcpy's server, when the phone's
camera runs, and (where PipeWire runs) the camera itself, read by an app, from the stand-in phone."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from handset import android, webcam  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def spin(until, seconds=15.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.01)


class NamesTest(unittest.TestCase):
    def test_a_phone_gets_a_camera_and_a_service_of_its_own(self):
        self.assertEqual(webcam.node_name("29061FDH20075G"), "handset-camera-29061fdh20075g")
        self.assertEqual(webcam.node_name("192.168.1.4:5555"), "handset-camera-192.168.1.4_5555")
        self.assertEqual(webcam.unit_name("ABC"), "handset-camera-abc.service")

    def test_scrcpy_is_asked_for_the_camera_alone(self):
        options = dict(word.split("=", 1) for word in webcam.server_options("0000abcd", "front").split())
        self.assertEqual(options["video_source"], "camera")
        self.assertEqual(options["camera_facing"], "front")
        self.assertEqual(options["audio"], "false")
        self.assertEqual(options["control"], "false")
        self.assertEqual(options["camera_ar"], "16:9")
        self.assertEqual(options["max_size"], "1280")


class RunningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.saved
        self.tmp.cleanup()

    def write(self, pid, facing="front"):
        path = webcam.state_path("P1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as file:
            json.dump({"phone": "P1", "facing": facing, "pid": pid}, file)

    def test_the_window_knows_which_camera_is_on(self):
        self.assertIsNone(webcam.running("P1"))
        self.write(os.getpid())
        self.assertEqual(webcam.running("P1"), "front")

    def test_a_webcam_that_has_gone_is_off(self):
        child = subprocess.Popen(["true"])
        child.wait()
        self.write(child.pid)
        self.assertIsNone(webcam.running("P1"))

    def test_switching_starts_a_service_and_stops_the_last(self):
        calls = []
        with mock.patch.object(webcam.subprocess, "run", side_effect=lambda args, **_k: calls.append(args)):
            webcam.switch("P1", "back")
            webcam.switch("P1", None)
        self.assertEqual(calls[0][:3], ["systemctl", "--user", "stop"])
        self.assertIn("systemd-run", calls[1][0])
        self.assertIn("--unit=handset-camera-p1.service", calls[1])
        self.assertEqual(calls[1][-2:], ["--webcam", "P1"])
        self.assertEqual(len(calls), 3)  # off: only stopped


class WatchedTest(unittest.TestCase):
    """The phone's camera runs while an app watches, and a while after."""

    def setUp(self):
        self.state = "suspended"
        self.cam = webcam.Webcam("P1", "Pixel", look=lambda _name: self.state)
        self.turned = []
        self.cam._phone_on = lambda: (self.turned.append("on"), setattr(self.cam, "phone_on", True))
        self.cam._phone_off = lambda: (self.turned.append("off"), setattr(self.cam, "phone_on", False))

    def test_on_while_watched_off_a_while_after(self):
        self.cam._look()
        self.assertEqual(self.turned, [])
        self.state = "running"
        self.cam._look()
        self.assertEqual(self.turned, ["on"])
        self.state = "idle"
        self.cam._look()
        self.assertEqual(self.turned, ["on"])  # a moment's gap: still on
        self.cam.watched_at -= webcam.LINGER_S + 1
        self.cam._look()
        self.assertEqual(self.turned, ["on", "off"])

    def test_a_camera_that_wont_start_is_left_off(self):
        self.state = "running"
        self.cam.failed = webcam.TRIES
        self.cam._look()
        self.assertEqual(self.turned, [])


def pipewire_here() -> bool:
    Gst.init(None)
    if not (shutil.which("pw-dump") and Gst.ElementFactory.find("pipewiresink")
            and Gst.ElementFactory.find("pipewiresrc") and Gst.ElementFactory.find("openh264enc")):
        return False
    try:
        return subprocess.run(["pw-dump", "--no-colors", "0"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(pipewire_here(), "needs a running PipeWire and GStreamer's PipeWire and openh264 elements")
class LiveTest(unittest.TestCase):
    """The camera on this session's PipeWire, fed by the stand-in phone, read as an app would."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HANDSET_ADB", "FAKEADB_DIR")}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["FAKEADB_DIR"] = self.tmp.name
        # The session's own runtime folder stays (PipeWire's socket is there); the state file doesn't.
        self.state_path = webcam.state_path
        webcam.state_path = lambda phone_id: os.path.join(self.tmp.name, "state", phone_id + ".json")
        self.server = android.scrcpy_server
        android.scrcpy_server = lambda: (__file__, "3.3.4")
        self.linger = webcam.LINGER_S
        webcam.LINGER_S = 1.0
        self.cam = webcam.Webcam(f"TEST{os.getpid()}", "Test phone", find_serial=lambda _p: "FAKE7")

    def tearDown(self):
        self.cam.stop()
        webcam.LINGER_S = self.linger
        webcam.state_path = self.state_path
        android.scrcpy_server = self.server
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_an_app_sees_the_phones_picture_and_the_phone_rests_after(self):
        self.assertTrue(self.cam.start())
        spin(lambda: webcam.node_state(self.cam.name) is not None, 5)
        self.assertFalse(self.cam.phone_on)  # offered, but nobody's watching: the phone rests
        # An app, as one is: a process of its own reading the camera (one frame of grey is 1280×720 bytes).
        frame = 1280 * 720
        app = subprocess.Popen(["gst-launch-1.0", "-q", "pipewiresrc", f"target-object={self.cam.name}", "!",
                                "videoconvert", "!", "video/x-raw,format=GRAY8,width=1280,height=720", "!",
                                "fdsink", "fd=1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frames = []

        def read():
            while True:
                data = app.stdout.read(frame)
                if len(data) < frame:
                    return
                frames.append(max(data[::97]))

        import threading
        threading.Thread(target=read, daemon=True).start()
        try:
            spin(lambda: self.cam.phone_on, 10)
            # The stand-in phone's moving ball, not the black that fills in before it.
            spin(lambda: frames and frames[-1] > 100, 20)
        finally:
            app.kill()
            app.wait()
        spin(lambda: not self.cam.phone_on, 15)
