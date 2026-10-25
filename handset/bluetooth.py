"""A phone's sound and calls on this computer over Bluetooth: the computer is to the phone as a
pair of headphones and a car's handsfree are (A2DP and HFP, which PipeWire already offers here).
Switched on, Handset connects the phone's two profiles from this side, and the phone sends its
sound here, and its calls when there's one (heard on the speakers, answered through the
microphone: PipeWire links both by itself). Switched off, they're disconnected and the phone
plays its own again. The phone must have been paired with the computer once (in Settings ›
Bluetooth on the phone, as for its notifications).

The phone is trusted (BlueZ's Trusted), so it may come back by itself and BlueZ reconnects its
sound after the link drops; and while the sound is wanted here, the phone's profiles are asked
for again whenever it comes back in reach, and every little while until they come. Not when the
phone ended the link itself (the person disconnected it on the phone, or turned its Bluetooth
off: BlueZ says why a link ended): then it's left alone until the person asks again.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import debug

A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"  # the phone sends its sound
# While the sound is wanted and the phone isn't sending it, it's asked for again this often.
RETRY_S = 30
# Why BlueZ says a link ended, when the phone ended it.
ENDED_BY_PHONE = "org.bluez.Reason.Remote"
HFP_GATEWAY = "0000111f-0000-1000-8000-00805f9b34fb"  # the phone's calls
PROFILES = (A2DP_SOURCE, HFP_GATEWAY)


def phones(bus: Gio.DBusConnection) -> list[tuple[str, str, str]]:
    """The paired devices that can send sound or calls: (path, name, address)."""
    reply = bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None,
                          GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 5000, None)
    found = []
    for path, interfaces in reply.unpack()[0].items():
        device = interfaces.get("org.bluez.Device1")
        if not device or not device.get("Paired"):
            continue
        uuids = {u.lower() for u in device.get("UUIDs", [])}
        if uuids & set(PROFILES):
            found.append((path, device.get("Alias") or device.get("Name") or "", device.get("Address", "")))
    return found


def trust(bus: Gio.DBusConnection, device: str):
    """The device trusted: BlueZ lets it connect by itself, without asking, and reconnects its
    sound after the link drops."""
    bus.call("org.bluez", device, "org.freedesktop.DBus.Properties", "Set",
             GLib.Variant("(ssv)", ("org.bluez.Device1", "Trusted", GLib.Variant("b", True))), None,
             Gio.DBusCallFlags.NONE, 5000, None, _trusted, device)


def _trusted(bus, result, device: str):
    try:
        bus.call_finish(result)
    except GLib.Error as error:
        debug("Bluetooth: couldn't trust", device, error.message)


def choose(found: list[tuple[str, str, str]], name: str, addresses: set[str]) -> str | None:
    """The phone's device among the paired ones: by an address known to be it (paired for its
    notifications), by its name, or the one phone there is."""
    for path, _alias, address in found:
        if address in addresses:
            return path
    for path, alias, _address in found:
        if name and alias.casefold() == name.casefold():
            return path
    return found[0][0] if len(found) == 1 else None


class BluetoothSound:
    """One phone's sound and calls here over Bluetooth, while it's on. `on_ended(why)` when it
    can't come for now (not paired, out of reach): it keeps trying by itself (keeps_trying)."""

    keeps_trying = True

    def __init__(self, name: str, addresses: set[str], on_ended: Callable[[str], None] | None = None):
        self.name = name
        self.addresses = addresses
        self.on_ended = on_ended
        self.device: str | None = None
        self.bus: Gio.DBusConnection | None = None
        self.stopped = False
        self.retry = 0
        self.watch = 0
        self.ended_watch = 0
        self.let_go = False  # the phone ended the link: not asked for again until the person asks

    def start(self):
        self.stopped = False
        if self.let_go:
            return
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            self.device = choose(phones(self.bus), self.name, self.addresses)
        except GLib.Error as error:
            self._ended(f"Bluetooth isn't there ({error.message}).")
            self._later()
            return
        if not self.device:
            self._ended(f"Pair {self.name} with this computer first: Settings › Bluetooth on the phone.")
            self._later()
            return
        trust(self.bus, self.device)
        if not self.watch:
            self.watch = self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                                   self.device, "org.bluez.Device1", Gio.DBusSignalFlags.NONE,
                                                   self._changed)
        if not self.ended_watch:
            self.ended_watch = self.bus.signal_subscribe("org.bluez", "org.bluez.Device1", "Disconnected", self.device,
                                                         None, Gio.DBusSignalFlags.NONE, self._disconnected)
        self._connect()

    def stop(self):
        self.stopped = True
        if self.retry:
            GLib.source_remove(self.retry)
            self.retry = 0
        for watch in (self.watch, self.ended_watch):
            if watch and self.bus:
                self.bus.signal_unsubscribe(watch)
        self.watch = self.ended_watch = 0
        if self.device and self.bus:
            for uuid in PROFILES:
                self._call("DisconnectProfile", uuid)

    def _connect(self):
        for uuid in PROFILES:
            self._call("ConnectProfile", uuid)

    def _disconnected(self, _bus, _sender, _path, _interface, _signal, parameters):
        reason, _message = parameters.unpack()
        debug("Bluetooth: the phone's link ended:", reason)
        if reason == ENDED_BY_PHONE and not self.stopped:
            # Disconnected on the phone: its choice stands. Nothing more is asked of it until the
            # person switches the sound here again (or the phone sends it here by itself).
            self.let_go = True
            if self.retry:
                GLib.source_remove(self.retry)
                self.retry = 0
            self._ended(f"{self.name} disconnected: switch Play Sound Here off and on to have its sound here again.")

    def _changed(self, _bus, _sender, _path, _interface, _signal, parameters):
        _interface, changed, _gone = parameters.unpack()
        if changed.get("Connected") and not self.stopped and not self.let_go:
            # Back in reach (or it came by itself): its sound asked for again, once it's settled.
            GLib.timeout_add_seconds(2, lambda: (not self.stopped and self._connect(), False)[1])

    def _later(self):
        """Tried again in a while, while it's wanted."""
        if not self.retry and not self.stopped:
            self.retry = GLib.timeout_add_seconds(RETRY_S, self._again)

    def _again(self):
        self.retry = 0
        if not self.stopped and not self.let_go:
            self.start() if not self.device else self._connect()
        return GLib.SOURCE_REMOVE

    def _call(self, method: str, uuid: str):
        self.bus.call("org.bluez", self.device, "org.bluez.Device1", method, GLib.Variant("(s)", (uuid,)), None,
                      Gio.DBusCallFlags.NONE, 20000, None, self._done, (method, uuid))

    def _done(self, bus, result, asked):
        method, uuid = asked
        try:
            bus.call_finish(result)
            debug("Bluetooth", method, uuid[4:8], "done")
        except GLib.Error as error:
            debug("Bluetooth", method, uuid[4:8], "didn't:", error.message)
            if method == "ConnectProfile" and uuid == A2DP_SOURCE and not self.stopped and not self.let_go:
                self._ended(f"{self.name}'s sound comes here once it's near, with Bluetooth on.")
                self._later()

    def _ended(self, why: str):
        debug("Bluetooth sound:", why)
        if self.on_ended:
            self.on_ended(why)
