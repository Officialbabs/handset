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
from dataclasses import dataclass, replace

from gi.repository import Gio, GLib

# A watch that drops (the service restarting, say) is tried again after this long.
RECONNECT_MS = 2000
SERVICE = "/usr/lib/ada/desktop/bin/ada-phones"


def socket_path() -> str:
    """Where the service listens, as Ada's own programs look for it."""
    path = os.environ.get("ADA_PHONES_SOCKET")
    if path:
        return path
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "ada", "phones.sock")
    return os.path.join(f"/tmp/ada-{os.getuid()}", "ada", "phones.sock")


# A phone switched off in Handset (Connected, in its window): listed, but not used at all.
DISCONNECTED = "Disconnected in Handset"
# A phone kept to its cable (Over Wi-Fi off): on Wi-Fi alone, it's as if it weren't there.
CABLE_ONLY = "Plug it in: Wi-Fi is off for it in Handset"


def choices() -> tuple[set[str], set[str]]:
    """The phones switched off in Handset, and the ones kept to their cables (its settings)."""
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config, "handset", "settings.json")) as file:
            settings = json.load(file)
    except (OSError, ValueError):
        return set(), set()
    if not isinstance(settings, dict):
        return set(), set()
    listed = lambda key: {str(p) for p in settings.get(key) or [] if isinstance(p, str)}  # noqa: E731
    return listed("disconnected"), listed("cable_only")


def as_handset_uses(phones: list[Phone]) -> list[Phone]:
    """The phones with Handset's own choices applied: one switched off is offline to everything
    in Handset (its window, notifications, clipboard, sound); one kept to its cable is offline
    while it's on Wi-Fi alone."""
    off, cable_only = choices()
    kept = []
    for phone in phones:
        if phone.id in off:
            phone = replace(phone, online=False, usb=False, wifi=False, problem=DISCONNECTED)
        elif phone.id in cable_only and phone.wifi:
            phone = replace(phone, online=phone.usb, wifi=False, problem=None if phone.usb else CABLE_ONLY)
        kept.append(phone)
    return kept


class Choosing:
    """A watch on the phones (the service's or Handset's finder) with Handset's own choices
    applied; refresh() applies them again (they changed) to what was heard last. Every one in the
    process is refreshed by refresh_all()."""

    all: list = []

    def __init__(self, inner, on_state: Callable[[list[Phone]], None]):
        self.inner = inner
        self.on_state = on_state
        self.heard: list[Phone] | None = None

    def start(self):
        if self not in Choosing.all:
            Choosing.all.append(self)
        self.inner.start()

    def stop(self):
        if self in Choosing.all:
            Choosing.all.remove(self)
        self.inner.stop()

    def heard_now(self, phones: list[Phone]):
        self.heard = phones
        self.on_state(as_handset_uses(phones))

    def refresh(self):
        if self.heard is not None:
            self.on_state(as_handset_uses(self.heard))


def refresh_all():
    """Handset's choices changed (a phone switched off or on, or to its cable): every watch in
    this process tells its own again."""
    for choosing in list(Choosing.all):
        choosing.refresh()


def has_service() -> bool:
    """Whether the desktop has a phone service to ask (Ada's); without one Handset finds the
    phones itself (finder.py). HANDSET_PHONES=own or =service decides instead (for trying)."""
    wanted = os.environ.get("HANDSET_PHONES")
    if wanted in ("own", "service"):
        return wanted == "service"
    return os.path.exists(socket_path()) or os.path.exists(SERVICE)


def watch(on_state: Callable[[list[Phone]], None], on_gone: Callable[[], None] | None = None):
    """A watch on the phones, from the service or Handset's own finder, as Handset uses them
    (as_handset_uses): start(), stop() and refresh()."""
    choosing = Choosing(None, on_state)
    if has_service():
        choosing.inner = Watch(choosing.heard_now, on_gone)
    else:
        from .finder import Subscription

        choosing.inner = Subscription(choosing.heard_now, on_gone)
    return choosing


# The desktop's own phone settings (Ada's Settings › Phones), where its phones are set up.
SETTINGS = ("/usr/lib/ada/desktop/ada-surface", "/usr/share/ada/qml/settings", "show", "phones")


def open_phone_settings() -> bool:
    """The desktop's phone settings opened, where it has them (False where it hasn't)."""
    if not has_service() or not os.access(SETTINGS[0], os.X_OK):
        return False
    import subprocess

    try:
        subprocess.Popen(SETTINGS, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


def set_up_where() -> str:
    """Where a phone is set up, in words to follow "set it up"."""
    return "with Ada (Settings › Phones)" if has_service() else "in Handset (its Phones window)"


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
    battery: int | None = None  # percent, when it's known
    charging: bool = False
    system: str = ""  # its version of Android or iOS ("17", "27.0.1")
    last_seen: float | None = None  # when it was last connected (seconds since the epoch)

    @property
    def system_name(self) -> str:
        """"Android 17", "iOS 27.0.1"; "" when not known."""
        if not self.system:
            return ""
        return f"{'iOS' if self.platform == 'ios' else 'Android'} {self.system}"

    @property
    def ada_working(self) -> bool:
        return self.ada_doing is not None

    @classmethod
    def from_state(cls, entry: dict) -> Phone:
        using = entry.get("using") if isinstance(entry.get("using"), dict) else None
        battery = entry.get("battery") if isinstance(entry.get("battery"), dict) else {}
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
            battery=level if isinstance(level := battery.get("level"), int) else None,
            charging=bool(battery.get("charging")),
            system=str(entry.get("ios" if entry.get("platform") == "ios" else "android") or ""),
            last_seen=entry["last_seen"] / 1000 if isinstance(entry.get("last_seen"), (int, float)) else None,
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
