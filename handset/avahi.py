"""Services on the local network, as the system's Avahi daemon finds them (over D-Bus): an
Android phone pairing for Wireless debugging (_adb-tls-pairing._tcp) or ready to connect
(_adb-tls-connect._tcp). Without Avahi nothing is found, and the person types the address their
phone shows instead.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import debug

AVAHI = "org.freedesktop.Avahi"
BROWSER = "org.freedesktop.Avahi.ServiceBrowser"
IPV4 = 0  # Avahi's protocol for IPv4 (-1 is either)
ANY = -1


class Browser:
    """One kind of service, followed: `on_found(name, address, port)` for each resolved (on the
    main loop), `on_lost(name)` when one goes."""

    def __init__(self, kind: str, on_found: Callable[[str, str, int], None],
                 on_lost: Callable[[str], None] | None = None):
        self.kind = kind
        self.on_found = on_found
        self.on_lost = on_lost
        self.bus: Gio.DBusConnection | None = None
        self.path: str | None = None
        self.signals = 0

    def start(self) -> bool:
        """False when there's no Avahi to ask."""
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            # Its signals are taken before the browser exists: Avahi may send the first at once.
            self.signals = self.bus.signal_subscribe(AVAHI, BROWSER, None, None, None, Gio.DBusSignalFlags.NONE,
                                                     self._signal)
            reply = self.bus.call_sync(AVAHI, "/", "org.freedesktop.Avahi.Server", "ServiceBrowserNew",
                                       GLib.Variant("(iissu)", (ANY, IPV4, self.kind, "local", 0)),
                                       GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as error:
            debug("no Avahi to look for", self.kind, error.message)
            self.stop()
            return False
        self.path = reply.unpack()[0]
        return True

    def stop(self):
        if self.bus and self.signals:
            self.bus.signal_unsubscribe(self.signals)
            self.signals = 0
        if self.bus and self.path:
            self.bus.call(AVAHI, self.path, BROWSER, "Free", None, None, Gio.DBusCallFlags.NONE, 2000, None,
                          None, None)
            self.path = None

    def _signal(self, bus, _sender, path, _interface, name, params):
        if path != self.path:
            return
        if name == "ItemNew":
            interface, protocol, service, kind, domain, _flags = params.unpack()
            bus.call(AVAHI, "/", "org.freedesktop.Avahi.Server", "ResolveService",
                     GLib.Variant("(iisssiu)", (interface, protocol, service, kind, domain, IPV4, 0)), None,
                     Gio.DBusCallFlags.NONE, 10000, None, self._resolved, service)
        elif name == "ItemRemove" and self.on_lost:
            self.on_lost(params.unpack()[2])

    def _resolved(self, bus, result, service):
        try:
            reply = bus.call_finish(result).unpack()
        except GLib.Error as error:
            debug("couldn't resolve", service, error.message)
            return
        address, port = reply[7], reply[8]
        if self.path:
            self.on_found(service, address, port)
