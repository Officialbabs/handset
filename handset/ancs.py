"""An iPhone's notifications over Bluetooth: Apple's Notification Center Service (ANCS), as a watch
gets them. The computer advertises itself (Bluetooth Low Energy) asking for the service; the
iPhone, paired with it once from its Bluetooth settings, connects and shares its notifications,
and Handset reads each one's app, title and text and can answer its actions (a call's Accept and
Decline). Through BlueZ, over D-Bus; pairing is the desktop's own (its agent asks to confirm).

The computer only advertises while an iPhone is being paired (`handset --pair-iphone`, for a few
minutes) or once one has been, so that it comes back.
"""

from __future__ import annotations

import struct
import unicodedata
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

from .ams import AMS, ENTITY_UPDATE, REMOTE_COMMAND, Ams, Writes
from .log import debug
from .media import NowPlaying
from .notifications import Note

ANCS = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
NOTIFICATION_SOURCE = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"

ADDED, MODIFIED, REMOVED = 0, 1, 2
SILENT, IMPORTANT, PRE_EXISTING, POSITIVE, NEGATIVE = 1, 2, 4, 8, 16
GET_NOTIFICATION, GET_APP, PERFORM_ACTION = 0, 1, 2
APP_ID, TITLE, SUBTITLE, MESSAGE, POSITIVE_LABEL, NEGATIVE_LABEL = 0, 1, 2, 3, 6, 7
INCOMING_CALL = 1
MAX_LENGTH = 512
PAIRING_S = 5 * 60

ADVERTISEMENT_XML = """<node><interface name="org.bluez.LEAdvertisement1">
  <method name="Release"/>
  <property name="Type" type="s" access="read"/>
  <property name="SolicitUUIDs" type="as" access="read"/>
  <property name="Discoverable" type="b" access="read"/>
  <property name="Includes" type="as" access="read"/>
</interface></node>"""
ADVERTISEMENT_PATH = "/io/github/Officialbabs/Handset/ancs"


# ---- The service's packets ---------------------------------------------------------------------

def parse_source(data: bytes) -> tuple[int, int, int, int] | None:
    """(event, flags, category, uid) from a Notification Source notification."""
    if len(data) < 8:
        return None
    event, flags, category, _count, uid = struct.unpack("<BBBBI", data[:8])
    return event, flags, category, uid


def ask_notification(uid: int, flags: int) -> bytes:
    """Get Notification Attributes: its app, title, subtitle and message, and its actions' names."""
    out = struct.pack("<BI", GET_NOTIFICATION, uid) + bytes([APP_ID])
    for attribute in (TITLE, SUBTITLE, MESSAGE):
        out += struct.pack("<BH", attribute, MAX_LENGTH)
    if flags & POSITIVE:
        out += bytes([POSITIVE_LABEL])
    if flags & NEGATIVE:
        out += bytes([NEGATIVE_LABEL])
    return out


def ask_app(app: str) -> bytes:
    """Get App Attributes: the app's display name."""
    return bytes([GET_APP]) + app.encode() + b"\0" + bytes([0])


def perform(uid: int, positive: bool) -> bytes:
    return struct.pack("<BIB", PERFORM_ACTION, uid, 0 if positive else 1)


def parse_reply(data: bytes, wanted: int) -> tuple[int, str | int, dict[int, str]] | None:
    """A whole Data Source reply (command, uid or app id, {attribute: text}), or None while it's
    still coming (it can come in several pieces): `wanted` attributes make it whole."""
    if not data:
        return None
    command = data[0]
    if command == GET_NOTIFICATION:
        if len(data) < 5:
            return None
        subject, at = struct.unpack("<I", data[1:5])[0], 5
    elif command == GET_APP:
        end = data.find(b"\0", 1)
        if end < 0:
            return None
        subject, at = data[1:end].decode(errors="replace"), end + 1
    else:
        return None
    attributes: dict[int, str] = {}
    while at + 3 <= len(data):
        attribute, length = struct.unpack("<BH", data[at:at + 3])
        if at + 3 + length > len(data):
            return None
        attributes[attribute] = data[at + 3:at + 3 + length].decode(errors="replace")
        at += 3 + length
    return (command, subject, attributes) if len(attributes) >= wanted else None


def plain_name(name: str) -> str:
    """An app's name as the iPhone gives it, without the invisible marks some carry (WhatsApp's
    starts with a left-to-right mark)."""
    return "".join(c for c in name if unicodedata.category(c) != "Cf").strip()


# ---- BlueZ -------------------------------------------------------------------------------------

class Ancs:
    """The iPhone's notifications, from whichever paired iPhone connects: `on_note(note)` and
    `on_gone(phone_id, key)` on GLib's main loop. `phone_id` is the id the notes carry (the
    iPhone's in Ada's service, when there's one, so its apps' entries and windows go with them).
    With `on_media`, what it plays too (ams), over the same link."""

    def __init__(self, on_note: Callable[[Note], None], on_gone: Callable[[str, str], None],
                 known: Callable[[], set[str]], remember: Callable[[str], None],
                 on_media: Callable[[NowPlaying | None], None] | None = None,
                 app_of: Callable[[str], str] = lambda _name: ""):
        self.on_note = on_note
        self.on_gone = on_gone
        self.known = known  # the Bluetooth addresses of iPhones paired for this before
        self.remember = remember
        self.phone_id = "iphone"
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.writes = Writes(self.bus)
        self.media = Ams(self.bus, self.writes, on_media, app_of) if on_media else None
        self.adapter: str | None = None
        self.advertising = False
        self.pairing_until = 0.0
        self.device: str | None = None  # the connected iPhone's object path
        self.address = ""
        self.chars: dict[str, str] = {}  # uuid → characteristic path
        self.queue: list[tuple[bytes, int, Callable[[tuple], None]]] = []  # (request, attributes, then)
        self.waiting: tuple[bytes, int, Callable[[tuple], None]] | None = None
        self.buffer = b""
        self.app_names: dict[str, str] = {}
        self.registration = 0

    # -- start, pairing and advertising

    def start(self):
        self.adapter = self._adapter()
        if not self.adapter:
            debug("no Bluetooth adapter: no iPhone notifications")
            return
        self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged", None, None,
                                  Gio.DBusSignalFlags.NONE, self._properties)
        for signal in ("InterfacesAdded", "InterfacesRemoved"):
            self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.ObjectManager", signal, "/", None,
                                      Gio.DBusSignalFlags.NONE, lambda *_a: GLib.idle_add(self._look))
        GLib.timeout_add_seconds(10, self._tick)
        self._look()

    def pair(self):
        """Visible to iPhones for a few minutes, to be paired from one's Bluetooth settings."""
        self.pairing_until = time.monotonic() + PAIRING_S
        self._tick()

    def _tick(self):
        wanted = self.device is None and (time.monotonic() < self.pairing_until or bool(self.known()))
        if wanted != self.advertising:
            self._advertise(wanted)
        return GLib.SOURCE_CONTINUE

    def _adapter(self) -> str | None:
        try:
            objects = self._objects()
        except GLib.Error:
            return None
        return next((path for path, interfaces in objects.items() if "org.bluez.LEAdvertisingManager1" in interfaces), None)

    def _objects(self) -> dict:
        reply = self.bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None,
                                   GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 5000, None)
        return reply.unpack()[0]

    def _advertise(self, on: bool):
        """Advertising on or off. BlueZ reads the advertisement's properties from us while it
        registers it, so the call can't wait on the main loop: it's answered when it's done."""
        manager = "org.bluez.LEAdvertisingManager1"
        self.advertising = on  # as asked; set back if BlueZ says no
        if on:
            if not self.registration:
                info = Gio.DBusNodeInfo.new_for_xml(ADVERTISEMENT_XML).interfaces[0]
                self.registration = self.bus.register_object(ADVERTISEMENT_PATH, info, self._advert_call,
                                                             self._advert_property, None)
            call, args = "RegisterAdvertisement", GLib.Variant("(oa{sv})", (ADVERTISEMENT_PATH, {}))
        else:
            call, args = "UnregisterAdvertisement", GLib.Variant("(o)", (ADVERTISEMENT_PATH,))
        self.bus.call("org.bluez", self.adapter, manager, call, args, None, Gio.DBusCallFlags.NONE, 10000, None,
                      self._advertised, on)

    def _advertised(self, bus, result, on: bool):
        try:
            bus.call_finish(result)
            debug("advertising for an iPhone's notifications" if on else "not advertising")
        except GLib.Error as error:
            debug("Bluetooth advertising:", error.message)
            if on:
                self.advertising = False

    @staticmethod
    def _advert_call(_bus, _sender, _path, _interface, method, _params, invocation):
        invocation.return_value(None)  # Release: BlueZ is done with it

    @staticmethod
    def _advert_property(_bus, _sender, _path, _interface, name):
        return {"Type": GLib.Variant("s", "peripheral"),
                "SolicitUUIDs": GLib.Variant("as", [ANCS]),
                "Discoverable": GLib.Variant("b", True),
                "Includes": GLib.Variant("as", ["local-name"])}.get(name)

    def stop(self):
        """Advertising off (BlueZ drops it anyway when this process goes)."""
        if self.advertising:
            try:
                self.bus.call_sync("org.bluez", self.adapter, "org.bluez.LEAdvertisingManager1",
                                   "UnregisterAdvertisement", GLib.Variant("(o)", (ADVERTISEMENT_PATH,)), None,
                                   Gio.DBusCallFlags.NONE, 3000, None)
            except GLib.Error:
                pass
            self.advertising = False
        if self.registration:
            self.bus.unregister_object(self.registration)
            self.registration = 0

    # -- the connected iPhone and its service

    def _properties(self, _bus, _sender, path, _interface, _signal, parameters):
        interface, changed, _gone = parameters.unpack()
        if interface == "org.bluez.Device1" and {"ServicesResolved", "Connected", "Paired"} & set(changed):
            GLib.idle_add(self._look)
        elif interface == "org.bluez.GattCharacteristic1" and "Value" in changed:
            value = bytes(changed["Value"])
            if path == self.chars.get(NOTIFICATION_SOURCE):
                self._source(value)
            elif path == self.chars.get(DATA_SOURCE):
                self._data(value)
            elif self.media and self.media.owns(path):
                self.media.changed(path, value)

    def _look(self):
        """A connected device that offers the service: subscribed to, once."""
        try:
            objects = self._objects()
        except GLib.Error:
            return GLib.SOURCE_REMOVE
        if self.device and not objects.get(self.device, {}).get("org.bluez.Device1", {}).get("Connected"):
            debug("the iPhone went")
            self.device, self.chars = None, {}
            self.queue, self.waiting, self.buffer = [], None, b""
            self.writes.clear()
            self._tick()
        if self.device:
            self._look_media(objects)
            return GLib.SOURCE_REMOVE
        if self.media:
            self.media.detach()
        for path, interfaces in objects.items():
            service = interfaces.get("org.bluez.GattService1")
            if not service or service.get("UUID", "").lower() != ANCS:
                continue
            device = service.get("Device")
            props = objects.get(device, {}).get("org.bluez.Device1", {})
            if not props.get("Connected"):
                continue
            chars = self._chars(objects, path)
            if not {NOTIFICATION_SOURCE, CONTROL_POINT, DATA_SOURCE} <= set(chars):
                continue
            self.device, self.chars = device, chars
            self.address = props.get("Address", "")
            debug("iPhone notifications from", props.get("Alias") or self.address)
            if self.advertising:
                self._advertise(False)
            # The data source first: the notification source then sends what's on the phone.
            for uuid in (DATA_SOURCE, NOTIFICATION_SOURCE):
                self.bus.call(
                    "org.bluez", chars[uuid], "org.bluez.GattCharacteristic1", "StartNotify", None, None,
                    Gio.DBusCallFlags.NONE, 10000, None, self._started, uuid)
            self._look_media(objects)
            break
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _chars(objects: dict, service: str) -> dict[str, str]:
        """A GATT service's characteristics: uuid → path."""
        return {interfaces["org.bluez.GattCharacteristic1"]["UUID"].lower(): path
                for path, interfaces in objects.items()
                if path.startswith(service + "/") and "org.bluez.GattCharacteristic1" in interfaces}

    def _look_media(self, objects: dict):
        """The iPhone's media service, while it offers it (it can stop, and offer it again)."""
        if not self.media:
            return
        for path, interfaces in objects.items():
            service = interfaces.get("org.bluez.GattService1")
            if service and service.get("UUID", "").lower() == AMS and service.get("Device") == self.device:
                chars = self._chars(objects, path)
                if {REMOTE_COMMAND, ENTITY_UPDATE} <= set(chars):
                    self.media.attach(chars)
                    return
        self.media.detach()

    def _started(self, bus, result, uuid):
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            # Not paired yet (the iPhone asks to allow notifications), or it went: tried again
            # once it's paired (or comes back).
            debug("iPhone notifications didn't start:", uuid, error.message)
            if self.device:
                self.device, self.chars = None, {}
                GLib.timeout_add_seconds(5, lambda: (self._look(), False)[1])
            return
        if uuid == NOTIFICATION_SOURCE and self.address:
            self.remember(self.address)  # it comes back to this computer now: worth advertising for

    def _source(self, value: bytes):
        parsed = parse_source(value)
        if not parsed:
            return
        event, flags, category, uid = parsed
        key = str(uid)
        if event == REMOVED:
            self.on_gone(self.phone_id, key)
            return
        if flags & (PRE_EXISTING | SILENT):
            return  # there before it connected, or one the phone itself keeps quiet
        wanted = 4 + bool(flags & POSITIVE) + bool(flags & NEGATIVE)
        self._ask(ask_notification(uid, flags), wanted, lambda reply: self._got(reply, flags, category))

    def _got(self, reply: tuple, flags: int, category: int):
        _command, uid, attributes = reply
        app = attributes.get(APP_ID, "")
        title = attributes.get(TITLE, "")
        subtitle = attributes.get(SUBTITLE, "")
        message = attributes.get(MESSAGE, "")
        actions = []
        if flags & POSITIVE:
            actions.append(("ancs-positive", attributes.get(POSITIVE_LABEL) or "Accept"))
        if flags & NEGATIVE:
            actions.append(("ancs-negative", attributes.get(NEGATIVE_LABEL) or "Decline"))
        text = "\n".join(part for part in (subtitle, message) if part)
        note = Note(self.phone_id, str(uid), app, title, text,
                    urgent=bool(flags & IMPORTANT) or category == INCOMING_CALL, actions=tuple(actions))
        if app and app not in self.app_names:
            self._ask(ask_app(app), 1, lambda named: self._named(named, note))
        else:
            self.on_note(note)

    def _named(self, reply: tuple, note: Note):
        _command, app, attributes = reply
        self.app_names[app] = plain_name(attributes.get(0, "")) or app
        self.on_note(note)

    def app_name(self, app: str) -> str:
        return self.app_names.get(app, app)

    def act(self, key: str, positive: bool):
        """A notification's own action on the iPhone (Accept, Decline…)."""
        if self.device and key.isdigit():
            self._write(perform(int(key), positive))

    # -- one request at a time, its reply in pieces

    def _ask(self, request: bytes, wanted: int, then: Callable[[tuple], None]):
        self.queue.append((request, wanted, then))
        self._next()

    def _next(self):
        if self.waiting or not self.queue or not self.device:
            return
        self.waiting = self.queue.pop(0)
        self.buffer = b""
        self._write(self.waiting[0])
        GLib.timeout_add_seconds(5, self._gave_up, self.waiting)

    def _gave_up(self, waiting):
        if self.waiting is waiting:
            debug("no reply from the iPhone to a notification request")
            self.waiting = None
            self._next()
        return GLib.SOURCE_REMOVE

    def _data(self, value: bytes):
        if not self.waiting:
            return
        self.buffer += value
        reply = parse_reply(self.buffer, self.waiting[1])
        if reply:
            then = self.waiting[2]
            self.waiting = None
            then(reply)
            self._next()

    def _write(self, value: bytes):
        path = self.chars.get(CONTROL_POINT)
        if path:
            self.writes.write(path, value)  # after any request going (an action's, say)
