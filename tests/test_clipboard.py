"""The clipboard shared with the phones, as Handset's windows carry it: a stand-in clipboard and
stand-in windows and links, never the desktop's own clipboard."""

import os
import tempfile
import unittest

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from handset import clipboard  # noqa: E402
from handset.clipboard import ClipboardSync, image_digest  # noqa: E402


class FakeClipboard:
    def __init__(self, text=None):
        self.text = text
        self.handlers = []

    def connect(self, _signal, handler):
        self.handlers.append(handler)

    def set(self, value):
        self.text = value.get_string()

    def set_texture(self, texture):
        self.text, self.texture = None, texture

    def is_local(self):
        return False

    def read_text_async(self, _cancellable, callback):
        callback(self, None)

    def read_text_finish(self, _result):
        return self.text


class FakeWindow:
    def __init__(self):
        self.active = False
        self.handlers = []

    def is_active(self):
        return self.active

    def connect(self, _signal, handler):
        self.handlers.append(handler)

    def comes_forward(self):
        self.active = True
        for handler in self.handlers:
            handler(self, None)


class FakeLink:
    def __init__(self, on_text, on_image):
        self.on_text = on_text
        self.on_image = on_image
        self.sent = []
        self.images = []
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def set(self, text, then=None):
        self.sent.append(text)
        if then:
            then()

    def set_image(self, path, mime, then=None):
        self.images.append((path, mime))
        if then:
            then()


class FakeApp:
    def __init__(self):
        self.windows = []

    def handset_windows(self):
        return self.windows


class ClipboardSyncTest(unittest.TestCase):
    def setUp(self):
        # Never the desktop's own clipboard: wl-copy stands in, saying the desktop won't let it.
        self.copied_out = []
        self.wl_copy = clipboard.wl_copy
        clipboard.wl_copy = lambda data, mime: False
        self.addCleanup(setattr, clipboard, "wl_copy", self.wl_copy)
        self.app = FakeApp()
        self.sync = ClipboardSync(self.app)
        self.sync.clipboard = FakeClipboard("on the computer")
        self.window = FakeWindow()
        self.app.windows.append(self.window)
        self.sync.watch(self.window)
        self.links = {}
        for phone in ("PIXEL", "IPHONE"):
            self.sync.attach(phone, lambda on_text, on_image, p=phone: self.links.setdefault(p, FakeLink(on_text, on_image)))

    def test_a_window_coming_forward_sends_the_computers_text(self):
        self.window.comes_forward()
        self.assertEqual(self.links["PIXEL"].sent, ["on the computer"])
        self.assertEqual(self.links["IPHONE"].sent, ["on the computer"])
        self.window.comes_forward()  # the same text: not again
        self.assertEqual(self.links["PIXEL"].sent, ["on the computer"])

    def test_copied_on_a_phone_waits_for_a_window_to_come_forward(self):
        self.links["PIXEL"].on_text("on the phone")
        self.assertEqual(self.sync.clipboard.text, "on the computer")  # not in front: it can't be set yet
        self.assertEqual(self.links["IPHONE"].sent, ["on the phone"])  # the other phone has it at once
        self.window.comes_forward()
        self.assertEqual(self.sync.clipboard.text, "on the phone")
        self.assertEqual(self.links["PIXEL"].sent, [])  # its own: not sent back

    def test_copied_on_a_phone_while_a_window_is_in_front(self):
        self.window.active = True
        self.links["IPHONE"].on_text("on the iPhone")
        self.assertEqual(self.sync.clipboard.text, "on the iPhone")

    def picture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "picture.png")
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 4, 4)
        pixbuf.fill(0x3366CCFF)
        pixbuf.savev(path, "png", [], [])
        return path

    def test_an_image_copied_here_goes_to_the_phones(self):
        picture = self.picture()
        self.sync.clipboard.text = None  # an image, not text
        self.sync._read_image = lambda done: done((picture, "image/png"))
        self.window.comes_forward()
        self.assertEqual(self.links["PIXEL"].images, [(picture, "image/png")])
        self.window.comes_forward()  # the same image: not again
        self.assertEqual(len(self.links["IPHONE"].images), 1)

    def test_ctrl_v_puts_the_image_on_the_phone_first(self):
        picture = self.picture()
        self.sync.clipboard.text = None
        self.sync._read_image = lambda done: done((picture, "image/png"))
        pasted = []
        self.sync.paste_then("IPHONE", lambda: pasted.append(list(self.links["IPHONE"].images)))
        self.assertEqual(pasted, [[(picture, "image/png")]])  # the image was there when it pasted
        self.assertEqual(self.sync.sent_image, image_digest(picture))

    def test_an_image_copied_on_the_iphone(self):
        picture = self.picture()
        self.window.active = True
        self.links["IPHONE"].on_image(picture, "image/png")
        self.assertIsNotNone(self.sync.clipboard.texture)  # on the computer's clipboard
        self.assertEqual(self.links["PIXEL"].images, [(picture, "image/png")])  # and the other phone's

    def test_copied_on_a_phone_with_no_window_in_front(self):
        # Where the desktop lets a clipboard manager set it, it's there at once, whatever's in front.
        def copy_out(data, mime):
            self.copied_out.append((data, mime))
            self.sync.clipboard.text = data.decode() if mime.startswith("text/") else None
            return True

        clipboard.wl_copy = copy_out
        self.links["PIXEL"].on_text("on the phone")
        self.assertEqual(self.copied_out, [(b"on the phone", "text/plain;charset=utf-8")])
        self.assertIsNone(self.sync.pending)  # nothing left waiting for a window
        self.window.comes_forward()
        self.assertEqual(self.links["PIXEL"].sent, [])  # not sent back to the phone it came from
        picture = self.picture()
        self.links["IPHONE"].on_image(picture, "image/png")
        with open(picture, "rb") as file:
            self.assertEqual(self.copied_out[-1], (file.read(), "image/png"))

    def test_wl_copy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = os.path.join(tmp.name, "wl-copy")
        with open(tool, "w") as file:
            file.write(f"#!/bin/sh\necho \"$@\" > {tmp.name}/args\ncat > {tmp.name}/data\n")
        os.chmod(tool, 0o755)
        saved = {k: os.environ.get(k) for k in ("PATH", "WAYLAND_DISPLAY")}
        os.environ["PATH"] = tmp.name + os.pathsep + os.environ["PATH"]
        os.environ["WAYLAND_DISPLAY"] = "test-display"
        try:
            self.assertTrue(self.wl_copy(b"\x89PNG", "image/png"))
            with open(os.path.join(tmp.name, "data"), "rb") as file:
                self.assertEqual(file.read(), b"\x89PNG")
            with open(os.path.join(tmp.name, "args")) as file:
                self.assertEqual(file.read().split(), ["--type", "image/png"])
            os.environ.pop("WAYLAND_DISPLAY")
            self.assertFalse(self.wl_copy(b"text", "text/plain"))  # not Wayland: the window's way
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_links_go_with_their_windows(self):
        self.sync.release({"IPHONE"})
        self.assertTrue(self.links["PIXEL"].stopped)
        self.assertFalse(self.links["IPHONE"].stopped)
        self.assertEqual(list(self.sync.links), ["IPHONE"])


if __name__ == "__main__":
    unittest.main()
