"""Phones found by Handset itself, where the desktop has no phone service of its own: Android
phones through adb (`adb track-devices -l`: plugged in with USB debugging allowed, or on Wi-Fi
once paired), iPhones through usbmuxd (the system's iPhone service: plugged in, and whether
this computer is trusted). A phone seen once is remembered (~/.config/handset/phones.json), so
it's listed while it's away, as a phone service lists the phones it knows. `OwnWatch` gives the
same calls as service.Watch, with the same Phones.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from gi.repository import GLib

from .log import debug
from .service import Phone

USBMUXD = os.environ.get("USBMUXD_SOCKET_ADDRESS", "/var/run/usbmuxd")
LOCKDOWN_PORT = 62078
AGAIN_S = 3  # a watch that ends (adb's server restarting, usbmuxd not there yet) starts again
BATTERY_S = 60  # a connected Android phone's battery is read again this often
SEEN_EVERY_S = 300  # when a connected phone was last seen is written down this often
RECONNECT_S = 30  # an Android phone away on Wi-Fi is tried at its address this often
WIFI_PORT = 5555  # adb on Wi-Fi the old way (`adb tcpip`), as it's turned on by cable
# iPhones by the model code they give (ProductType), as people know them.
IPHONES = {
    "iPhone10,1": "iPhone 8", "iPhone10,4": "iPhone 8", "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,5": "iPhone 8 Plus", "iPhone10,3": "iPhone X", "iPhone10,6": "iPhone X",
    "iPhone11,2": "iPhone XS", "iPhone11,4": "iPhone XS Max", "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR", "iPhone12,1": "iPhone 11", "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max", "iPhone12,8": "iPhone SE",
    "iPhone13,1": "iPhone 12 mini", "iPhone13,2": "iPhone 12", "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max", "iPhone14,4": "iPhone 13 mini", "iPhone14,5": "iPhone 13",
    "iPhone14,2": "iPhone 13 Pro", "iPhone14,3": "iPhone 13 Pro Max", "iPhone14,6": "iPhone SE",
    "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus", "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max", "iPhone15,4": "iPhone 15", "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max", "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus", "iPhone17,1": "iPhone 16 Pro", "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,5": "iPhone 16e", "iPhone18,3": "iPhone 17", "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max", "iPhone18,4": "iPhone Air",
}
# What adb says of a phone it can't use yet, in words for the person.
WAITING = {
    "unauthorized": "Allow USB debugging on the phone (tap Allow)",
    "authorizing": "Allow USB debugging on the phone (tap Allow)",
    "no permissions": "This computer can't open the phone's USB: add the Android udev rules",
    "offline": "Unplug the phone and plug it in again",
}
UNTRUSTED = "Unlock it and trust this computer"


def iphone_model(product: str) -> str:
    return IPHONES.get(product, "iPhone")


def known_path() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "handset", "phones.json")


def load_known() -> dict[str, dict]:
    """The phones seen before: id → {"name", "platform", "model"}."""
    try:
        with open(known_path()) as file:
            known = json.load(file).get("phones", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: v for k, v in known.items() if isinstance(v, dict)} if isinstance(known, dict) else {}


def save_known(known: dict[str, dict]):
    path = known_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".new", "w") as file:
        json.dump({"phones": known}, file, indent=1)
    os.replace(path + ".new", path)


def forget(phone_id: str):
    known = load_known()
    if known.pop(phone_id, None) is not None:
        save_known(known)


# ---- adb -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str  # "device" once usable; "unauthorized", "offline", "no permissions"…
    usb: bool


def parse_track(payload: str) -> list[AdbDevice]:
    """One list `adb track-devices -l` sends: a line per connection, its serial, its state and
    what adb knows of it (usb:3-1 by cable)."""
    found = []
    for line in payload.splitlines():
        words = line.split()
        if len(words) < 2:
            continue
        serial, rest = words[0], words[1:]
        # "no permissions" is two words (and a link to udev's page after it).
        state = "no permissions" if rest[:2] == ["no", "permissions"] else rest[0]
        found.append(AdbDevice(serial, state, any(w.startswith("usb:") for w in rest)))
    return found


def read_frames(stream, on_payload: Callable[[str], None]):
    """adb's framed answers: four hex digits of length, then that much text."""
    while True:
        head = stream.read(4)
        if len(head) < 4:
            return
        try:
            length = int(head, 16)
        except ValueError:
            return
        payload = stream.read(length) if length else b""
        on_payload(payload.decode(errors="replace"))


def parse_battery(text: str) -> tuple[int | None, bool]:
    """`dumpsys battery`: its level, and whether it's charging (on any power)."""
    level = re.search(r"^\s*level:\s*(\d+)", text, re.M)
    powered = re.findall(r"^\s*(?:AC|USB|Wireless|Dock) powered:\s*true", text, re.M)
    return (int(level.group(1)) if level else None), bool(powered)


def battery(serial: str) -> tuple[int | None, bool]:
    from .android import shell

    try:
        return parse_battery(shell(serial, "dumpsys battery", timeout=10))
    except (OSError, subprocess.SubprocessError):
        return None, False


# ---- usbmuxd -------------------------------------------------------------------------------------

def mux_message(message: dict, tag: int = 1) -> bytes:
    body = plistlib.dumps(message)
    return struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body


def mux_read(sock: socket.socket) -> dict | None:
    head = recv_all(sock, 16)
    if head is None:
        return None
    length = struct.unpack("<IIII", head)[0]
    body = recv_all(sock, length - 16)
    return plistlib.loads(body) if body is not None else None


def recv_all(sock: socket.socket, count: int) -> bytes | None:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def mux_ask(message: dict, timeout: float = 5) -> tuple[dict | None, socket.socket | None]:
    """One request to usbmuxd, its answer, and the socket (a Connect's is then the phone's)."""
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(timeout)
    try:
        sock.connect(USBMUXD)
        sock.sendall(mux_message({"ClientVersionString": "handset", "ProgName": "handset", **message}))
        return mux_read(sock), sock
    except OSError:
        sock.close()
        return None, None


def trusted(udid: str) -> bool:
    """Whether this computer is paired with the iPhone (its "Trust This Computer" record)."""
    answer, sock = mux_ask({"MessageType": "ReadPairRecord", "PairRecordID": udid})
    if sock:
        sock.close()
    return bool(answer and answer.get("PairRecordData"))


def lockdown_values(device_id: int, keys: tuple[str, ...] = ("DeviceName", "ProductType")) -> dict:
    """Values the iPhone tells anyone plugged into it (its name, its model), no pairing needed."""
    answer, sock = mux_ask({"MessageType": "Connect", "DeviceID": device_id,
                            "PortNumber": socket.htons(LOCKDOWN_PORT)})
    if not sock:
        return {}
    values = {}
    try:
        if not answer or answer.get("Number") != 0:
            return {}
        for key in keys:
            body = plistlib.dumps({"Label": "handset", "Request": "GetValue", "Key": key})
            sock.sendall(struct.pack(">I", len(body)) + body)
            head = recv_all(sock, 4)
            reply = recv_all(sock, struct.unpack(">I", head)[0]) if head else None
            if reply:
                value = plistlib.loads(reply).get("Value")
                if isinstance(value, str):
                    values[key] = value
    except (OSError, ValueError, struct.error, plistlib.InvalidFileException):
        pass
    finally:
        sock.close()
    return values


# ---- The watch -----------------------------------------------------------------------------------

class OwnWatch:
    """Phones as adb and usbmuxd see them: `on_state(phones)` on GLib's main loop whenever they
    change (the remembered ones that are away among them), as service.Watch calls it.
    `waiting` holds the phones plugged in but not usable yet (USB debugging not allowed, this
    computer not trusted): what adb or usbmuxd calls each → why, for setting them up. The
    readers run in threads of their own and hand what they hear to the main loop."""

    def __init__(self, on_state: Callable[[list[Phone]], None], on_gone: Callable[[], None] | None = None,
                 adb: Callable[..., list[str]] | None = None):
        self.on_state = on_state
        self.on_gone = on_gone
        self.adb = adb
        self.connected: set[str] = set()  # adb serials usable now
        self.android: dict[str, Phone] = {}  # adb serial → the phone on it, once asked who it is
        self.attached: set[int] = set()  # usbmuxd's device ids
        self.iphones: dict[int, Phone] = {}  # usbmuxd's device id → the phone
        self.waiting: dict[str, str] = {}
        self.known = load_known()
        self.stopped = False
        self.adb_process: subprocess.Popen | None = None
        self.mux: socket.socket | None = None
        self.said: list[Phone] | None = None
        self.browser = None  # avahi.Browser: Android phones on Wi-Fi with Wireless debugging
        self.reconnect_timer = 0
        self.battery_timer = 0
        self.connecting: set[str] = set()

    def start(self):
        from .avahi import Browser
        from .pairing import CONNECTING

        self.stopped = False
        self._say()
        threading.Thread(target=self._track_android, daemon=True).start()
        threading.Thread(target=self._track_iphones, daemon=True).start()
        self.browser = Browser(CONNECTING, self._wireless_found)
        if not self.browser.start():
            self.browser = None
        self.reconnect_timer = GLib.timeout_add_seconds(RECONNECT_S, self._reconnect)
        self.battery_timer = GLib.timeout_add_seconds(BATTERY_S, self._batteries)

    def stop(self):
        self.stopped = True
        if self.browser:
            self.browser.stop()
            self.browser = None
        for timer in (self.reconnect_timer, self.battery_timer):
            if timer:
                GLib.source_remove(timer)
        self.reconnect_timer = self.battery_timer = 0
        process, self.adb_process = self.adb_process, None
        if process:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        sock, self.mux = self.mux, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def phones(self) -> list[Phone]:
        """Every phone: the ones here (by cable rather than Wi-Fi), then the remembered ones away."""
        here: dict[str, Phone] = {}
        for phone in list(self.iphones.values()) + list(self.android.values()):
            have = here.get(phone.id)
            if have is None or (phone.usb and not have.usb):
                here[phone.id] = phone
        away = [Phone(id=pid, name=entry.get("name") or "Phone", platform=entry.get("platform", "android"),
                      online=False, usb=False, wifi=False, ada_allowed=False, ada_doing=None, ada_paused=False,
                      problem=None, model=entry.get("model", ""), system=entry.get("system", ""),
                      last_seen=entry.get("last_seen") if isinstance(entry.get("last_seen"), (int, float)) else None)
                for pid, entry in self.known.items() if pid not in here]
        return sorted(here.values(), key=lambda p: (p.name.casefold(), p.id)) + away

    def forget(self, phone_id: str):
        """Not listed any more while it's away (back, it's remembered again)."""
        self.known.pop(phone_id, None)
        forget(phone_id)
        self._say()

    def _say(self):
        if self.stopped:
            return GLib.SOURCE_REMOVE
        phones = self.phones()
        remembered = False
        now = time.time()
        for phone in phones:
            if phone.online and not phone.problem:
                had = self.known.get(phone.id, {})
                seen = had.get("last_seen") if isinstance(had.get("last_seen"), (int, float)) else 0
                entry = {**had, "name": phone.name, "platform": phone.platform, "model": phone.model,
                         "system": phone.system, "last_seen": now if now - seen > SEEN_EVERY_S else seen}
                if self.known.get(phone.id) != entry:
                    self.known[phone.id] = entry
                    remembered = True
        if remembered:
            try:
                save_known(self.known)
            except OSError as error:
                debug("phones not remembered:", error)
        if phones != self.said:
            self.said = phones
            self.on_state(phones)
        return GLib.SOURCE_REMOVE

    # -- Android

    def _adb(self, *args: str) -> list[str]:
        if self.adb:
            return self.adb(None, *args)
        from .android import adb_command

        return adb_command(None, *args)

    def _track_android(self):
        while not self.stopped:
            try:
                self.adb_process = subprocess.Popen(self._adb("track-devices", "-l"), stdout=subprocess.PIPE,
                                                    stderr=subprocess.DEVNULL)
            except (OSError, TypeError) as error:  # no adb here: no Android phones
                debug("adb isn't there:", error)
                return
            read_frames(self.adb_process.stdout, lambda payload: GLib.idle_add(self._adb_listed, payload))
            if self.stopped:
                return
            GLib.idle_add(self._adb_listed, "")  # adb's server went: nothing's connected through it
            time.sleep(AGAIN_S)

    def _adb_listed(self, payload: str):
        devices = parse_track(payload)
        self.connected = {d.serial for d in devices if d.state == "device"}
        for serial in list(self.android):
            if serial not in self.connected:
                del self.android[serial]
        self.waiting = {k: v for k, v in self.waiting.items() if not k.startswith("adb:")}
        for device in devices:
            if device.state != "device":
                self.waiting[f"adb:{device.serial}"] = WAITING.get(device.state, device.state)
            elif device.serial not in self.android:
                threading.Thread(target=self._identify, args=(device,), daemon=True).start()
        self._say()
        return GLib.SOURCE_REMOVE

    def _identify(self, device: AdbDevice):
        """Who the phone on a connection is: its serial number (the id), its name and model."""
        from .android import shell

        try:
            said = shell(device.serial, "getprop ro.serialno; getprop ro.product.model; "
                                        "settings get global device_name; getprop ro.build.version.release", timeout=10)
            said = said.splitlines()
        except (OSError, subprocess.SubprocessError) as error:
            debug("couldn't ask", device.serial, "who it is:", error)
            return
        number, model, name, system = (said + ["", "", "", ""])[:4]
        number, model, name, system = number.strip(), model.strip(), name.strip(), system.strip()
        if not number:
            return
        name = name if name and name != "null" else model or "Android phone"
        address, port = None, None
        if not device.usb and ":" in device.serial and not device.serial.startswith("adb-"):
            host, _, number_port = device.serial.rpartition(":")
            address, port = host, int(number_port) if number_port.isdigit() else None
        level, charging = battery(device.serial)
        phone = Phone(id=number, name=name, platform="android", online=True, usb=device.usb, wifi=not device.usb,
                      ada_allowed=False, ada_doing=None, ada_paused=False, problem=None, address=address,
                      port=port, model=model, battery=level, charging=charging, system=system, last_seen=time.time())
        GLib.idle_add(self._identified, device.serial, phone)

    def _identified(self, serial: str, phone: Phone):
        if serial in self.connected:  # still there
            self.android[serial] = phone
            self._say()
        return GLib.SOURCE_REMOVE

    def _batteries(self):
        """The connected Android phones' batteries, read again (off the main loop)."""
        serials = list(self.android)

        def work():
            for serial in serials:
                level, charging = battery(serial)
                GLib.idle_add(self._battery_read, serial, level, charging)

        threading.Thread(target=work, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _battery_read(self, serial: str, level: int | None, charging: bool):
        phone = self.android.get(serial)
        if phone and (phone.battery, phone.charging) != (level, charging):
            self.android[serial] = replace(phone, battery=level, charging=charging)
            self._say()
        return GLib.SOURCE_REMOVE

    def remember_address(self, phone_id: str, address: str, port: int = WIFI_PORT):
        """Where an Android phone is on Wi-Fi (adb turned to it there by cable), to connect to it
        again when it's back."""
        if phone_id in self.known:
            self.known[phone_id] = {**self.known[phone_id], "address": address, "port": port}
            save_known(self.known)

    def _online(self, phone_id: str) -> bool:
        return any(p.id == phone_id for p in self.android.values())

    def _reconnect(self):
        """The Android phones remembered on Wi-Fi at an address, that aren't here: asked again."""
        for phone_id, entry in self.known.items():
            address = entry.get("address")
            if entry.get("platform") == "android" and address and not self._online(phone_id):
                self._connect(f"{address}:{entry.get('port') or WIFI_PORT}")
        return GLib.SOURCE_CONTINUE

    def _wireless_found(self, name: str, address: str, port: int):
        """An Android phone with Wireless debugging on (adb-SERIAL-xxxxxx): connected, if it's one
        Handset knows (paired once) and isn't here already."""
        if not name.startswith("adb-"):
            return
        phone_id = name[4:].rsplit("-", 1)[0]
        if self.known.get(phone_id, {}).get("platform") == "android" and not self._online(phone_id):
            self._connect(f"{address}:{port}")

    def _connect(self, target: str):
        if target in self.connecting:
            return
        self.connecting.add(target)

        def work():
            try:
                subprocess.run(self._adb("connect", target), capture_output=True, timeout=10)
            except (OSError, TypeError, subprocess.SubprocessError):
                pass
            GLib.idle_add(self.connecting.discard, target)

        threading.Thread(target=work, daemon=True).start()

    # -- iPhones

    def _track_iphones(self):
        while not self.stopped:
            answer, sock = mux_ask({"MessageType": "Listen"}, timeout=None)
            if not sock or not answer or answer.get("Number") != 0:
                if sock:
                    sock.close()
                time.sleep(AGAIN_S * 2)  # no usbmuxd (yet): no iPhones by cable
                continue
            self.mux = sock
            try:
                while not self.stopped:
                    message = mux_read(sock)
                    if message is None:
                        break
                    GLib.idle_add(self._mux_event, message)
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
            sock.close()
            if not self.stopped:
                GLib.idle_add(self._mux_gone)
                time.sleep(AGAIN_S)

    def _mux_gone(self):
        self.attached.clear()
        self.iphones.clear()
        self.waiting = {k: v for k, v in self.waiting.items() if not k.startswith("ios:")}
        self._say()
        return GLib.SOURCE_REMOVE

    def _mux_event(self, message: dict):
        kind = message.get("MessageType")
        device_id = message.get("DeviceID")
        if kind == "Attached":
            self.attached.add(device_id)
            threading.Thread(target=self._iphone_came, args=(device_id, message.get("Properties") or {}),
                             daemon=True).start()
        elif kind == "Detached":
            self.attached.discard(device_id)
            phone = self.iphones.pop(device_id, None)
            if phone:
                self.waiting.pop(f"ios:{phone.id}", None)
                self._say()
        elif kind == "Paired" and device_id in self.iphones:
            phone = self.iphones[device_id]
            self.iphones[device_id] = replace(phone, problem=None)
            self.waiting.pop(f"ios:{phone.id}", None)
            self._say()
        return GLib.SOURCE_REMOVE

    def _iphone_came(self, device_id: int, properties: dict):
        """Who the iPhone just plugged in is, and whether it trusts this computer (off the main loop)."""
        udid = str(properties.get("SerialNumber") or "")
        if not udid:
            return
        usb = properties.get("ConnectionType") == "USB"
        values = lockdown_values(device_id, ("DeviceName", "ProductType", "ProductVersion"))
        model = iphone_model(values.get("ProductType", ""))
        problem = None if trusted(udid) else UNTRUSTED
        phone = Phone(id=udid, name=values.get("DeviceName") or model, platform="ios", online=True, usb=usb,
                      wifi=not usb, ada_allowed=False, ada_doing=None, ada_paused=False, problem=problem,
                      model=model, system=values.get("ProductVersion", ""), last_seen=time.time())
        GLib.idle_add(self._iphone_identified, device_id, phone)

    def _iphone_identified(self, device_id: int, phone: Phone):
        if device_id in self.attached and not self.stopped:
            self.iphones[device_id] = phone
            if phone.problem:
                self.waiting[f"ios:{phone.id}"] = phone.problem
            self._say()
        return GLib.SOURCE_REMOVE


class Subscription:
    """One look at the phones Handset finds itself (a window's, the sync's): one OwnWatch for the
    whole process, however many look, started with the first and stopped with the last. The same
    calls as service.Watch."""

    hub: OwnWatch | None = None
    looking: list[Subscription] = []

    def __init__(self, on_state: Callable[[list[Phone]], None], on_gone: Callable[[], None] | None = None):
        self.on_state = on_state
        self.on_gone = on_gone

    def start(self):
        cls = Subscription
        if self not in cls.looking:
            cls.looking.append(self)
        if cls.hub is None:
            cls.hub = OwnWatch(cls._tell)
            cls.hub.start()
        else:
            GLib.idle_add(self._first)

    def _first(self):
        hub = Subscription.hub
        if self in Subscription.looking and hub is not None:
            self.on_state(hub.phones())
        return GLib.SOURCE_REMOVE

    def stop(self):
        cls = Subscription
        if self in cls.looking:
            cls.looking.remove(self)
        if not cls.looking and cls.hub is not None:
            cls.hub.stop()
            cls.hub = None

    @staticmethod
    def _tell(phones: list[Phone]):
        for looking in list(Subscription.looking):
            looking.on_state(phones)
