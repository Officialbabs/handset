"""Ada's phone service, as Handset hears it: the phones it knows and what Ada is doing on each.

The service (`ada-phones serve`) answers newline-delimited JSON on a Unix socket. Any program
may watch it: `{"op":"watch"}` answers `{"ok":true,"state":{…}}` and then pushes
`{"event":"state","state":{…}}` whenever something changes. Working a phone and showing its
screen are kept for Ada's own programs, so Handset only listens here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio, GLib

# A watch that drops (the service restarting, say) is tried again after this long.
RECONNECT_MS = 2000


def socket_path() -> str:
    """Where the service listens, as Ada's own programs look for it."""
    path = os.environ.get("ADA_PHONES_SOCKET")
    if path:
        return path
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "ada", "phones.sock")
    return os.path.join(f"/tmp/ada-{os.getuid()}", "ada", "phones.sock")


@dataclass(frozen=True)
class Phone:
    """One phone, from the service's state."""

    id: str
    name: str
    platform: str  # "android" or "ios"
    online: bool  # connected by cable or Wi-Fi now
    usb: bool
    wifi: bool
    ada_allowed: bool
    ada_doing: str | None  # what Ada is doing on it ("Tapping"…), None when she isn't working
    ada_paused: bool  # Ada is waiting while the person uses it
    problem: str | None
    address: str | None = None  # on Wi-Fi: where the service reaches it
    port: int | None = None
    model: str = ""  # "Pixel 7", "iPhone 12"

    @property
    def ada_working(self) -> bool:
        return self.ada_doing is not None

    @classmethod
    def from_state(cls, entry: dict) -> Phone:
        using = entry.get("using") if isinstance(entry.get("using"), dict) else None
        usb, wifi = bool(entry.get("usb")), bool(entry.get("wifi"))
        return cls(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or entry.get("model") or "Phone"),
            platform="ios" if entry.get("platform") == "ios" else "android",
            online=usb or wifi,
            usb=usb,
            wifi=wifi,
            ada_allowed=bool(entry.get("ada")),
            ada_doing=(str(using.get("doing") or "Working") if using is not None else None),
            ada_paused=bool(using and using.get("paused")),
            problem=entry.get("problem") or None,
            address=entry.get("address") or None,
            port=entry.get("port") if isinstance(entry.get("port"), int) else None,
            model=str(entry.get("model") or ""),
        )


def phones_in(state: dict) -> list[Phone]:
    """The phones in a state the service sent, skipping entries it couldn't name."""
    phones = state.get("phones") if isinstance(state, dict) else None
    if not isinstance(phones, list):
        return []
    return [Phone.from_state(p) for p in phones if isinstance(p, dict) and p.get("id")]


def state_of(line: str) -> dict | None:
    """The state in one line from the service, or None for anything else (frames, errors,
    a line cut short)."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or not isinstance(message.get("state"), dict):
        return None
    return message["state"]


def ada_line(phone: Phone) -> str:
    """What Ada is up to on the phone, in a few words for the window."""
    if not phone.ada_working:
        return ""
    if phone.ada_paused:
        return "Ada is waiting while you use it"
    doing = phone.ada_doing.rstrip(".…")
    return f"Ada: {doing[:1].lower() + doing[1:]}…" if doing else "Ada is working on it"


def status_line(phone: Phone) -> str:
    """How the phone is connected, in a few words. The service's problem only when it isn't:
    while plugged in it also says, for one, that the phone isn't on Wi-Fi."""
    if phone.usb:
        return "Connected by cable"
    if phone.wifi:
        return "Connected over Wi-Fi"
    if phone.problem:
        return str(phone.problem)
    return "Not connected"


class Watch:
    """A standing watch on the service: `on_state(phones)` runs on GLib's main loop with every
    state it sends, and `on_gone()` when the watch drops (it comes back on its own)."""

    def __init__(self, on_state: Callable[[list[Phone]], None], on_gone: Callable[[], None] | None = None,
                 path: str | None = None):
        self.on_state = on_state
        self.on_gone = on_gone
        self.path = path or socket_path()
        self.cancellable: Gio.Cancellable | None = None
        self.connection: Gio.SocketConnection | None = None
        self.retry = 0
        self.stopped = False

    def start(self):
        self.stopped = False
        self._connect()

    def stop(self):
        self.stopped = True
        if self.retry:
            GLib.source_remove(self.retry)
            self.retry = 0
        if self.cancellable:
            self.cancellable.cancel()
        if self.connection:
            self.connection.close(None)
            self.connection = None

    def _connect(self):
        self.retry = 0
        self.cancellable = Gio.Cancellable()
        address = Gio.UnixSocketAddress.new(self.path)
        Gio.SocketClient().connect_async(address, self.cancellable, self._connected)
        return GLib.SOURCE_REMOVE

    def _connected(self, client, result):
        try:
            self.connection = client.connect_finish(result)
            self.connection.get_output_stream().write_all(b'{"op":"watch"}\n', self.cancellable)
        except GLib.Error:
            self._dropped()
            return
        self.lines = Gio.DataInputStream.new(self.connection.get_input_stream())
        self.lines.read_line_async(GLib.PRIORITY_DEFAULT, self.cancellable, self._line)

    def _line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            self._dropped()
            return
        state = state_of(line)
        if state is not None:
            self.on_state(phones_in(state))
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self.cancellable, self._line)

    def _dropped(self):
        if self.connection:
            self.connection.close(None)
            self.connection = None
        if self.stopped:
            return
        if self.on_gone:
            self.on_gone()
        self.retry = GLib.timeout_add(RECONNECT_MS, self._connect)
