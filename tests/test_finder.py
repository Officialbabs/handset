"""Phones Handset finds itself, where the desktop has no phone service: adb's list (through the
stand-in adb, fakeadb.py) and usbmuxd's (a stand-in usbmuxd on a socket of the test's own), the
phones remembered while away, and one finder shared by every window."""

import io
import os
import plistlib
import socket
import struct
import tempfile
import threading
import time
import unittest

from gi.repository import GLib

from handset import finder, service
from handset.finder import (OwnWatch, Subscription, iphone_model, load_known, parse_track, read_frames,
                            save_known)

HERE = os.path.dirname(os.path.abspath(__file__))
UDID = "00000000-0000000000000001"


def spin(until, seconds=8.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    return until()


class FakeUsbmuxd:
    """usbmuxd as Handset asks it: Listen (one iPhone attached, `trusted` or not), ReadPairRecord,
    and Connect to the phone's lockdown, whose GetValue says its name and model."""

    def __init__(self, path: str, trusted: bool = True):
        self.trusted = trusted
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(8)
        self.listeners: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.server.close()
        for sock in self.listeners:
            sock.close()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    @staticmethod
    def _send(sock, message):
        body = plistlib.dumps(message)
        sock.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)

    def _serve(self, sock):
        message = finder.mux_read(sock)
        kind = message.get("MessageType") if message else None
        if kind == "Listen":
            self._send(sock, {"MessageType": "Result", "Number": 0})
            self._send(sock, {"MessageType": "Attached", "DeviceID": 3,
                              "Properties": {"ConnectionType": "USB", "SerialNumber": UDID, "DeviceID": 3}})
            self.listeners.append(sock)
            return  # kept open, as usbmuxd keeps a listener's
        if kind == "ReadPairRecord":
            self._send(sock, {"PairRecordData": b"<plist/>"} if self.trusted else {"Number": 2})
        elif kind == "Connect":
            self._send(sock, {"MessageType": "Result", "Number": 0})
            values = {"DeviceName": "Test iPhone", "ProductType": "iPhone13,2"}
            try:
                while True:
                    head = finder.recv_all(sock, 4)
                    if not head:
                        break
                    asked = plistlib.loads(finder.recv_all(sock, struct.unpack(">I", head)[0]))
                    body = plistlib.dumps({"Key": asked["Key"], "Value": values.get(asked["Key"], "")})
                    sock.sendall(struct.pack(">I", len(body)) + body)
            except OSError:
                pass
        sock.close()

    def detach(self):
        for sock in self.listeners:
            self._send(sock, {"MessageType": "Detached", "DeviceID": 3})


class ReadingTest(unittest.TestCase):
    def test_adbs_list(self):
        listed = parse_track("0A1B2C3D4E5F6G         device usb:3-1 product:panther model:Pixel_7 transport_id:15\n"
                             "192.168.1.5:5555       device product:panther model:Pixel_7 transport_id:16\n"
                             "R58M123               unauthorized usb:3-2 transport_id:17\n"
                             "R58M456               no permissions (missing udev rules?); see [http://...] usb:3-3\n")
        self.assertEqual([(d.serial, d.state, d.usb) for d in listed], [
            ("0A1B2C3D4E5F6G", "device", True), ("192.168.1.5:5555", "device", False),
            ("R58M123", "unauthorized", True), ("R58M456", "no permissions", True)])
        self.assertEqual(parse_track(""), [])

    def test_adbs_frames(self):
        got = []
        read_frames(io.BytesIO(b"0005hello0000"), got.append)
        self.assertEqual(got, ["hello", ""])

    def test_batteries(self):
        from handset.finder import parse_battery

        self.assertEqual(parse_battery("  AC powered: false\n  USB powered: true\n  level: 39\n"), (39, True))
        self.assertEqual(parse_battery("  AC powered: false\n  USB powered: false\n  level: 100\n"), (100, False))
        self.assertEqual(parse_battery(""), (None, False))

    def test_models(self):
        self.assertEqual(iphone_model("iPhone13,2"), "iPhone 12")
        self.assertEqual(iphone_model("iPhone99,1"), "iPhone")


class Finding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HANDSET_ADB", "FAKEADB_DIR", "XDG_CONFIG_HOME")}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["FAKEADB_DIR"] = self.tmp.name
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        self.usbmuxd_path = finder.USBMUXD
        finder.USBMUXD = os.path.join(self.tmp.name, "usbmuxd")
        self.again = finder.AGAIN_S
        finder.AGAIN_S = 0.2

    def tearDown(self):
        finder.USBMUXD = self.usbmuxd_path
        finder.AGAIN_S = self.again
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.tmp.name, name + ".new"), "w") as file:
            file.write(text)
        os.replace(os.path.join(self.tmp.name, name + ".new"), os.path.join(self.tmp.name, name))


class WatchTest(Finding):
    def test_both_kinds_found_and_remembered(self):
        usbmuxd = FakeUsbmuxd(finder.USBMUXD)
        self.write("track", "FAKE7                  device usb:3-1 product:fake model:Pixel_7 transport_id:1\n")
        seen = []
        watch = OwnWatch(seen.append)
        watch.start()
        try:
            self.assertTrue(spin(lambda: seen and len([p for p in seen[-1] if p.online]) == 2))
            found = {p.id: p for p in seen[-1]}
            iphone, pixel = found[UDID], found["FAKE7"]
            self.assertEqual((iphone.id, iphone.name, iphone.model, iphone.usb, iphone.problem),
                             (UDID, "Test iPhone", "iPhone 12", True, None))
            self.assertEqual((pixel.id, pixel.name, pixel.model, pixel.platform, pixel.usb),
                             ("FAKE7", "Fake Pixel", "Pixel 7", "android", True))
            self.assertEqual((pixel.system_name, pixel.battery, pixel.charging), ("Android 17", 80, True))
            # Unplugged, they're listed still, away.
            self.write("track", "")
            usbmuxd.detach()
            self.assertTrue(spin(lambda: not any(p.online for p in seen[-1])))
            self.assertEqual({p.id for p in seen[-1]}, {UDID, "FAKE7"})
            known = load_known()["FAKE7"]
            self.assertEqual({k: known[k] for k in ("name", "platform", "model", "system")},
                             {"name": "Fake Pixel", "platform": "android", "model": "Pixel 7", "system": "17"})
            self.assertAlmostEqual(known["last_seen"], time.time(), delta=30)  # when it was last connected
            away = next(p for p in seen[-1] if p.id == "FAKE7")
            self.assertEqual((away.online, away.system), (False, "17"))
            watch.forget("FAKE7")
            self.assertEqual([p.id for p in seen[-1]], [UDID])
        finally:
            watch.stop()
            usbmuxd.close()

    def test_phones_not_ready_yet(self):
        usbmuxd = FakeUsbmuxd(finder.USBMUXD, trusted=False)
        self.write("track", "FAKE7                  unauthorized usb:3-1 transport_id:1\n")
        seen = []
        watch = OwnWatch(seen.append)
        watch.start()
        try:
            self.assertTrue(spin(lambda: len(watch.waiting) == 2))
            self.assertEqual(watch.waiting, {"adb:FAKE7": finder.WAITING["unauthorized"],
                                             f"ios:{UDID}": finder.UNTRUSTED})
            iphone = next(p for p in seen[-1] if p.id == UDID)
            self.assertEqual(iphone.problem, finder.UNTRUSTED)
            self.assertNotIn(UDID, load_known())  # remembered once it's set up
        finally:
            watch.stop()
            usbmuxd.close()


class SharingTest(Finding):
    def test_one_finder_for_every_look(self):
        save_known({"FAKE7": {"name": "Fake Pixel", "platform": "android", "model": "Pixel 7"}})
        first, second = [], []
        one, two = Subscription(first.append), Subscription(second.append)
        one.start()
        two.start()
        try:
            hub = Subscription.hub
            self.assertTrue(spin(lambda: first and second))
            self.assertEqual([p.id for p in second[-1]], ["FAKE7"])  # away, but known
            one.stop()
            self.assertIs(Subscription.hub, hub)  # the other still looks
        finally:
            two.stop()
        self.assertIsNone(Subscription.hub)


class ChoosingTest(unittest.TestCase):
    def test_service_or_own(self):
        saved = os.environ.get("HANDSET_PHONES")
        try:
            os.environ["HANDSET_PHONES"] = "own"
            self.assertFalse(service.has_service())
            self.assertIsInstance(service.watch(lambda phones: None).inner, Subscription)
            self.assertIn("Handset", service.set_up_where())
            os.environ["HANDSET_PHONES"] = "service"
            self.assertIsInstance(service.watch(lambda phones: None).inner, service.Watch)
        finally:
            if saved is None:
                os.environ.pop("HANDSET_PHONES", None)
            else:
                os.environ["HANDSET_PHONES"] = saved


if __name__ == "__main__":
    unittest.main()
