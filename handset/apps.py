"""A phone's apps on the desk. While a phone is connected each of its apps has a menu entry of its
own (its name, the phone's name, its icon), so it's in search and among the desk's apps like any
other (and so has each phone, opening its window); opening it runs `handset --phone PHONE --app APP`, which shows that one app in a window of
its own. Its window carries the entry's id, so the dock counts it as that app, not as Handset. An
app pinned to the dock keeps its entry when the phone goes; the others go with it (appsync.py
keeps them).

The entries live in ~/.local/share/applications as io.github.Officialbabs.Handset.App.*.desktop,
their icons in ~/.local/share/handset/icons. The dock's pins are a list in the desktop's own
settings (~/.config/os-ai/dock.ini, [Dock] Pinned), which the dock follows as it changes.
"""

from __future__ import annotations

import glob
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import time
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


def icon_files(phone_id: str, app: str) -> list[str]:
    """The app's icon as it's kept: named by what's in it too, so a new icon is a new file, which
    the desktop sees (it keeps a picture it has read by its path)."""
    return glob.glob(os.path.join(glob.escape(icons_dir()), f"{app_id(phone_id, app)}*.png"))


def cache_dir(phone_id: str) -> str:
    """Where a phone's app icons are kept, as they're read from it."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "apps", hashlib.sha1(phone_id.encode()).hexdigest()[:16])


def cached_icon(folder: str, app: str) -> str:
    """An app's kept icon: its package (or bundle id) is the name, made plain."""
    return os.path.join(folder, f"{re.sub(r'[^A-Za-z0-9._-]', '_', app)}.png")


def dock_path() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "os-ai", "dock.ini")


def launcher() -> str:
    """How the menu entries start Handset: as it was started now, when that was its launcher
    (or HANDSET_LAUNCHER says)."""
    if os.environ.get("HANDSET_LAUNCHER"):
        return os.environ["HANDSET_LAUNCHER"]
    started = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(started) == "handset" and os.access(started, os.X_OK):
        return started
    return shutil.which("handset") or "handset"


def open_window(phone_id: str, app: str | None = None):
    """The phone's app in its own window (or the phone's window), as its entry would open it."""
    command = [launcher(), "--phone", phone_id] + (["--app", app] if app else [])
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as error:
        print("handset: the window didn't open:", error, file=sys.stderr)


def quoted(arg: str) -> str:
    """One Exec argument, as desktop entries quote them."""
    if arg and all(c.isalnum() or c in "._-/+:@" for c in arg):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


# ---- The entries ------------------------------------------------------------------------------

def entries(phone_id: str | None = None) -> list[PhoneApp]:
    """The phone apps with menu entries (of one phone, or all), from the entries themselves."""
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
            app = PhoneApp(entry.get_string(ENTRY, "X-Handset-Phone"),
                           entry.get_string(ENTRY, "X-Handset-Phone-Name"),
                           entry.get_string(ENTRY, "X-Handset-Platform"),
                           entry.get_string(ENTRY, "X-Handset-App"),
                           entry.get_string(ENTRY, "X-Handset-App-Name"))
        except GLib.Error:
            continue
        if phone_id is None or app.phone_id == phone_id:
            found.append(app)
    return found


def has_entry(phone_id: str, app: str) -> bool:
    return os.path.exists(entry_path(phone_id, app))


def find(phone_id: str, app: str) -> PhoneApp | None:
    return next((p for p in entries(phone_id) if p.app == app), None)


def write_entry(app: PhoneApp, icon: bytes | None = None, replace: bool = True) -> bool:
    """The app's menu entry, with its icon (with none: the one it has, or a letter). Nothing is
    written when the entry is there already as it would be, or when it's there and `replace`
    is False. True when something changed."""
    if has_entry(app.phone_id, app.app) and not replace:
        return False
    os.makedirs(icons_dir(), exist_ok=True)
    os.makedirs(applications_dir(), exist_ok=True)
    named = named_icon(app.phone_id, app.app)
    if icon is None and named and os.path.isfile(named):
        picture = named
    else:
        data = icon or letter_icon(app.name)
        picture = os.path.join(icons_dir(), f"{app.id}-{hashlib.sha1(data).hexdigest()[:10]}.png")
        if not os.path.exists(picture):
            GLib.file_set_contents(picture, data)  # the picture before the entry that names it
    entry = GLib.KeyFile()
    entry.set_string(ENTRY, "Type", "Application")
    entry.set_string(ENTRY, "Name", app.title)
    entry.set_string(ENTRY, "Comment", f"{app.name} on {app.phone_name}, through Handset")
    entry.set_string(ENTRY, "Keywords", f"{app.name};{app.phone_name};phone;")
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
    # The desk reads these: entries with a device gather under that phone rather than among
    # the computer's own apps. X-Ada-Device-App stays out deliberately: with it the desk
    # folds the entry into its own phone-app row, which opens the desk's phone window —
    # and the person wants this entry's. Until the desk can prefer another app's entry,
    # an app it also lists shows twice, and that's the chosen trade-off (27 Oct).
    entry.set_string(ENTRY, "X-Ada-Device", app.phone_id)
    entry.set_string(ENTRY, "X-Ada-Device-Name", app.phone_name)
    data, _length = entry.to_data()
    path = entry_path(app.phone_id, app.app)
    try:
        with open(path) as file:
            if file.read() == data:
                return False
    except OSError:
        pass
    GLib.file_set_contents(path, data.encode())
    # An icon it had stays a while: the desktop shows it by its path until it reads the entry again.
    return True


# ---- The phones' own entries -------------------------------------------------------------------
# Each phone has an entry too ("Pixel 7"), opening its window: its window carries the entry's id,
# so the dock shows each phone under its own icon, to pin and open on its own.

def phone_app_id(phone_id: str) -> str:
    token = hashlib.sha1(phone_id.encode()).hexdigest()[:16]
    return f"{APP_ID}.Phone.p{token}"


def phone_entry_path(phone_id: str) -> str:
    return os.path.join(applications_dir(), f"{phone_app_id(phone_id)}.desktop")


def phone_entries() -> list[str]:
    """The phones with entries (their ids)."""
    found = []
    for path in glob.glob(os.path.join(glob.escape(applications_dir()), f"{APP_ID}.Phone.*.desktop")):
        entry = GLib.KeyFile()
        try:
            entry.load_from_file(path, GLib.KeyFileFlags.NONE)
            found.append(entry.get_string(ENTRY, "X-Handset-Phone"))
        except GLib.Error:
            continue
    return found


def write_phone_entry(phone_id: str, name: str, platform: str) -> bool:
    """The phone's own entry, opening its window: True when something changed."""
    os.makedirs(icons_dir(), exist_ok=True)
    os.makedirs(applications_dir(), exist_ok=True)
    data = phone_icon(name, platform)
    picture = os.path.join(icons_dir(), f"{phone_app_id(phone_id)}-{hashlib.sha1(data).hexdigest()[:10]}.png")
    if not os.path.exists(picture):
        GLib.file_set_contents(picture, data)
    entry = GLib.KeyFile()
    entry.set_string(ENTRY, "Type", "Application")
    entry.set_string(ENTRY, "Name", name)
    entry.set_string(ENTRY, "Comment", f"{name}'s screen in a window, through Handset")
    entry.set_string(ENTRY, "Keywords", f"{name};phone;mirror;handset;")
    entry.set_string(ENTRY, "Exec", " ".join(quoted(a) for a in (launcher(), "--phone", phone_id)))
    entry.set_string(ENTRY, "Icon", picture)
    entry.set_string(ENTRY, "Terminal", "false")
    entry.set_string(ENTRY, "Categories", "Utility;")
    entry.set_string(ENTRY, "X-Handset-Phone", phone_id)
    entry.set_string(ENTRY, "X-Handset-Platform", platform)
    text, _length = entry.to_data()
    path = phone_entry_path(phone_id)
    try:
        with open(path) as file:
            if file.read() == text:
                return False
    except OSError:
        pass
    GLib.file_set_contents(path, text.encode())
    return True


def remove_phone_entry(phone_id: str):
    for path in [phone_entry_path(phone_id)] + glob.glob(
            os.path.join(glob.escape(icons_dir()), f"{phone_app_id(phone_id)}*.png")):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def android_head(cr, cx: float, cy: float, edge: float, eyes: tuple[float, float, float]):
    """The Android robot's head, white, centred at (cx, cy), `edge` wide: its dome, its two
    antennae, its eyes in the colour behind it."""
    radius = edge / 2
    base = cy + radius * 0.35  # the dome's flat edge, below the middle
    cr.set_source_rgb(1, 1, 1)
    cr.arc(cx, base, radius, 3.1416, 0)
    cr.close_path()
    cr.fill()
    cr.set_line_width(edge * 0.06)
    cr.set_line_cap(1)  # round
    for side in (-1, 1):
        cr.move_to(cx + side * radius * 0.42, base - radius * 0.88)
        cr.line_to(cx + side * radius * 0.68, base - radius * 1.32)
        cr.stroke()
    cr.set_source_rgb(*eyes)
    for side in (-1, 1):
        cr.arc(cx + side * radius * 0.42, base - radius * 0.45, edge * 0.065, 0, 6.2832)
        cr.fill()


def phone_icon(name: str, platform: str, size: int = 192) -> bytes:
    """A phone's icon: a phone, upright, its screen lit in the platform's colour: an Android
    phone's with Android's robot on it, an iPhone's plain (a home bar at its foot)."""
    import cairo

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)

    def rounded(x0, y0, x1, y1, radius):
        cr.new_sub_path()
        cr.arc(x1 - radius, y0 + radius, radius, -1.5708, 0)
        cr.arc(x1 - radius, y1 - radius, radius, 0, 1.5708)
        cr.arc(x0 + radius, y1 - radius, radius, 1.5708, 3.1416)
        cr.arc(x0 + radius, y0 + radius, radius, 3.1416, 4.7124)
        cr.close_path()

    width = size * 0.56
    x0, y0, x1, y1 = (size - width) / 2, size * 0.06, (size + width) / 2, size * 0.94
    rounded(x0, y0, x1, y1, size * 0.11)
    cr.set_source_rgb(0.11, 0.11, 0.13)
    cr.fill()
    inset = size * 0.035
    top, bottom = (0.20, 0.62, 0.42), (0.10, 0.45, 0.62)  # Android: green into blue
    if platform == "ios":
        top, bottom = (0.36, 0.42, 0.95), (0.62, 0.30, 0.85)  # iPhone: blue into violet
    gradient = cairo.LinearGradient(0, y0, 0, y1)
    gradient.add_color_stop_rgb(0, *top)
    gradient.add_color_stop_rgb(1, *bottom)
    rounded(x0 + inset, y0 + inset, x1 - inset, y1 - inset, size * 0.08)
    cr.set_source(gradient)
    cr.fill()
    rounded(size / 2 - size * 0.07, y0 + inset * 1.6, size / 2 + size * 0.07, y0 + inset * 1.6 + size * 0.035,
            size * 0.0175)  # the camera's pill
    cr.set_source_rgb(0.11, 0.11, 0.13)
    cr.fill()
    if platform == "ios":
        bar = size * 0.15
        rounded(size / 2 - bar / 2, y1 - inset - size * 0.045, size / 2 + bar / 2, y1 - inset - size * 0.027,
                size * 0.009)  # the home bar
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.fill()
    else:
        middle = tuple((a + b) / 2 for a, b in zip(top, bottom))
        android_head(cr, size / 2, size / 2, size * 0.36, middle)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


def forget_old_icons(older_than: float = 24 * 3600):
    """Icons no entry names any more, kept a day for the desktop's sake, gone after that."""
    named = set()
    paths = [entry_path(app.phone_id, app.app) for app in entries()] + [phone_entry_path(p) for p in phone_entries()]
    for path in paths:
        entry = GLib.KeyFile()
        try:
            entry.load_from_file(path, GLib.KeyFileFlags.NONE)
            named.add(entry.get_string(ENTRY, "Icon"))
        except GLib.Error:
            continue
    now = time.time()
    for path in glob.glob(os.path.join(glob.escape(icons_dir()), "*.png")):
        try:
            if path not in named and now - os.path.getmtime(path) > older_than:
                os.unlink(path)
        except OSError:
            pass


def named_icon(phone_id: str, app: str) -> str | None:
    """The icon the app's entry names now."""
    entry = GLib.KeyFile()
    try:
        entry.load_from_file(entry_path(phone_id, app), GLib.KeyFileFlags.NONE)
        return entry.get_string(ENTRY, "Icon")
    except GLib.Error:
        return None


def remove_entry(phone_id: str, app: str):
    """The app's entry and icon gone (it stays in the dock's list if it's pinned)."""
    for path in [entry_path(phone_id, app)] + icon_files(phone_id, app):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# ---- The pins ---------------------------------------------------------------------------------

def pinned() -> list[PhoneApp]:
    """The phone apps pinned to the dock."""
    docked_ids = set(docked())
    return [app for app in entries() if f"{app.id}.desktop" in docked_ids]


def is_pinned(phone_id: str, app: str) -> bool:
    return f"{app_id(phone_id, app)}.desktop" in docked()


def pin(app: PhoneApp, icon: bytes | None) -> bool:
    """The app's entry (its icon too, when given), then its place at the end of the dock. False
    when the dock's pins couldn't be changed (the entry is there all the same: it can be pinned
    from the dock's own menu while its window is open)."""
    write_entry(app, icon)
    return dock(f"{app.id}.desktop", True)


def unpin(phone_id: str, app: str):
    """Out of the dock. Its entry stays while it's one of a connected phone's apps."""
    dock(f"{app_id(phone_id, app)}.desktop", False)


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
