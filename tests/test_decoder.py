"""Handset tests for decoding: the picture's colours come out right, whichever layout the
decoder gives (three planes from software, luma and interleaved chroma from GPUs)."""

import subprocess
import threading
import time
import unittest

from handset.decoder import Decoder, hardware, kept_size  # first: it says which GTK
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# GPU frames reach GTK through its display: there's one only where GTK can start.
DISPLAY = Gtk.init_check()

# GStreamer's colour bars, as H.264.
BARS = ("videotestsrc num-buffers=60 pattern=smpte ! video/x-raw,width=720,height=480,framerate=30/1 ! "
        "videoconvert ! openh264enc ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream ! "
        "fdsink fd=1")


def decode(formats, gpu_frames=False):
    frames = []
    shown = []

    def show(texture):
        # As the window does: only the newest frame is kept. A GPU frame holds one of the
        # decoder's buffers until GTK lets go of it, so keeping them all would starve it.
        shown[:] = [texture]
        frames.append(None)

    decoder = Decoder("h264", show, formats=formats, gpu_frames=gpu_frames)
    decoder.start()
    encoder = subprocess.Popen(["gst-launch-1.0", "-q"] + BARS.split(), stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)

    def feed():
        while chunk := encoder.stdout.read1(65536):
            decoder.push(chunk)

    threading.Thread(target=feed, daemon=True).start()
    end = time.monotonic() + 15
    while len(frames) < 3 and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    decoder.stop()
    encoder.kill()
    return shown[-1]


def pixel(texture, x, y):
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    downloader.set_color_state(Gdk.ColorState.get_srgb())
    data, stride = downloader.download_bytes()
    i = y * stride + x * 4
    return tuple(data.get_data()[i:i + 3])


class ColoursTest(unittest.TestCase):
    def check_bars(self, texture):
        # White, yellow and blue bars, near enough (compression shifts them a step or two).
        for (x, want) in ((50, (255, 255, 255)), (150, (255, 255, 0)), (670, (0, 0, 255))):
            got = pixel(texture, x, 100)
            self.assertTrue(all(abs(g - w) <= 12 for g, w in zip(got, want)), (x, got, want))

    def test_three_planes(self):
        texture = decode(("I420",))
        self.assertEqual(texture.get_format(), Gdk.MemoryFormat.G8_B8_R8_420)
        self.check_bars(texture)

    def test_interleaved_chroma_as_from_a_gpu(self):
        texture = decode(("NV12",))
        self.assertEqual(texture.get_format(), Gdk.MemoryFormat.G8_B8R8_420)
        self.check_bars(texture)

    @unittest.skipUnless(hardware("h264") and DISPLAY, "no GPU decoder, or no display, here")
    def test_frames_left_in_the_gpus_memory(self):
        texture = decode(("NV12",), gpu_frames=True)
        self.assertIsInstance(texture, Gdk.DmabufTexture)
        self.check_bars(texture)

    def test_made_twice_the_size_shown(self):
        frames = []
        decoder = Decoder("h264", frames.append, gpu_frames=False)
        decoder.native = (1080, 2400)
        decoder.show_at(270, 600)
        self.assertEqual(decoder.scaled_size(), (540, 1200))
        decoder.show_at(1000, 2222)
        self.assertIsNone(decoder.scaled_size())  # as big as it is: as it is

    def test_the_gpus_frames_made_for_the_window(self):
        # An iPhone 12's padded frames are made so their screen's part is exactly twice the
        # pixels the window shows, for the window to halve.
        decoder = Decoder("h265", lambda t: None, gpu_frames=True, crop=lambda size: (1170, 2532))
        decoder.dmabuf = True
        decoder.native, decoder.native_crop = (1184, 2576), (1170, 2532)
        decoder.show_at(275, 595)
        made = decoder.scaled_size()
        self.assertEqual(decoder.exact, (made, (550, 1190)))
        self.assertEqual((made[0] % 2, made[1] % 2), (0, 0))
        self.assertGreaterEqual(made[0] * 1170 / 1184, 550)  # its screen's part covers what's kept
        self.assertGreaterEqual(made[1] * 2532 / 2576, 1190)
        decoder.show_at(700, 1500)  # more than half the phone's screen: as decoded
        self.assertIsNone(decoder.scaled_size())
        decoder.native, decoder.native_crop = (1080, 2400), None  # a Pixel's, not padded
        decoder.show_at(271, 600)
        self.assertEqual(decoder.scaled_size(), (542, 1200))

    def test_the_screens_part_scales_too(self):
        decoder = Decoder("h265", lambda t: None, gpu_frames=False, crop=lambda size: (1170, 2532))
        decoder.native, decoder.native_crop = (1184, 2576), (1170, 2532)
        decoder.show_at(275, 595)
        width, height = decoder.scaled_size()
        self.assertAlmostEqual(width / 1184, 550 / 1170, places=2)
        self.assertAlmostEqual(height / 2576, 1190 / 2532, places=2)

    def test_the_screens_part_is_even(self):
        # An iPhone 12's frames made 556×1210: its screen's part, 549.4×1189.3, as 549×1189 GTK
        # refused for the GPU (NV12 needs whole pairs of pixels).
        self.assertEqual(kept_size((556, 1210), (1184, 2576), (1170, 2532)), (550, 1190))
        self.assertEqual(kept_size((1184, 2576), (1184, 2576), (1170, 2532)), (1170, 2532))
        width, height = kept_size((375, 813), (1184, 2576), (1170, 2532))
        self.assertEqual((width % 2, height % 2), (0, 0))
        self.assertLessEqual(width, 374)

    def test_scaled_frames_decode_at_that_size(self):
        frames, shown = [], []
        decoder = Decoder("h264", lambda t: shown.append(t), formats=("I420",), gpu_frames=False)
        # Said up front: a short clip can be decoded whole before the size learned from it applies.
        decoder.native = (720, 480)
        decoder.show_at(180, 120)
        decoder.start()
        encoder = subprocess.Popen(["gst-launch-1.0", "-q"] + BARS.split(), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        threading.Thread(target=lambda: [decoder.push(c) for c in iter(lambda: encoder.stdout.read1(65536), b"")],
                         daemon=True).start()
        end = time.monotonic() + 10
        while not (shown and shown[-1].get_width() == 360) and time.monotonic() < end:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.005)
        decoder.stop()
        encoder.kill()
        self.assertEqual((shown[-1].get_width(), shown[-1].get_height()), (360, 240))

    def test_hardware_is_a_yes_or_no(self):
        self.assertIn(hardware("h264"), (True, False))


if __name__ == "__main__":
    unittest.main()
