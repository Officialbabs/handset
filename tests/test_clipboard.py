"""The clipboard shared with the phones, as Handset's windows carry it: a stand-in clipboard and
stand-in windows and links, never the desktop's own clipboard."""

import unittest

from handset.clipboard import ClipboardSync


class FakeClipboard:
    def __init__(self, text=None):
        self.text = text
        self.handlers = []

    def connect(self, _signal, handler):
        self.handlers.append(handler)

    def set(self, value):
        self.text = value.get_string()

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
    def __init__(self, on_text):
        self.on_text = on_text
        self.sent = []
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def set(self, text):
        self.sent.append(text)


class FakeApp:
    def __init__(self):
        self.windows = []

    def handset_windows(self):
        return self.windows


class ClipboardSyncTest(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.sync = ClipboardSync(self.app)
        self.sync.clipboard = FakeClipboard("on the computer")
        self.window = FakeWindow()
        self.app.windows.append(self.window)
        self.sync.watch(self.window)
        self.links = {}
        for phone in ("PIXEL", "IPHONE"):
            self.sync.attach(phone, lambda on_text, p=phone: self.links.setdefault(p, FakeLink(on_text)))

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

    def test_links_go_with_their_windows(self):
        self.sync.release({"IPHONE"})
        self.assertTrue(self.links["PIXEL"].stopped)
        self.assertFalse(self.links["IPHONE"].stopped)
        self.assertEqual(list(self.sync.links), ["IPHONE"])


if __name__ == "__main__":
    unittest.main()
