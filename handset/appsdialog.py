"""The phone's apps, in a list to search: open one in a window of its own, or pin it to the dock.

The list comes from the phone each time (Android through scrcpy's server, an iPhone through
Handset's helper); Android icons follow one by one, read out of each app's APK (apkicon.py), and
are kept in ~/.cache/handset/apps so the next look is quick.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import apps  # noqa: E402
from .apps import PhoneApp  # noqa: E402
from .log import debug  # noqa: E402
from .service import Phone  # noqa: E402

ICON_SIZE = 192  # pixels, for the dock as much as for this list
FALLBACK_ICON = "application-x-executable-symbolic"


def cache_dir(phone_id: str) -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "apps", hashlib.sha1(phone_id.encode()).hexdigest()[:16])


def cached_icon(folder: str, app: str) -> str:
    """Where an app's icon is kept: its package (or bundle id) is the name, when it's a plain one."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", app)
    return os.path.join(folder, f"{safe}.png")


class AppsDialog(Adw.Dialog):
    def __init__(self, window, phone: Phone):
        super().__init__(title=f"Apps on {phone.name}", content_width=400, content_height=600)
        self.window = window
        self.phone = phone
        self.folder = cache_dir(phone.id)
        self.serial: str | None = None
        self.rows: dict[str, tuple[Adw.ActionRow, Gtk.Image, Gtk.ToggleButton, str]] = {}
        self.names: dict[Adw.ActionRow, str] = {}  # each row's app, for the search
        self.closed = False
        self.connect("closed", lambda _d: setattr(self, "closed", True))

        self.search = Gtk.SearchEntry(placeholder_text="Search apps", hexpand=True)
        self.search.connect("search-changed", lambda _e: self.list.invalidate_filter())
        top = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        top.append(self.search)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.list.set_filter_func(self._matches)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(Adw.Clamp(child=self.list, margin_start=12, margin_end=12, margin_top=6,
                                     margin_bottom=12))
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        loading.append(Adw.Spinner(width_request=32, height_request=32))
        loading.append(Gtk.Label(label=f"Asking {phone.name} for its apps…"))
        self.problem = Adw.StatusPage(icon_name="phone-symbolic")
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(loading, "loading")
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.problem, "problem")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(Adw.HeaderBar())
        view.add_top_bar(top)
        self.set_child(view)
        threading.Thread(target=self._list, daemon=True).start()

    # ---- The list ---------------------------------------------------------------------------

    def _list(self):
        """The phone's apps (slow: off the main loop), then their icons one by one."""
        from . import android, iphone

        os.makedirs(self.folder, exist_ok=True)
        phone = self.phone
        if phone.platform == "ios":
            link = iphone.open_link(phone.id, phone.address, phone.model)
            listed = iphone.list_apps(link, self.folder) if isinstance(link, iphone.Link) else link
        else:
            self.serial = android.find_serial(phone.id, phone.address, phone.port)
            if not self.serial:
                listed = f"{phone.name} isn't connected: plug it in, or keep it on the same Wi-Fi."
            else:
                found = android.list_apps(self.serial)
                listed = found if isinstance(found, str) else [
                    (name, package, self._icon_if_kept(package)) for name, package in found]
        GLib.idle_add(self._listed, listed)
        if phone.platform != "ios" and self.serial and not isinstance(listed, str):
            for _name, package, icon in listed:
                if self.closed:
                    break
                if icon is None:
                    path = self._fetch_icon(package)
                    if path:
                        GLib.idle_add(self._icon_came, package, path)

    def _icon_if_kept(self, package: str) -> str | None:
        path = cached_icon(self.folder, package)
        return path if os.path.isfile(path) else None

    def _fetch_icon(self, package: str) -> str | None:
        """An Android app's icon, read from its APK and kept (None: it couldn't be read)."""
        from . import apkicon

        read = apkicon.read_app(self.serial, package, size=ICON_SIZE)
        if not read or not read[1]:
            debug("no icon for", package)
            return None
        path = cached_icon(self.folder, package)
        GLib.file_set_contents(path, read[1])
        return path

    def _listed(self, listed):
        if isinstance(listed, str):
            self.problem.set_title("No apps to show")
            self.problem.set_description(listed)
            self.stack.set_visible_child_name("problem")
            return GLib.SOURCE_REMOVE
        for name, app, icon in listed:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), activatable=True,
                                tooltip_text="Open it in a window of its own")
            image = Gtk.Image(pixel_size=32)
            self._set_icon(image, icon)
            row.add_prefix(image)
            pin = Gtk.ToggleButton(icon_name="view-pin-symbolic", valign=Gtk.Align.CENTER,
                                   active=apps.is_pinned(self.phone.id, app))
            pin.add_css_class("flat")
            self._pin_tip(pin)
            pin.connect("toggled", self._pin_toggled, name, app)
            row.add_suffix(pin)
            row.connect("activated", lambda _r, n=name, a=app: self._open(n, a))
            self.list.append(row)
            self.rows[app] = (row, image, pin, name)
            self.names[row] = name
        self.stack.set_visible_child_name("list")
        self.search.grab_focus()
        return GLib.SOURCE_REMOVE

    def _matches(self, row) -> bool:
        wanted = self.search.get_text().strip().casefold()
        return not wanted or wanted in self.names.get(row, "").casefold()

    @staticmethod
    def _set_icon(image: Gtk.Image, path: str | None):
        if path and os.path.isfile(path):
            image.set_from_file(path)
        else:
            image.set_from_icon_name(FALLBACK_ICON)

    def _icon_came(self, app: str, path: str):
        if app in self.rows:
            self._set_icon(self.rows[app][1], path)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _pin_tip(pin: Gtk.ToggleButton):
        pin.set_tooltip_text("Unpin from the dock" if pin.get_active() else "Pin to the dock")

    # ---- Opening and pinning -----------------------------------------------------------------

    def _phone_app(self, name: str, app: str) -> PhoneApp:
        return PhoneApp(self.phone.id, self.phone.name, self.phone.platform, app, name)

    def _open(self, name: str, app: str):
        self.window.get_application().open_app(self.phone.id, app, self._phone_app(name, app))
        self.close()

    def _pin_toggled(self, pin: Gtk.ToggleButton, name: str, app: str):
        self._pin_tip(pin)
        if not pin.get_active():
            apps.unpin(self.phone.id, app)
            self.toasts.add_toast(Adw.Toast(title=f"{name} is out of the dock"))
            return
        icon = cached_icon(self.folder, app)
        if os.path.isfile(icon) or self.phone.platform == "ios" or not self.serial:
            self._pin(name, app, icon)
            return

        def fetch():  # its icon first, if it hasn't come yet
            path = self._fetch_icon(app)
            GLib.idle_add(self._pin, name, app, path)

        threading.Thread(target=fetch, daemon=True).start()

    def _pin(self, name: str, app: str, icon: str | None):
        data = None
        if icon and os.path.isfile(icon):
            with open(icon, "rb") as file:
                data = file.read()
            self._icon_came(app, icon)
        docked = apps.pin(self._phone_app(name, app), data)
        self.toasts.add_toast(Adw.Toast(title=f"{name} is in the dock" if docked else
                                        f"{name} is in your apps: open it, then pin it from the dock"))
        return GLib.SOURCE_REMOVE
