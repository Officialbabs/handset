"""A phone's apps on the desk. Each app pinned from Handset gets a menu entry of its own (its
name, the phone's name, its icon) and a place in the desktop's dock; opening it runs
`handset --phone PHONE --app APP`, which shows that one app in a window of its own. Its window
carries the entry's id, so the dock counts it as that app, not as Handset.

The entries live in ~/.local/share/applications as io.github.Officialbabs.Handset.App.*.desktop,
their icons in ~/.local/share/handset/icons. The dock's pins are a list in the desktop's own
settings (~/.config/os-ai/dock.ini, [Dock] Pinned), which the dock follows as it changes.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
from dataclasses import dataclass

from gi.repository import GLib

from . import APP_ID

ENTRY = "Desktop Entry"


@dataclass(frozen=True)
class PhoneApp:
    """One app on one phone. `app` is its package name (Android) or bundle id (iPhone)."""

    phone_id: str
    phone_name: str
    platform: str
    app: str
    name: str

    @property
    def id(self) -> str:
        return app_id(self.phone_id, self.app)

    @property
    def title(self) -> str:
        return f"{self.name} ({self.phone_name})"


def app_id(phone_id: str, app: str) -> str:
    """The desktop's id for one phone's app: Handset's, then a token of the two (an app id's
    parts are letters, digits and _, and none starts with a digit)."""
    token = hashlib.sha1(f"{phone_id}/{app}".encode()).hexdigest()[:16]
    return f"{APP_ID}.App.p{token}"


def data_dir() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def applications_dir() -> str:
    return os.path.join(data_dir(), "applications")


def icons_dir() -> str:
    return os.path.join(data_dir(), "handset", "icons")


def entry_path(phone_id: str, app: str) -> str:
    return os.path.join(applications_dir(), f"{app_id(phone_id, app)}.desktop")


def icon_path(phone_id: str, app: str) -> str:
    return os.path.join(icons_dir(), f"{app_id(phone_id, app)}.png")


def dock_path() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "os-ai", "dock.ini")


def launcher() -> str:
    """How the menu entries start Handset: as it was started now, when that was its launcher."""
    started = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(started) == "handset" and os.access(started, os.X_OK):
        return started
    return shutil.which("handset") or "handset"


def quoted(arg: str) -> str:
    """One Exec argument, as desktop entries quote them."""
    if arg and all(c.isalnum() or c in "._-/+:@" for c in arg):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


# ---- The pins ---------------------------------------------------------------------------------

def pinned() -> list[PhoneApp]:
    """The phone apps pinned from Handset, from their menu entries."""
    found = []
    try:
        names = sorted(os.listdir(applications_dir()))
    except OSError:
        return []
    for name in names:
        if not (name.startswith(f"{APP_ID}.App.") and name.endswith(".desktop")):
            continue
        entry = GLib.KeyFile()
        try:
            entry.load_from_file(os.path.join(applications_dir(), name), GLib.KeyFileFlags.NONE)
            found.append(PhoneApp(entry.get_string(ENTRY, "X-Handset-Phone"),
                                  entry.get_string(ENTRY, "X-Handset-Phone-Name"),
                                  entry.get_string(ENTRY, "X-Handset-Platform"),
                                  entry.get_string(ENTRY, "X-Handset-App"),
                                  entry.get_string(ENTRY, "X-Handset-App-Name")))
        except GLib.Error:
            continue
    return found


def is_pinned(phone_id: str, app: str) -> bool:
    return os.path.exists(entry_path(phone_id, app))


def find(phone_id: str, app: str) -> PhoneApp | None:
    return next((p for p in pinned() if p.phone_id == phone_id and p.app == app), None)


def pin(app: PhoneApp, icon: bytes | None) -> bool:
    """The app's icon and menu entry, then its place at the end of the dock. False when the
    dock's pins couldn't be changed (the entry is there all the same: it can be pinned from the
    dock's own menu while its window is open)."""
    os.makedirs(icons_dir(), exist_ok=True)
    os.makedirs(applications_dir(), exist_ok=True)
    picture = icon_path(app.phone_id, app.app)
    GLib.file_set_contents(picture, icon or letter_icon(app.name))
    entry = GLib.KeyFile()
    entry.set_string(ENTRY, "Type", "Application")
    entry.set_string(ENTRY, "Name", app.title)
    entry.set_string(ENTRY, "Comment", f"{app.name} on {app.phone_name}, through Handset")
    entry.set_string(ENTRY, "Exec", " ".join(quoted(a) for a in
                                             (launcher(), "--phone", app.phone_id, "--app", app.app)))
    entry.set_string(ENTRY, "Icon", picture)
    entry.set_string(ENTRY, "Terminal", "false")
    entry.set_string(ENTRY, "Categories", "Utility;")
    entry.set_string(ENTRY, "X-Handset-Phone", app.phone_id)
    entry.set_string(ENTRY, "X-Handset-Phone-Name", app.phone_name)
    entry.set_string(ENTRY, "X-Handset-Platform", app.platform)
    entry.set_string(ENTRY, "X-Handset-App", app.app)
    entry.set_string(ENTRY, "X-Handset-App-Name", app.name)
    data, _length = entry.to_data()
    GLib.file_set_contents(entry_path(app.phone_id, app.app), data.encode())
    return dock(f"{app.id}.desktop", True)


def unpin(phone_id: str, app: str):
    """Out of the dock, and its entry and icon gone."""
    dock(f"{app_id(phone_id, app)}.desktop", False)
    for path in (entry_path(phone_id, app), icon_path(phone_id, app)):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def docked() -> list[str]:
    settings = GLib.KeyFile()
    try:
        settings.load_from_file(dock_path(), GLib.KeyFileFlags.KEEP_COMMENTS)
        return list(settings.get_string_list("Dock", "Pinned"))
    except GLib.Error:
        return []


def dock(entry_id: str, wanted: bool) -> bool:
    """Puts an entry in the dock's pins (at the end) or takes it out, keeping the rest of the
    settings file as it is. With no file yet the dock shows its own first pins, which a new
    file would lose: then nothing changes, and False says so."""
    settings = GLib.KeyFile()
    try:
        settings.load_from_file(dock_path(), GLib.KeyFileFlags.KEEP_COMMENTS)
    except GLib.Error:
        return False
    try:
        pins = list(settings.get_string_list("Dock", "Pinned"))
    except GLib.Error:
        pins = []
    if (entry_id in pins) == wanted:
        return True
    pins = pins + [entry_id] if wanted else [p for p in pins if p != entry_id]
    settings.set_string_list("Dock", "Pinned", pins)
    data, _length = settings.to_data()
    try:
        GLib.file_set_contents(dock_path(), data.encode())  # a new file renamed over: all or nothing
    except GLib.Error:
        return False
    return True


# ---- Icons --------------------------------------------------------------------------------

def letter_icon(name: str, size: int = 192) -> bytes:
    """A stand-in icon, for an app whose own couldn't be had: its first letter on a rounded
    square, in a colour of its own."""
    import cairo

    letter = (name.strip()[:1] or "?").upper()
    hue = int(hashlib.sha1(name.encode()).hexdigest()[:2], 16) / 255
    r, g, b = hsv(hue, 0.45, 0.78)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    radius, inset = size * 0.22, size * 0.06
    x0, y0, x1, y1 = inset, inset, size - inset, size - inset
    cr.new_sub_path()
    cr.arc(x1 - radius, y0 + radius, radius, -1.5708, 0)
    cr.arc(x1 - radius, y1 - radius, radius, 0, 1.5708)
    cr.arc(x0 + radius, y1 - radius, radius, 1.5708, 3.1416)
    cr.arc(x0 + radius, y0 + radius, radius, 3.1416, 4.7124)
    cr.close_path()
    cr.set_source_rgb(r, g, b)
    cr.fill()
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(size * 0.5)
    extents = cr.text_extents(letter)
    cr.move_to(size / 2 - extents.width / 2 - extents.x_bearing, size / 2 - extents.height / 2 - extents.y_bearing)
    cr.set_source_rgb(1, 1, 1)
    cr.show_text(letter)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


def hsv(h: float, s: float, v: float) -> tuple[float, float, float]:
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
