"""Handset tests for listening to Ada's phone service, against a stand-in service."""

import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset import service
from handset.service import Phone, Watch, ada_line, phones_in, state_of, status_line
from tests.fakeservice import FakeService, phone


def spin(until, seconds=3.0):
    """Run GLib's main loop until `until()` is true, or fail after `seconds`."""
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.005)


class SocketPathTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("ADA_PHONES_SOCKET", "XDG_RUNTIME_DIR")}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_as_ada_looks_for_it(self):
        os.environ["ADA_PHONES_SOCKET"] = "/x/phones.sock"
        self.assertEqual(service.socket_path(), "/x/phones.sock")
        del os.environ["ADA_PHONES_SOCKET"]
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/7"
        self.assertEqual(service.socket_path(), "/run/user/7/ada/phones.sock")
        del os.environ["XDG_RUNTIME_DIR"]
        self.assertEqual(service.socket_path(), f"/tmp/ada-{os.getuid()}/ada/phones.sock")


class StateTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(state_of('{"ok":true,"state":{"phones":[]}}'), {"phones": []})
        self.assertEqual(state_of('{"event":"state","state":{"phones":[]}}'), {"phones": []})
        self.assertIsNone(state_of('{"event":"frame","jpeg":"…"}'))
        self.assertIsNone(state_of('{"ok":true,"st'))
        self.assertIsNone(state_of("[]"))

    def test_phones(self):
        phones = phones_in({"phones": [phone(), phone(id="", name="nameless"), "junk"]})
        self.assertEqual([p.name for p in phones], ["Pixel 7"])
        pixel = phones[0]
        self.assertFalse(pixel.online)
        self.assertFalse(pixel.ada_working)
        self.assertEqual(phones_in({}), [])

    def test_ada_at_work(self):
        busy = Phone.from_state(phone(wifi=True, using={"since": 1, "doing": "Tapping", "paused": False}))
        self.assertTrue(busy.online)
        self.assertEqual(ada_line(busy), "Ada: tapping…")
        waiting = Phone.from_state(phone(using={"since": 1, "doing": "Tapping", "paused": True}))
        self.assertEqual(ada_line(waiting), "Ada is waiting while you use it")
        idle = Phone.from_state(phone())
        self.assertEqual(ada_line(idle), "")

    def test_status(self):
        self.assertEqual(status_line(Phone.from_state(phone(usb=True, wifi=True))), "Connected by cable")
        self.assertEqual(status_line(Phone.from_state(phone(wifi=True))), "Connected over Wi-Fi")
        self.assertEqual(status_line(Phone.from_state(phone())), "Not connected")
        self.assertEqual(status_line(Phone.from_state(phone(problem="It's locked"))), "It's locked")
        iphone = Phone.from_state(phone(platform="ios", name="Babs iPhone"))
        self.assertEqual(iphone.platform, "ios")


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "phones.sock")
        self.fake = FakeService(self.path, [phone()])
        self.seen = []
        self.gone = 0
        self.watch = Watch(self.seen.append, self._gone, path=self.path)

    def _gone(self):
        self.gone += 1

    def tearDown(self):
        self.watch.stop()
        self.fake.close()
        self.tmp.cleanup()

    def test_first_state_then_changes(self):
        self.watch.start()
        spin(lambda: len(self.seen) == 1)
        self.assertEqual(self.fake.requests, [{"op": "watch"}])
        self.assertFalse(self.seen[0][0].ada_working)
        self.fake.push([phone(wifi=True, using={"since": 1, "doing": "Typing", "paused": False})])
        spin(lambda: len(self.seen) == 2)
        self.assertEqual(ada_line(self.seen[1][0]), "Ada: typing…")

    def test_comes_back_after_a_drop(self):
        service.RECONNECT_MS = 50
        try:
            self.watch.start()
            spin(lambda: len(self.seen) == 1)
            self.fake.drop_watchers()
            spin(lambda: self.gone == 1 and len(self.seen) == 2)
        finally:
            service.RECONNECT_MS = 2000

    def test_no_service_yet(self):
        watch = Watch(self.seen.append, self._gone, path=os.path.join(self.tmp.name, "none.sock"))
        watch.start()
        spin(lambda: self.gone == 1)
        watch.stop()
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
