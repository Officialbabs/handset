"""Adding a phone, where Handset sets phones up itself: an Android phone by its cable (USB
debugging allowed) or over Wi-Fi (Wireless debugging's pairing code), an iPhone by its cable, a
step at a time (pairing.py does the work). Each page watches the phones as they come, and says
what to do on the phone, and when it's done.
"""

from __future__ import annotations

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .service import Phone, watch  # noqa: E402

ANDROID_STEPS = (
    ("Turn On Developer Options", "On the phone: Settings › About phone, then tap Build number seven times"),
    ("Turn On USB Debugging", "Settings › System › Developer options › USB debugging"),
    ("Plug It In and Tap Allow", "Tick Always allow from this computer, so it isn't asked again"),
)
# The iPhone's steps: (key, title, what to do).
IPHONE_STEPS = (
    ("plug", "Plug It In", "By its cable, and unlock it"),
    ("trust", "Trust This Computer", "Tap Trust on the iPhone, then enter its passcode"),
    ("developer-mode", "Developer Mode", "Lets this computer show the iPhone's screen and work it"),
    ("ddi", "Developer Disk Image", "Apple's screen and touch services, signed by Apple for this iPhone"),
    ("wifi", "Wi-Fi", "Optional: so it works without the cable, on the same Wi-Fi as this computer"),
)


def step_icon(state: str) -> Gtk.Widget:
    if state == "running":
        return Adw.Spinner(width_request=16, height_request=16)
    names = {"done": ("object-select-symbolic", "success"), "problem": ("dialog-warning-symbolic", "warning"),
             "waiting": ("dialog-information-symbolic", "accent")}
    name, css = names.get(state, ("radio-symbolic", "dim-label"))
    icon = Gtk.Image(icon_name=name)
    icon.add_css_class(css)
    return icon


class AddPhoneDialog(Adw.Dialog):
    def __init__(self, window, kind: str | None = None, phone: Phone | None = None):
        super().__init__(title="Add a Phone", content_width=460, content_height=640)
        self.window = window
        self.phones: list[Phone] = []
        self.before: set[str] | None = None  # the phones online when it opened: a new one is the one added
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.android = AndroidPage(self)
        self.iphone = IPhonePage(self, phone)
        if kind is None:
            self.nav.add(self._choose())
        self.nav.add(self.android if kind != "ios" else self.iphone)
        self.watch = watch(self._phones_changed)
        self.watch.start()
        self.connect("closed", self._closed)

    def _closed(self, _dialog):
        self.watch.stop()
        self.android.stop()
        self.iphone.stop()

    def _choose(self) -> Adw.NavigationPage:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Which Phone?")
        for title, subtitle, target in (
                ("Android Phone", "By cable, or over Wi-Fi with Wireless debugging", self.android),
                ("iPhone", "By cable, iOS 17 or later; then over Wi-Fi too, if you like", self.iphone)):
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_prefix(Gtk.Image(icon_name="phone-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, t=target: self.nav.push(t))
            group.add(row)
        page.add(group)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        return Adw.NavigationPage(title="Add a Phone", tag="choose", child=view)

    def _phones_changed(self, phones: list[Phone]):
        self.phones = phones
        if self.before is None:
            self.before = {p.id for p in phones if p.online}
        self.android.phones_changed(phones)
        self.iphone.phones_changed(phones)

    def new_phone(self, platform: str) -> Phone | None:
        """A phone of that kind that came online while adding (or is waiting to be set up)."""
        return next((p for p in self.phones if p.platform == platform and p.online
                     and (p.id not in (self.before or set()) or p.problem)), None)

    def show(self, phone: Phone):
        self.window.get_application().open_window(phone.id)
        self.close()


def toolbar(content: Gtk.Widget) -> Adw.ToolbarView:
    view = Adw.ToolbarView(content=content)
    view.add_top_bar(Adw.HeaderBar())
    return view


class AndroidPage(Adw.NavigationPage):
    def __init__(self, dialog: AddPhoneDialog):
        super().__init__(title="Android Phone", tag="android")
        from .pairing import AndroidWifi

        self.dialog = dialog
        self.done: Phone | None = None
        page = Adw.PreferencesPage()

        cable = Adw.PreferencesGroup(title="By Cable")
        for number, (title, subtitle) in enumerate(ANDROID_STEPS, 1):
            row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
            row.add_prefix(Gtk.Label(label=str(number), width_chars=2, css_classes=["dim-label", "numeric"]))
            cable.add(row)
        self.status = Adw.ActionRow(title="Waiting for the phone…", use_markup=False)
        self.status_icon = step_icon("running")
        self.status.add_prefix(self.status_icon)
        self.show_button = Gtk.Button(label="Show", valign=Gtk.Align.CENTER, visible=False)
        self.show_button.add_css_class("suggested-action")
        self.show_button.connect("clicked", lambda _b: self.done and self.dialog.show(self.done))
        self.status.add_suffix(self.show_button)
        cable.add(self.status)
        page.add(cable)

        wifi = Adw.PreferencesGroup(
            title="Or Over Wi-Fi",
            description="Android 11 or later, on the same Wi-Fi as this computer. On the phone: Developer "
                        "options › Wireless debugging › Pair device with pairing code. Handset finds the "
                        "phone on the network; if it doesn't, type the IP address and port shown with the code.")
        self.code = Adw.EntryRow(title="Pairing Code", input_purpose=Gtk.InputPurpose.DIGITS, use_markup=False)
        self.code.connect("entry-activated", lambda _e: self._pair())
        wifi.add(self.code)
        self.address = Adw.EntryRow(title="IP Address and Port", use_markup=False)
        self.address.connect("entry-activated", lambda _e: self._pair())
        wifi.add(self.address)
        self.pair_button = Adw.ButtonRow(title="Pair")
        self.pair_button.add_css_class("suggested-action")
        self.pair_button.connect("activated", lambda _r: self._pair())
        wifi.add(self.pair_button)
        self.wifi_status = Adw.ActionRow(use_markup=False, visible=False)
        wifi.add(self.wifi_status)
        page.add(wifi)
        self.set_child(toolbar(page))

        self.wifi = AndroidWifi(self._wifi_step, self._wifi_done)
        self.connect("shown", lambda _p: self._look())

    def _look(self):
        if not self.wifi.browsers:
            self.wifi.look()

    def stop(self):
        self.wifi.stop()

    def phones_changed(self, phones: list[Phone]):
        from .finder import Subscription

        phone = self.dialog.new_phone("android")
        hub = Subscription.hub
        waiting = [why for key, why in (hub.waiting.items() if hub else []) if key.startswith("adb:")]
        if phone and not phone.problem:
            self.done = phone
            self.status.set_title(f"{phone.name} is ready")
            self._icon("done")
            self.show_button.set_visible(True)
        elif waiting:
            self.status.set_title(waiting[0])
            self._icon("waiting")

    def _icon(self, state: str):
        self.status.remove(self.status_icon)
        self.status_icon = step_icon(state)
        self.status.add_prefix(self.status_icon)

    def _pair(self):
        self.pair_button.set_sensitive(False)
        self.wifi.pair(self.code.get_text(), self.address.get_text())

    def _wifi_step(self, words: str):
        self.wifi_status.set_visible(True)
        self.wifi_status.set_title(words)

    def _wifi_done(self, problem: str | None):
        from .pairing import AndroidWifi

        self.pair_button.set_sensitive(True)
        self.wifi_status.set_visible(True)
        self.wifi_status.set_title(problem or "Paired and connected: it shows up in a moment")
        # Another go needs a fresh look (the phone shows a new code, on a new port).
        self.wifi = AndroidWifi(self._wifi_step, self._wifi_done)
        self._look()


class IPhonePage(Adw.NavigationPage):
    """The iPhone's steps, run one after another as each is done; each says what to do on the
    phone while it waits for the person."""

    def __init__(self, dialog: AddPhoneDialog, phone: Phone | None):
        super().__init__(title="iPhone", tag="ios")
        self.dialog = dialog
        self.phone = phone
        self.states: dict[str, str] = {}
        self.rows: dict[str, Adw.ActionRow] = {}
        self.icons: dict[str, Gtk.Widget] = {}
        self.running = None  # pairing.IPhoneStep
        self.started = False
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description="Keep the iPhone unlocked and near while it's set up.")
        for key, title, subtitle in IPHONE_STEPS:
            row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
            self.rows[key] = row
            self.icons[key] = step_icon("")
            row.add_prefix(self.icons[key])
            group.add(row)
        self.wifi_button = Gtk.Button(label="Set Up", valign=Gtk.Align.CENTER, sensitive=False)
        self.wifi_button.connect("clicked", lambda _b: self._run("wifi"))
        self.rows["wifi"].add_suffix(self.wifi_button)
        page.add(group)
        finish = Adw.PreferencesGroup()
        self.show_row = Adw.ButtonRow(title="Show the iPhone", sensitive=False)
        self.show_row.add_css_class("suggested-action")
        self.show_row.connect("activated", lambda _r: self.phone and self.dialog.show(self.phone))
        finish.add(self.show_row)
        page.add(finish)
        self.set_child(toolbar(page))

    def stop(self):
        if self.running:
            self.running.stop()
            self.running = None

    def _set(self, key: str, state: str, words: str | None = None):
        self.states[key] = state
        row = self.rows[key]
        row.remove(self.icons[key])
        self.icons[key] = step_icon(state)
        row.add_prefix(self.icons[key])
        if words:
            row.set_subtitle(words)

    def phones_changed(self, phones: list[Phone]):
        if self.phone is not None:
            self.phone = next((p for p in phones if p.id == self.phone.id), self.phone)
        else:
            self.phone = self.dialog.new_phone("ios")
        plugged = bool(self.phone and self.phone.online and self.phone.usb)
        if plugged and self.states.get("plug") != "done":
            self._set("plug", "done", f"{self.phone.name} is plugged in")
            if not self.started:
                self.started = True
                self._run("trust")
        elif not plugged and self.states.get("plug") != "done":
            self._set("plug", "running" if self.started else "", None)

    def _run(self, step: str):
        from .iphone import helper_binary
        from .pairing import IPhoneStep, ddi_folder

        helper = helper_binary()
        if not helper or not self.phone:
            self._set(step, "problem", "Handset's iPhone helper isn't built: run ./install.sh in its folder")
            return
        extra = []
        if step == "ddi":
            folder = ddi_folder()
            if folder is None:
                self._download_ddi()
                return
            extra = ["--images", folder]
        elif step == "wifi":
            config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
            extra = ["--out", os.path.join(config, "handset", "pairings", f"{self.phone.id}.rppairing")]
            self.wifi_button.set_sensitive(False)
        self._set(step, "running")
        self.stop()
        self.running = IPhoneStep(helper, self.phone.id, step, lambda event, s=step: self._event(s, event), extra)
        self.running.start()

    def _event(self, step: str, event: dict):
        kind, words = event.get("event"), event.get("message") or event.get("text")
        if kind == "waiting":
            self._set(step, "waiting", words)
        elif kind == "status":
            self._set(step, "running", words)
        elif kind == "error":
            self.running = None
            self._set(step, "problem", words)
            if step == "wifi":
                self.wifi_button.set_sensitive(True)
        elif kind == "done":
            self.running = None
            self._set(step, "done", words)
            following = {"trust": "developer-mode", "developer-mode": "ddi"}.get(step)
            if following:
                self._run(following)
            if step == "ddi":
                self.wifi_button.set_sensitive(True)
                self.show_row.set_sensitive(True)
            if step == "wifi":
                self.wifi_button.set_visible(False)

    def _download_ddi(self):
        """Apple's developer disk image isn't here: asked for, then fetched (pairing.fetch_ddi)."""
        from .pairing import DDI_SIZE_MB

        dialog = Adw.AlertDialog(
            heading="Download the Developer Disk Image?",
            body=f"The iPhone's screen and touches need Apple's developer disk image ({DDI_SIZE_MB} MB), the "
                 "one Xcode uses. Handset fetches it once from GitHub (a public copy of Xcode's), checks it, "
                 "and keeps it; Apple's server then signs it for this iPhone.")
        dialog.add_response("cancel", "Not Now")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._download_answered)
        dialog.present(self.dialog)

    def _download_answered(self, _dialog, response: str):
        if response != "download":
            self._set("ddi", "problem", "Not downloaded: the screen and touches need it")
            return
        from .pairing import fetch_ddi

        self._set("ddi", "running", "Downloading…")

        def work():
            problem = fetch_ddi(lambda words: GLib.idle_add(self._set, "ddi", "running", words))
            GLib.idle_add(self._fetched, problem)

        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, problem: str | None):
        if problem:
            self._set("ddi", "problem", problem)
        else:
            self._run("ddi")
        return GLib.SOURCE_REMOVE
