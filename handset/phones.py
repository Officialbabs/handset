"""Handset's own window: the phones it knows (each with a page: show it, its apps, its sound,
forget it), setting a new one up, Handset's settings, and what this computer has for it.

Where the desktop has a phone service (Ada's), phones are set up there and listed here as it
knows them; elsewhere Handset finds them itself (finder.py) and sets them up here (pairing.py).
"""

from __future__ import annotations

import os
import shutil
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .log import debug  # noqa: E402
from .service import Phone, has_service, status_line, watch  # noqa: E402

# Settings with a switch here: (key, title, subtitle). Each is on unless it says false.
SWITCHES = (
    ("phone_notifications", "Notifications",
     "The phones' notifications on this computer; clicking one opens its app"),
    ("auto_pause", "One Thing Playing at a Time",
     "A phone or this computer starting to play pauses the others"),
    ("pop_out_apps", "Opened Apps in Windows of Their Own",
     "An Android app another app opens comes out into its own window"),
    ("phone_files", "Phone Files in the File Manager",
     "Each connected phone as a folder in Phones, in your home folder"),
)


def autostart_path() -> str:
    from . import APP_ID

    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "autostart", f"{APP_ID}.Sync.desktop")


def starts_at_sign_in() -> bool:
    """Whether the sync starts at sign-in: its autostart entry is there and not hidden."""
    entry = GLib.KeyFile()
    try:
        entry.load_from_file(autostart_path(), GLib.KeyFileFlags.KEEP_COMMENTS)
    except GLib.Error:
        return False
    try:
        return not entry.get_boolean("Desktop Entry", "Hidden")
    except GLib.Error:
        return True


def start_at_sign_in(wanted: bool) -> bool:
    """Hides or shows the sync's autostart entry (install.sh makes it); False when there's none."""
    entry = GLib.KeyFile()
    try:
        entry.load_from_file(autostart_path(), GLib.KeyFileFlags.KEEP_COMMENTS)
    except GLib.Error:
        return False
    entry.set_boolean("Desktop Entry", "Hidden", not wanted)
    entry.save_to_file(autostart_path())
    return True


def checks() -> list[tuple[str, bool, str]]:
    """What this computer has for the phones: (what, there, words). Slow-ish: off the main loop."""
    from . import android, finder, iphone
    from .decoder import hardware

    found = []
    found.append(("Android phones", bool(android.adb_binary()),
                  "adb is here" if android.adb_binary() else "Install adb (android-tools) to reach Android phones"))
    version = android.scrcpy_version()
    found.append(("Android apps and sound", bool(version),
                  f"scrcpy {version} is here" if version else
                  "Install scrcpy for apps in their own windows, sound here, and the smoothest picture"))
    usbmuxd = os.path.exists(finder.USBMUXD)
    found.append(("iPhones by cable", usbmuxd, "usbmuxd is running" if usbmuxd else
                  "Install usbmuxd (and plug an iPhone in) to reach iPhones"))
    helper = iphone.helper_binary()
    found.append(("Handset's iPhone helper", bool(helper), "Built and in place" if helper else
                  "Run ./install.sh in Handset's folder to build it (it needs Rust)"))
    gpu = hardware("h264") and hardware("h265")
    found.append(("Video on the graphics card", gpu, "The phones' pictures are decoded by the graphics card"
                  if gpu else "Install your graphics card's VA-API driver for a lighter, smoother picture"))
    bluez = False
    try:
        system = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        bluez = system.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                 "NameHasOwner", GLib.Variant("(s)", ("org.bluez",)), GLib.VariantType("(b)"),
                                 Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
    except GLib.Error:
        pass
    found.append(("Bluetooth", bluez, "For an iPhone's notifications and sound" if bluez else
                  "Turn Bluetooth on (BlueZ) for an iPhone's notifications and sound"))
    return found


def phone_picture(phone: Phone, size: int) -> Gtk.Image:
    """The phone's own icon (as its entry has: Apple's logo, Android's robot)."""
    from gi.repository import Gdk

    from .apps import phone_icon

    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(phone_icon(phone.name, phone.platform, size * 2)))
    return Gtk.Image(paintable=texture, pixel_size=size)


def ago(when: float | None) -> str:
    """When it was, in a few words: "3 minutes ago", "yesterday at 18:02", "12 October at 09:15"."""
    if not when:
        return "Not yet"
    import time

    seconds = time.time() - when
    if seconds < 90:
        return "Just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 6 * 3600:
        hours = round(seconds / 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    moment = GLib.DateTime.new_from_unix_local(int(when))
    today = GLib.DateTime.new_now_local()
    if moment.get_day_of_year() == today.get_day_of_year() and moment.get_year() == today.get_year():
        return moment.format("Today at %H:%M")
    if today.add_days(-1).get_day_of_year() == moment.get_day_of_year():
        return moment.format("Yesterday at %H:%M")
    return moment.format("%-d %B at %H:%M")


def battery_words(phone: Phone) -> str:
    if phone.battery is None:
        return ""
    words = f"{phone.battery}%" + (", charging" if phone.charging and phone.online else "")
    return words if phone.online else f"{words} when last seen"


class BluetoothState:
    """Which phones are connected to this computer over Bluetooth (BlueZ), as it changes:
    `on_change()` then. A phone is its paired device by the address it shares its notifications
    by (an iPhone), or by its name."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.devices: list[tuple[str, str, bool]] = []  # paired: (address, name, connected)
        self.signals: list[int] = []
        self.timer = 0
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        except GLib.Error:
            self.bus = None
            return
        self.signals.append(self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.Properties",
                                                      "PropertiesChanged", None, "org.bluez.Device1",
                                                      Gio.DBusSignalFlags.NONE, lambda *_a: self._soon()))
        for signal in ("InterfacesAdded", "InterfacesRemoved"):
            self.signals.append(self.bus.signal_subscribe("org.bluez", "org.freedesktop.DBus.ObjectManager", signal,
                                                          "/", None, Gio.DBusSignalFlags.NONE, lambda *_a: self._soon()))
        self._read()

    def stop(self):
        for signal in self.signals:
            self.bus.signal_unsubscribe(signal)
        self.signals = []
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _soon(self):
        if not self.timer:
            self.timer = GLib.timeout_add(300, self._read)

    def _read(self):
        self.timer = 0
        self.bus.call("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None,
                      GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 5000, None, self._got, None)
        return GLib.SOURCE_REMOVE

    def _got(self, bus, result, _data):
        try:
            objects = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        devices = []
        for interfaces in objects.values():
            device = interfaces.get("org.bluez.Device1")
            if device and device.get("Paired"):
                devices.append((device.get("Address", ""), device.get("Alias") or device.get("Name") or "",
                                bool(device.get("Connected"))))
        if devices != self.devices:
            self.devices = devices
            self.on_change()

    def state(self, phone: Phone) -> str | None:
        """"connected", "paired" (not now), or None (not paired with this computer)."""
        from .appsync import known_iphones

        known = known_iphones() if phone.platform == "ios" else set()
        for address, name, connected in self.devices:
            if address in known or (name and name.casefold() == phone.name.casefold()):
                return "connected" if connected else "paired"
        return None


BLUETOOTH = {"connected": ("bluetooth-active-symbolic", "Connected over Bluetooth"),
             "paired": ("bluetooth-disconnected-symbolic", "Paired, not connected over Bluetooth now")}


def label(text: str, *classes: str, **props) -> Gtk.Label:
    widget = Gtk.Label(label=text, wrap=True, xalign=0, **props)
    for name in classes:
        widget.add_css_class(name)
    return widget


class PhonesWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Handset", default_width=520, default_height=700)
        self.set_size_request(360, 400)
        self.settings = app.settings
        self.own = not has_service()
        self.phones: list[Phone] = []
        self.rows: list[Gtk.Widget] = []
        self.waiting_rows: list[Gtk.Widget] = []
        self.pages: dict[str, PhonePage] = {}

        self.nav = Adw.NavigationView()
        self.toasts = Adw.ToastOverlay(child=self.nav)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a Phone")
        add.connect("clicked", lambda _b: self.add_phone())
        header.pack_start(add)
        page = Adw.PreferencesPage()
        self.phones_group = Adw.PreferencesGroup(title="Phones")
        page.add(self.phones_group)
        self.waiting_group = Adw.PreferencesGroup(title="Plugged In, Not Ready Yet", visible=False)
        page.add(self.waiting_group)
        page.add(self._settings_group())
        self.computer = Adw.PreferencesGroup(title="This Computer")
        page.add(self.computer)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        self.nav.add(Adw.NavigationPage(title="Handset", tag="phones", child=view))

        self.bluetooth = BluetoothState(self._bluetooth_changed)
        self._fill_phones()
        self.watch = watch(self._phones_changed)
        self.watch.start()
        self.connect("close-request", self._closing)
        threading.Thread(target=lambda: GLib.idle_add(self._checked, checks()), daemon=True).start()
        self.waiting_timer = GLib.timeout_add_seconds(1, self._fill_waiting) if self.own else 0

    def say(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text))

    def _closing(self, _window):
        self.watch.stop()
        self.bluetooth.stop()
        if self.waiting_timer:
            GLib.source_remove(self.waiting_timer)
            self.waiting_timer = 0
        return False

    # ---- The phones -------------------------------------------------------------------------

    def _phones_changed(self, phones: list[Phone]):
        self.phones = phones
        self._fill_phones()
        for phone_id, page in list(self.pages.items()):
            page.update(next((p for p in phones if p.id == phone_id), None))
        self._fill_waiting()

    def _bluetooth_changed(self):
        self._fill_phones()
        for page in self.pages.values():
            page.update(page.phone)

    def _fill_phones(self):
        for row in self.rows:
            self.phones_group.remove(row)
        self.rows = []
        if not self.phones:
            empty = Adw.ActionRow(title="No phones yet", use_markup=False,
                                  subtitle="Add one with the + button")
            empty.add_prefix(Gtk.Image(icon_name="phone-symbolic"))
            self.rows = [empty]
        else:
            cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=1,
                                max_children_per_line=2, column_spacing=12, row_spacing=12,
                                activate_on_single_click=True)
            for phone in self.phones:
                cards.append(self._card(phone))
            cards.connect("child-activated", lambda _box, child: self.show_phone_page(child.phone_id))
            self.rows = [cards]
        for row in self.rows:
            self.phones_group.add(row)

    def _card(self, phone: Phone) -> Gtk.FlowBoxChild:
        """One phone: its icon, name and how it's connected on top (a click opens its page), and
        along the bottom whether Handset uses it and the button that shows it."""
        app = self.get_application()
        connected = app.is_connected(phone.id)
        top = Gtk.Box(spacing=12, margin_start=14, margin_end=10, margin_top=14, margin_bottom=12)
        top.append(phone_picture(phone, 44))
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        words.append(label(phone.name, "heading", ellipsize=3))
        words.append(label(" · ".join(x for x in (phone.model if phone.model != phone.name else "",
                                                  phone.problem or status_line(phone),
                                                  battery_words(phone) if phone.online else "") if x),
                           "dim-label", "caption", ellipsize=3))
        top.append(words)
        state = self.bluetooth.state(phone)
        if state:
            icon, tip = BLUETOOTH[state]
            mark = Gtk.Image(icon_name=icon, tooltip_text=tip)
            if state != "connected":
                mark.add_css_class("dim-label")
            top.append(mark)
        top.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))

        bottom = Gtk.Box(spacing=8, margin_start=14, margin_end=10, margin_top=10, margin_bottom=10)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=connected)
        switch.connect("notify::active", lambda button, _p, p=phone.id:
                       app.is_connected(p) != button.get_active() and app.choose_phone(p, connected=button.get_active()))
        bottom.append(switch)
        state = label("Connected" if connected else "Disconnected", "caption", hexpand=True)
        state.set_mnemonic_widget(switch)
        bottom.append(state)
        show = Gtk.Button(valign=Gtk.Align.CENTER, sensitive=connected, tooltip_text=f"Show {phone.name}",
                          child=Adw.ButtonContent(icon_name="video-display-symbolic", label="Show"))
        show.add_css_class("flat")
        show.connect("clicked", lambda _b, p=phone.id: app.open_window(p))
        bottom.append(show)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card", "activatable"])
        body.append(top)
        body.append(Gtk.Separator())
        body.append(bottom)
        child = Gtk.FlowBoxChild(child=body, focusable=True)
        child.phone_id = phone.id
        return child

    def _fill_waiting(self):
        """Phones plugged in that can't be used yet, and why (Handset's own finder only)."""
        from .finder import Subscription

        hub = Subscription.hub
        waiting = dict(hub.waiting) if hub else {}
        for row in self.waiting_rows:
            self.waiting_group.remove(row)
        self.waiting_rows = []
        for key, why in sorted(waiting.items()):
            kind, _, ident = key.partition(":")
            phone = next((p for p in self.phones if p.id == ident), None)
            name = phone.name if phone else ("An iPhone" if kind == "ios" else "An Android phone")
            row = Adw.ActionRow(title=name, subtitle=why, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="phone-symbolic"))
            if kind == "ios" and phone:
                button = Gtk.Button(label="Set Up…", valign=Gtk.Align.CENTER)
                button.add_css_class("suggested-action")
                button.connect("clicked", lambda _b, p=phone: self.add_phone("ios", p))
                row.add_suffix(button)
            self.waiting_rows.append(row)
            self.waiting_group.add(row)
        self.waiting_group.set_visible(bool(self.waiting_rows))
        return GLib.SOURCE_CONTINUE

    def show_phone_page(self, phone_id: str):
        phone = next((p for p in self.phones if p.id == phone_id), None)
        if phone is None:
            return
        page = self.pages.get(phone_id)
        if page is None:
            page = PhonePage(self, phone)
            self.pages[phone_id] = page
            page.connect("hidden", lambda _p, i=phone_id: self.pages.pop(i, None))
        self.nav.push(page)

    def add_phone(self, kind: str | None = None, phone: Phone | None = None):
        """Adding a phone: where the desktop sets phones up itself, its settings (Ada's Settings ›
        Phones); elsewhere Handset's own steps."""
        from .service import open_phone_settings

        if not self.own:
            if not open_phone_settings():
                self.say("Set phones up in the desktop's settings, under Phones")
            return
        from .addphone import AddPhoneDialog

        AddPhoneDialog(self, kind, phone).present(self)

    # ---- Settings ---------------------------------------------------------------------------

    def _settings_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Settings")
        for key, title, subtitle in SWITCHES:
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=self.settings.get(key, True) is not False)
            row.connect("notify::active", self._switched, key)
            group.add(row)
        if os.path.exists(autostart_path()):
            row = Adw.SwitchRow(title="Start at Sign-In", active=starts_at_sign_in(),
                                subtitle="Phone apps among yours, notifications and media controls, "
                                         "without Handset's window open")
            row.connect("notify::active", self._sign_in_switched)
            group.add(row)
        return group

    def _switched(self, row, _param, key: str):
        from .window import save_settings

        self.settings[key] = row.get_active()
        save_settings(self.settings)

    def _sign_in_switched(self, row, _param):
        from .appsync import start_if_needed

        start_at_sign_in(row.get_active())
        if row.get_active():
            start_if_needed()

    # ---- This computer ----------------------------------------------------------------------

    def _checked(self, found: list[tuple[str, bool, str]]):
        for what, there, words in found:
            row = Adw.ActionRow(title=what, subtitle=words, use_markup=False)
            icon = Gtk.Image(icon_name="object-select-symbolic" if there else "dialog-warning-symbolic")
            icon.add_css_class("success" if there else "warning")
            row.add_suffix(icon)
            self.computer.add(row)
        return GLib.SOURCE_REMOVE


class PhonePage(Adw.NavigationPage):
    """One phone: how it's connected, showing it, its apps, its sound here, forgetting it."""

    def __init__(self, window: PhonesWindow, phone: Phone):
        super().__init__(title=phone.name, tag=f"phone-{phone.id}")
        self.window = window
        self.phone = phone
        page = Adw.PreferencesPage()

        top = Adw.PreferencesGroup()
        self.status = Adw.ActionRow(use_markup=False)
        self.status.add_prefix(phone_picture(phone, 40))
        top.add(self.status)
        app = window.get_application()
        self.connected = Adw.SwitchRow(title="Connected", active=app.is_connected(phone.id),
                                       subtitle="Handset uses this phone: its window, notifications, clipboard "
                                                "and sound. Off, it leaves the phone alone")
        self.connected.connect("notify::active", lambda row, _p: app.is_connected(self.phone.id) != row.get_active()
                               and app.choose_phone(self.phone.id, connected=row.get_active()))
        top.add(self.connected)
        self.wifi_on = Adw.SwitchRow(title="Over Wi-Fi", active=app.over_wifi(phone.id),
                                     subtitle="Off: only by its cable")
        self.wifi_on.connect("notify::active", lambda row, _p: app.over_wifi(self.phone.id) != row.get_active()
                             and app.choose_phone(self.phone.id, wifi=row.get_active()))
        top.add(self.wifi_on)
        page.add(top)

        about = Adw.PreferencesGroup(title="About")
        self.about = {}
        for key, title in (("battery", "Battery"), ("system", "System"), ("seen", "Last Connected"),
                           ("bluetooth", "Bluetooth")):
            row = Adw.ActionRow(title=title, use_markup=False)
            value = Gtk.Label(css_classes=["dim-label"], ellipsize=3)
            row.add_suffix(value)
            self.about[key] = value
            about.add(row)
        page.add(about)

        use = Adw.PreferencesGroup()
        show = Adw.ButtonRow(title="Show the Phone", start_icon_name="video-display-symbolic")
        show.connect("activated", lambda _r: window.get_application().open_window(self.phone.id))
        use.add(show)
        apps = Adw.ButtonRow(title="Apps…", start_icon_name="view-app-grid-symbolic")
        apps.connect("activated", lambda _r: self._apps())
        use.add(apps)
        self.files = Adw.ButtonRow(title="Open in Files", start_icon_name="folder-symbolic")
        self.files.connect("activated", lambda _r: self._open_files())
        use.add(self.files)
        page.add(use)

        here = Adw.PreferencesGroup(title="On This Computer")
        sound = window.get_application().sound
        self.sound = Adw.SwitchRow(title="Play Sound Here", active=bool(sound and sound.is_on(phone.id)),
                                   subtitle="The phone's sound on this computer while a window shows it"
                                   + (", its calls too" if phone.platform == "ios" else ""))
        self.sound.connect("notify::active", self._sound_switched)
        here.add(self.sound)
        if phone.platform == "ios":
            pair = Adw.ActionRow(title="Notifications over Bluetooth", use_markup=False,
                                 subtitle="Pair the iPhone with this computer once, from its Bluetooth settings")
            button = Gtk.Button(label="Pair…", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b: self._pair_bluetooth())
            pair.add_suffix(button)
            here.add(pair)
        page.add(here)

        if window.own and phone.platform == "android":
            wifi = Adw.PreferencesGroup()
            self.wifi = Adw.ButtonRow(title="Use Over Wi-Fi Too", start_icon_name="network-wireless-symbolic")
            self.wifi.connect("activated", lambda _r: self._wifi())
            wifi.add(self.wifi)
            page.add(wifi)
        else:
            self.wifi = None
        if window.own:
            setup = Adw.PreferencesGroup()
            if phone.platform == "ios":
                again = Adw.ButtonRow(title="Set Up Again…")
                again.connect("activated", lambda _r: window.add_phone("ios", self.phone))
                setup.add(again)
            forget = Adw.ButtonRow(title="Forget This Phone")
            forget.add_css_class("destructive-action")
            forget.connect("activated", lambda _r: self._forget())
            setup.add(forget)
            page.add(setup)

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.update(phone)

    def update(self, phone: Phone | None):
        if phone is None:
            self.status.set_title("Forgotten")
            self.status.set_subtitle("")
            return
        self.phone = phone
        if self.wifi is not None:
            self.wifi.set_visible(phone.usb)
        app = self.window.get_application()
        self.connected.set_active(app.is_connected(phone.id))  # switched in its window, say
        self.wifi_on.set_active(app.over_wifi(phone.id))
        self.about["battery"].set_label(battery_words(phone) or "Not known")
        self.about["system"].set_label(phone.system_name or "Not known yet")
        self.about["seen"].set_label("Now" if phone.online else ago(phone.last_seen))
        bluetooth = self.window.bluetooth.state(phone)
        self.about["bluetooth"].set_label({"connected": "Connected", "paired": "Not connected"}.get(
            bluetooth, "Not paired with this computer"))
        self.set_title(phone.name)
        self.status.set_title(phone.name)
        self.status.set_subtitle(" · ".join(x for x in (phone.model if phone.model != phone.name else "",
                                                        phone.problem or status_line(phone)) if x))

    def _wifi(self):
        """adb turned to Wi-Fi by its cable, and the address remembered (it lasts until the phone
        restarts; plugged in again, it's turned on again here)."""
        from . import android
        from .finder import Subscription
        from .pairing import wifi_by_cable

        self.wifi.set_sensitive(False)
        phone = self.phone

        def work():
            serial = android.find_serial(phone.id)
            done = wifi_by_cable(serial) if serial else f"{phone.name} isn't connected."
            GLib.idle_add(finished, done)

        def finished(done):
            self.wifi.set_sensitive(True)
            if isinstance(done, str):
                self.window.say(done)
                return
            if Subscription.hub:
                Subscription.hub.remember_address(phone.id, *done)
            self.window.say(f"{phone.name} works over Wi-Fi too, until it restarts")

        threading.Thread(target=work, daemon=True).start()

    def _open_files(self):
        """The phone's folder (~/Phones/NAME) in the file manager."""
        from .phonefs import mounted_folder

        folder = mounted_folder(self.phone.name)
        if not folder:
            self.window.say(f"{self.phone.name}'s folder isn't there: connect it (and Phone Files in Settings)")
            return
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(folder).get_uri(), None)

    def _apps(self):
        from .appsdialog import AppsDialog

        AppsDialog(self.window, self.phone).present(self.window)

    def _sound_switched(self, row, _param):
        app = self.window.get_application()
        if not app.sound:
            return
        app.sound.choose(self.phone.id, row.get_active(), app.window_for(self.phone.id))

    def _pair_bluetooth(self):
        from .appsync import ask_to_pair_iphone, start_if_needed

        start_if_needed()
        ask_to_pair_iphone()
        self.window.say("On the iPhone: Settings › Bluetooth, then pick this computer")

    def _forget(self):
        dialog = Adw.AlertDialog(heading=f"Forget {self.phone.name}?",
                                 body="It goes from Handset's phones, and its apps from your apps. Plugged in "
                                      "again, it comes back.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._forget_answered)
        dialog.present(self.window)

    def _forget_answered(self, _dialog, response: str):
        if response != "forget":
            return
        from . import apps
        from .finder import Subscription, forget

        phone_id = self.phone.id
        for app in apps.entries(phone_id):
            apps.remove_entry(phone_id, app.app)
        if Subscription.hub:
            Subscription.hub.forget(phone_id)
        else:
            forget(phone_id)
        debug("forgot", phone_id)
        self.window.nav.pop()
        self.window.say(f"{self.phone.name} is forgotten")
