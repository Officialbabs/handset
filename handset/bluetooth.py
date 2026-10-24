"""A phone's sound and calls on this computer over Bluetooth: the computer is to the phone as a
pair of headphones and a car's handsfree are (A2DP and HFP, which PipeWire already offers here).
Switched on, Handset connects the phone's two profiles from this side, and the phone sends its
sound here, and its calls when there's one (heard on the speakers, answered through the
microphone: PipeWire links both by itself). Switched off, they're disconnected and the phone
plays its own again. The phone must have been paired with the computer once (in Settings ›
Bluetooth on the phone, as for its notifications).
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import debug

A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"  # the phone sends its sound
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
    can't (not paired, out of reach)."""

    def __init__(self, name: str, addresses: set[str], on_ended: Callable[[str], None] | None = None):
        self.name = name
        self.addresses = addresses
        self.on_ended = on_ended
        self.device: str | None = None
        self.bus: Gio.DBusConnection | None = None

    def start(self):
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            self.device = choose(phones(self.bus), self.name, self.addresses)
        except GLib.Error as error:
            self._ended(f"Bluetooth isn't there ({error.message}).")
            return
        if not self.device:
            self._ended(f"Pair {self.name} with this computer first: Settings › Bluetooth on the phone.")
            return
        for uuid in PROFILES:
            self._call("ConnectProfile", uuid)

    def stop(self):
        if self.device and self.bus:
            for uuid in PROFILES:
                self._call("DisconnectProfile", uuid)

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
            if method == "ConnectProfile" and uuid == A2DP_SOURCE:
                self._ended(f"{self.name} didn't send its sound: keep it near, with Bluetooth on.")

    def _ended(self, why: str):
        debug("Bluetooth sound:", why)
        if self.on_ended:
            self.on_ended(why)
