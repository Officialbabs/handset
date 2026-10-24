"""Handset's window: nothing but the phone's screen, edge to edge and live, with what Ada is doing
on it when she is.

The mouse is a finger on the glass (press, drag, let go; the wheel scrolls; the right button goes
back; the middle button goes home, and moves the window when dragged), and the keyboard types on
the phone. The phone's buttons are shortcuts: see SHORTCUTS.
"""

from __future__ import annotations

import json
import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

from . import NAME  # noqa: E402
from .gestures import Fit, Gesture, classify, inside, scroll, to_edge  # noqa: E402
from .log import debug  # noqa: E402
from .screen import Screen  # noqa: E402
from .service import Phone, Watch, ada_line, status_line  # noqa: E402

# Typed letters wait this long for more, then go to the phone together.
TYPE_AFTER_MS = 120
# Wheel movement gathers this long, then scrolls the phone in one swipe.
SCROLL_AFTER_MS = 90
# A touchpad's scroll lifts its finger off the phone when no movement has come for this long
# (in case the touchpad never says it stopped).
LIFT_AFTER_MS = 160
# After the phone's video ends with the phone still there, try again after this long, and
# twice as long each time after (an iPhone asleep, say) up to the most.
RETRY_MS = 3000
MOST_RETRY_MS = 30000
# An awake phone that sends no picture this long after its video starts (or it wakes) is opened
# again, this many times before Handset says so.
PICTURE_WAIT_MS = 8000
# An iPhone takes longer: found on Wi-Fi, a tunnel, then its screen's stream.
IPHONE_PICTURE_WAIT_MS = 25000
PICTURE_TRIES = 3

# A middle-button press that moves further than this drags the window instead of going home.
MOVE_SLOP = 6
# The most of the monitor the window takes either way, leaving room for the desktop's own bars.
SCREEN_SHARE = 0.8
# The bezel above the screen, held to move the window (there's no title bar).
BEZEL = 22
FRAME = BEZEL  # what the window has besides the screen
# The phone's shape until the phone says (a Pixel 7's, upright).
FIRST_RATIO = 1080 / 2400

CSS = b"""
.handset-window.on-screen { background: black; }
.handset-ada { border-radius: 999px; padding: 6px 14px; margin-top: 10px; font-weight: 600; }
.handset-ring { border: 3px solid var(--accent-bg-color); }
.handset-asleep { background: alpha(black, 0.6); color: white; }
.handset-asleep .title { font-weight: 700; font-size: 1.15em; }
.handset-bezel { background: #101012; }
.handset-grip { background: alpha(white, 0.32); border-radius: 3px; min-width: 44px; min-height: 5px; }
.handset-window:not(.on-screen) .handset-bezel { background: none; }
.handset-window:not(.on-screen) .handset-grip { background: alpha(currentColor, 0.2); }
"""

# The phone's buttons and the window's own, as shortcuts: (action, accelerators).
SHORTCUTS = (
    ("win.back", ["<Control>b"]),
    ("win.home", ["<Control>h"]),
    ("win.recents", ["<Control>r"]),
    ("win.notifications", ["<Control>n"]),
    ("win.next-phone", ["<Control>p"]),
    ("win.fullscreen", ["F11"]),
    ("window.close", ["<Control>w"]),
    ("app.quit", ["<Control>q"]),
)

# Keys typed on the desk that mean a phone key.
SPECIAL_KEYS = {
    "Return": "enter", "KP_Enter": "enter", "BackSpace": "delete", "Delete": "forward_delete",
    "Tab": "tab", "Escape": "back", "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Home": "move_home", "End": "move_end",
}


def settings_path() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "handset", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path()) as file:
            settings = json.load(file)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as file:
        json.dump(settings, file)
    os.replace(path + ".tmp", path)


def migrated(settings: dict) -> dict:
    """Settings as Handset keeps them since it has a window per phone: the phones with windows
    ("windows"), and each phone's window size ("sizes": its screen's longer side) and whether
    it's maximized ("maximized"). The one-window settings before that move over."""
    settings = dict(settings)
    sizes = settings.get("sizes") if isinstance(settings.get("sizes"), dict) else {}
    maximized = settings.get("maximized") if isinstance(settings.get("maximized"), dict) else {}
    phone, size = settings.pop("phone", None), settings.pop("size", None)
    was_maximized = settings.get("maximized") is True
    if isinstance(phone, str) and phone:
        if isinstance(size, int) and size > 0:
            sizes.setdefault(phone, size)
        if was_maximized:
            maximized.setdefault(phone, True)
        settings.setdefault("windows", [phone])
    settings["sizes"], settings["maximized"] = sizes, maximized
    if not isinstance(settings.get("windows"), list):
        settings["windows"] = []
    return settings


def fitted(ratio: float, screen: tuple[int, int], longer: float | None = None) -> tuple[int, int]:
    """The window's size for a phone screen of `ratio` (width ÷ height): its longer side `longer`
    (as large as fits when not given), never more than SCREEN_SHARE of the monitor either way."""
    sw, sh = screen
    if longer is None:
        longer = max(sw, sh)
    w, h = (longer * ratio, longer) if ratio < 1 else (longer, longer / ratio)
    scale = min(1.0, sw * SCREEN_SHARE / w, sh * SCREEN_SHARE / h)
    return max(1, int(w * scale)), max(1, int(h * scale))  # down, so it always fits


def window_size(ratio: float, screen: tuple[int, int], longer: float | None = None) -> tuple[int, int]:
    """The window's size: the phone's screen as fitted() makes it, with the bezel above it."""
    width, height = fitted(ratio, (screen[0], screen[1] - FRAME), longer)
    return width, height + FRAME


def choose(phones: list[Phone], wanted: str | None, taken=(), free_only: bool = False) -> Phone | None:
    """The phone to show: the one asked for (by id or name), else of the phones no other window
    shows (`taken`, their ids) an Android one that's connected, else any Android one, else the
    first; and when every phone is taken, the first (or None, with `free_only`)."""
    if not phones:
        return None
    if wanted:
        for phone in phones:
            if wanted in (phone.id, phone.name):
                return phone
    free = [p for p in phones if p.id not in taken]
    if not free:
        return None if free_only else phones[0]
    androids = [p for p in free if p.platform == "android"]
    for phone in androids:
        if phone.online:
            return phone
    return (androids or free)[0]


class Window(Adw.ApplicationWindow):
    def __init__(self, app, wanted: str | None = None):
        super().__init__(application=app, title=NAME)
        self.add_css_class("handset-window")
        # libadwaita's windows are at least 360 wide: narrower than that is a phone's own shape.
        self.set_size_request(160, 240)
        self.settings = app.settings  # shared by the app's windows
        self.size_timer = 0
        self.wanted = wanted
        self.phones: list[Phone] = []
        self.phone: Phone | None = None
        self.device = None
        self.opening = False
        self.filling = False
        self.retry = 0
        self.retries = 0
        self.typed = ""
        self.frames_seen = 0
        self.picture_timer = 0
        self.picture_tries = 0
        self.type_timer = 0
        self.scrolled = 0.0
        self.scrolled_x = 0.0
        self.scroll_timer = 0
        self.scroll_finger: tuple[float, float] | None = None  # the touchpad's finger on the phone
        self.lift_timer = 0
        self.pointer = (0.0, 0.0)
        self.finger: tuple[float, float] | None = None
        self.press_started = 0.0
        self.moved = 0.0
        self.down_at = (0.0, 0.0)
        self.snap = (0, 0)  # how far a press near an edge was moved onto it, in the phone's pixels
        self.moving = False
        self.shown_at: tuple[int, int] | None = None
        self.resize_timer = 0
        self._take_size(wanted)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self.connect("close-request", self._closing)
        for name in ("default-width", "default-height", "maximized"):
            self.connect(f"notify::{name}", self._resized)
        self.watch = Watch(self._phones_changed, self._service_gone)
        self.watch.start()
        self._show_status("phone-symbolic", "Looking for your phones…", "")

    # ---- Layout ---------------------------------------------------------------------------

    def _build(self):
        self.picture = Screen(on_resize=self._screen_resized)
        self.dots = Gtk.DrawingArea(can_target=False)
        self.dots.set_draw_func(self._draw_finger)
        # While Ada works on the phone: a ring round its screen, and what she's doing at the top.
        self.ring = Gtk.Box(can_target=False, visible=False)
        self.ring.add_css_class("handset-ring")
        self.ada = Gtk.Label()
        self.ada.add_css_class("osd")
        self.ada.add_css_class("handset-ada")
        self.ada_shown = Gtk.Revealer(child=self.ada, can_target=False, halign=Gtk.Align.CENTER,
                                      valign=Gtk.Align.START,
                                      transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        # The phone's screen went off: its last picture, dimmed, and how to wake it.
        self.asleep = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, can_target=False,
                              visible=False)
        self.asleep.add_css_class("handset-asleep")
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, vexpand=True,
                        valign=Gtk.Align.CENTER)
        off = Gtk.Label(label="Screen off")
        off.add_css_class("title")
        words.append(off)
        words.append(Gtk.Label(label="Click to wake it"))
        self.asleep.append(words)
        self.screen = Gtk.Overlay(child=self.picture, focusable=True)
        for overlay in (self.dots, self.asleep, self.ring, self.ada_shown):
            self.screen.add_overlay(overlay)
        self.frame = Gtk.AspectFrame(ratio=0.45, obey_child=False, child=self.screen)

        self.status = Adw.StatusPage(icon_name="phone-symbolic", vexpand=True)
        self.again = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER)
        self.again.add_css_class("pill")
        self.again.connect("clicked", lambda _b: self._open())
        self.wake_button = Gtk.Button(label="Wake It", halign=Gtk.Align.CENTER, visible=False)
        self.wake_button.add_css_class("pill")
        self.wake_button.add_css_class("suggested-action")
        self.wake_button.connect("clicked", lambda _b: self.device and self.device.wake())
        self.picker = Gtk.DropDown(visible=False, halign=Gtk.Align.CENTER, tooltip_text="Which phone")
        self.picker.connect("notify::selected", self._picked)
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        choices.append(self.wake_button)
        choices.append(self.again)
        choices.append(self.picker)
        self.status.set_child(choices)
        # No title bar: the status page can still be held to move the window.
        status = Gtk.WindowHandle(child=self.status)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(status, "status")
        self.stack.add_named(self.frame, "screen")
        grip = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        grip.add_css_class("handset-grip")
        self.bezel = Gtk.WindowHandle(child=grip, height_request=BEZEL, tooltip_text="Drag to move")
        self.bezel.add_css_class("handset-bezel")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.bezel)
        content.append(self.stack)
        self.toasts = Adw.ToastOverlay(child=content)
        self.set_content(self.toasts)

        # The mouse as a finger.
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.screen.add_controller(drag)
        back = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        back.connect("pressed", lambda *_a: self._key("back"))
        self.screen.add_controller(back)
        middle = Gtk.GestureDrag(button=Gdk.BUTTON_MIDDLE)
        middle.connect("drag-begin", lambda *_a: setattr(self, "moving", False))
        middle.connect("drag-update", self._middle_drag)
        middle.connect("drag-end", self._middle_end)
        self.screen.add_controller(middle)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "pointer", (x, y)))
        self.screen.add_controller(motion)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        wheel.connect("scroll", self._wheel)
        wheel.connect("scroll-end", lambda _c: self._lift_scroll())
        self.screen.add_controller(wheel)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

        for name in ("back", "home", "recents", "notifications"):
            self._action(name, lambda _a, _p, key=name: self._key(key))
        self._action("next-phone", lambda _a, _p: self._next_phone())
        self._action("fullscreen", lambda _a, _p: self.unfullscreen() if self.is_fullscreen() else self.fullscreen())
        for action, accels in SHORTCUTS:
            self.get_application().set_accels_for_action(action, accels)

    def _take_size(self, phone_id: str | None):
        """The size the person gave this phone's window last time, as the monitor allows."""
        sizes, maximized = self.settings.get("sizes", {}), self.settings.get("maximized", {})
        saved = sizes.get(phone_id) if phone_id else None
        if not isinstance(saved, int) or saved <= 0:
            saved = None
        ratio = (lambda w, h: w / h)(*self.device.shown) if self.device else FIRST_RATIO
        self.set_default_size(*window_size(ratio, self._monitor(), saved))
        if phone_id and maximized.get(phone_id):
            self.maximize()

    def _monitor(self) -> tuple[int, int]:
        """The size of the monitor the window is on (before it shows: the first one)."""
        display = self.get_display()
        surface = self.get_surface()
        monitor = display.get_monitor_at_surface(surface) if surface else None
        if monitor is None:
            monitors = display.get_monitors()
            monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return 1366, 768
        geometry = monitor.get_geometry()
        return geometry.width, geometry.height

    def _monitor_scale(self) -> float:
        """The monitor's pixels per window pixel (2 on a HiDPI screen)."""
        surface = self.get_surface()
        monitor = self.get_display().get_monitor_at_surface(surface) if surface else None
        return monitor.get_scale() if monitor else 1.0

    def _action(self, name: str, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _show_status(self, icon: str, title: str, description: str, again: bool = False,
                     wake: bool = False):
        debug("window shows:", title, "·", description)
        self.wake_button.set_visible(wake)
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(description)
        self.again.set_visible(again)
        self.stack.set_visible_child_name("status")
        self.remove_css_class("on-screen")

    # ---- Phones from Ada's service --------------------------------------------------------

    def say(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text))

    def _phones_changed(self, phones: list[Phone]):
        ids = [p.id for p in self.phones]
        self.phones = phones
        if [p.id for p in phones] != ids:
            self._fill_picker()
        taken = self.get_application().shown(besides=self)
        phone = choose(phones, self.phone.id if self.phone else self.wanted, taken)
        self._show_phone(phone)

    def _service_gone(self):
        if not self.phones:
            self._show_status("phone-symbolic", "Ada's phone service isn't running",
                              "Handset finds your phones through it. It starts with the desktop.")

    def _fill_picker(self):
        self.filling = True  # a new list selects its first row: that's not the person's choice
        self.picker.set_model(Gtk.StringList.new([p.name for p in self.phones]))
        self.picker.set_visible(len(self.phones) > 1)
        self.filling = False

    def _next_phone(self):
        """The next phone no other window shows."""
        taken = self.get_application().shown(besides=self)
        count = len(self.phones)
        for step in range(1, count):
            index = (self.picker.get_selected() + step) % count
            if self.phones[index].id not in taken:
                self.picker.set_selected(index)
                return
        if count > 1:
            self.say("The other phones have windows of their own")

    def _picked(self, picker, _param):
        if self.filling:
            return
        index = picker.get_selected()
        if not (0 <= index < len(self.phones)) or (self.phone and self.phones[index].id == self.phone.id):
            return
        chosen = self.phones[index]
        other = self.get_application().window_for(chosen.id)
        if other and other is not self:
            # Shown already: that window comes forward, and this one keeps its phone.
            self.filling = True
            if self.phone:
                self.picker.set_selected(next(i for i, p in enumerate(self.phones) if p.id == self.phone.id))
            self.filling = False
            other.present()
            return
        self._close_phone()
        self.phone = None
        self._show_phone(chosen)

    def _show_phone(self, phone: Phone | None):
        if phone is None:
            self._close_phone()
            self.phone = None
            self.set_title(NAME)
            self._show_status("phone-symbolic", "No phones yet",
                              "Plug your phone in and set it up with Ada (Settings › Phones).")
            return
        changed = self.phone is None or self.phone.id != phone.id
        was_online = bool(self.phone and self.phone.online) and not changed
        self.phone = phone
        if changed:
            self._take_size(phone.id)
            self.get_application().remember_windows()
        index = next(i for i, p in enumerate(self.phones) if p.id == phone.id)
        if self.picker.get_selected() != index:
            self.picker.set_selected(index)
        self.set_title(f"{phone.name} – {status_line(phone)}")
        self._show_ada(phone)
        if phone.platform == "ios":
            # Handset's helper finds an iPhone on Wi-Fi by itself; Ada's service seeing it come
            # back is a good moment to try again, though.
            if changed or (phone.online and not was_online and self.device is None and not self.opening):
                self._open()
        elif not phone.online:
            self._close_phone()
            self._show_status("phone-symbolic", f"{phone.name} isn't connected",
                              "Plug it in, or keep it on the same Wi-Fi as this computer.")
        elif changed or not was_online:
            self._open()

    def _show_ada(self, phone: Phone):
        line = ada_line(phone)
        if line:
            self.ada.set_label(line)
        self.ada_shown.set_reveal_child(bool(line))
        self.ring.set_visible(phone.ada_working and not phone.ada_paused)

    # ---- The phone's screen ---------------------------------------------------------------

    def _open(self):
        if self.retry:
            GLib.source_remove(self.retry)
            self.retry = 0
        phone = self.phone
        if not phone or self.opening:
            return
        self._close_phone()
        self.opening = True
        self._show_status("phone-symbolic", f"Connecting to {phone.name}…", "")
        from . import android, iphone

        def work():
            if phone.platform == "ios":
                link = iphone.open_link(phone.id, phone.address, phone.model)
            else:
                link = android.open_link(phone.id, phone.address, phone.port)
            GLib.idle_add(self._opened, phone.id, link)

        threading.Thread(target=work, daemon=True).start()

    def _opened(self, phone_id: str, link):
        from . import android, iphone

        self.opening = False
        if not self.phone or self.phone.id != phone_id:
            return GLib.SOURCE_REMOVE
        if isinstance(link, str):
            self._show_status("phone-symbolic", f"Can't reach {self.phone.name}", link, again=True)
            return GLib.SOURCE_REMOVE
        name = self.phone.name
        if isinstance(link, iphone.Link):
            self.device = iphone.IPhone(link, self._frame, self._turned, self._touch_changed,
                                        self._video_ended, self._awake_changed,
                                        on_status=self._helper_status)
            self._turned()
            self.device.start()
            self._show_at()
            self._show_status("phone-symbolic", f"Connecting to {name} over Wi-Fi…", "")
            self._expect_picture(IPHONE_PICTURE_WAIT_MS)
            return GLib.SOURCE_REMOVE
        # The phone's own size when the GPU decodes; otherwise as sharp as the monitor can show
        # it and no more, since decoding on the CPU grows with the size.
        from .decoder import hardware

        if hardware("h264"):
            edge = 0
        else:
            edge = android.stream_edge(round(min(self._monitor()) * self._monitor_scale()), link.natural)
        debug("asking the phone for video", edge or "at its own size")
        self.device = android.Android(link, self._frame, self._turned, self._touch_changed,
                                      self._video_ended, self._awake_changed, edge=edge)
        self._turned()
        self.device.start()
        self._show_at()
        if link.awake:
            self._show_status("phone-symbolic", f"Waiting for {name}'s screen…", "")
        else:
            self._show_status("phone-symbolic", f"Waking {name}…", "Its screen was off.")
        self._expect_picture()
        return GLib.SOURCE_REMOVE

    def _expect_picture(self, wait: int = PICTURE_WAIT_MS):
        """A picture should come soon: if none does while the phone is awake, open it again."""
        if self.picture_timer:
            GLib.source_remove(self.picture_timer)
        self.picture_timer = GLib.timeout_add(wait, self._picture_check, self.frames_seen)

    def _helper_status(self, text: str):
        """What the iPhone helper is doing, while there's no picture yet."""
        if text and self.phone and self.stack.get_visible_child_name() == "status":
            self._show_status("phone-symbolic", f"{text}…", "")

    def _picture_check(self, seen: int):
        self.picture_timer = 0
        if not self.device or self.frames_seen != seen:
            return GLib.SOURCE_REMOVE
        if not self.device.awake:
            return GLib.SOURCE_REMOVE  # it's off; waking it looks again
        self.picture_tries += 1
        debug("no picture from the phone; try", self.picture_tries)
        if self.picture_tries >= PICTURE_TRIES:
            self.picture_tries = 0
            self._video_ended("It sends no picture: unlock it, or plug it in again.")
        else:
            self._open()
        return GLib.SOURCE_REMOVE

    def _awake_changed(self, awake: bool):
        self.asleep.set_visible(not awake)
        if awake:
            self._expect_picture()
        if self.stack.get_visible_child_name() == "status" and self.device and self.phone:
            name = self.phone.name
            if awake:
                self._show_status("phone-symbolic", f"Waiting for {name}'s screen…", "")
            else:
                self._show_status("phone-symbolic", f"{name}'s screen is off",
                                  "Wake it here, or press its power button.", wake=True)

    def _close_phone(self):
        if self.picture_timer:
            GLib.source_remove(self.picture_timer)
            self.picture_timer = 0
        if self.device:
            self.device.stop()
            self.device = None
        self.picture.set_paintable(None)
        self.asleep.set_visible(False)

    def _frame(self, texture):
        if self.device is None:
            return
        self.picture.set_paintable(texture)
        self.frames_seen += 1
        self.picture_tries = 0
        self.retries = 0
        if self.frames_seen == 1 and os.environ.get("HANDSET_SNAPSHOT"):
            # Debugging: SIGUSR1 saves the screen as drawn here, to compare with the phone's own.
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 10, self._snapshot, os.environ["HANDSET_SNAPSHOT"])
        if self.stack.get_visible_child_name() != "screen":
            debug("window shows the phone's screen", self.get_width(), "x", self.get_height())
            self.stack.set_visible_child_name("screen")
            self.add_css_class("on-screen")
            GLib.idle_add(self._sync_shown_size)  # once the screen has its size
            self.screen.grab_focus()

    def _snapshot(self, path: str):
        """Debugging: the phone's screen as this window's renderer draws it, saved as a PNG."""
        renderer = self.get_renderer()
        snapshot = Gtk.Snapshot()
        self.picture.do_snapshot(snapshot)  # drawn as the window draws it
        node = snapshot.to_node()
        if node is not None:
            renderer.render_texture(node, None).save_to_png(path)
        return self._log_snapshot(renderer, path)

    def _log_snapshot(self, renderer, path: str):
        texture = self.picture.get_paintable()
        debug("renderer", type(renderer).__name__, "texture", type(texture).__name__,
              texture.get_format() if texture else None, "color state",
              texture.get_color_state().equal(Gdk.ColorState.get_srgb()) if texture else None,
              "saved", path)
        return GLib.SOURCE_CONTINUE

    def _turned(self):
        width, height = self.device.shown
        ratio = width / height
        self.frame.set_ratio(ratio)
        # The window takes the phone's shape and turns with it, keeping the size the person gave
        # it as far as the monitor allows, unless they made it fill the screen.
        if not (self.is_maximized() or self.is_fullscreen()):
            longer = max(self.get_width(), self.get_height() - FRAME) or None
            self.set_default_size(*window_size(ratio, self._monitor(), longer))
        GLib.timeout_add(300, self._sync_shown_size)  # once the window has turned

    def _touch_changed(self, live: bool):
        if not live:
            self.toasts.add_toast(Adw.Toast(title="Touches go in one gesture at a time on this phone"))

    def _video_ended(self, why: str):
        name = self.phone.name if self.phone else "The phone"
        self._close_phone()
        self._show_status("phone-symbolic", f"{name}'s screen stopped", why, again=True)
        # An Android phone that's gone isn't coming back by itself; an iPhone asleep is.
        if self.phone and (self.phone.online or self.phone.platform == "ios"):
            delay = min(RETRY_MS * 2 ** self.retries, MOST_RETRY_MS)
            self.retries += 1
            self.retry = GLib.timeout_add(delay, self._retry)

    def _retry(self):
        self.retry = 0
        self._open()
        return GLib.SOURCE_REMOVE

    def _resized(self, *_args):
        """The person resized it (or it turned with the phone): remembered a moment later."""
        if self.size_timer:
            GLib.source_remove(self.size_timer)
        self.size_timer = GLib.timeout_add(600, self._remember_size)

    def _remember_size(self):
        """This phone's window size, for next time."""
        self.size_timer = 0
        if not self.phone:
            return GLib.SOURCE_REMOVE
        maximized = self.is_maximized()
        if not (maximized or self.is_fullscreen()):
            width, height = self.get_default_size()
            longer = max(width, height - FRAME)
            if longer > 0:
                self.settings["sizes"][self.phone.id] = longer
        self.settings["maximized"][self.phone.id] = maximized
        save_settings(self.settings)
        return GLib.SOURCE_REMOVE

    def _closing(self, _window):
        if self.size_timer:
            GLib.source_remove(self.size_timer)
        self._remember_size()
        self.get_application().remember_windows(closing=self)
        self.watch.stop()
        self._close_phone()
        return False

    # ---- The mouse --------------------------------------------------------------------------

    def _fit(self) -> Fit | None:
        if not self.device:
            return None
        return Fit((self.screen.get_width(), self.screen.get_height()), self.device.shown)

    def _drag_begin(self, gesture, x, y):
        if self.device and not self.device.awake:
            self.device.wake()  # a click on a sleeping screen wakes it, and does nothing else
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        fit = self._fit()
        point = to_edge(fit, x, y) if fit else None
        if point is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        raw = fit.to_screen(x, y)
        self.snap = (point[0] - raw[0], point[1] - raw[1])
        self.screen.grab_focus()
        self.down_at = (x, y)
        self.press_started = time.monotonic()
        self.moved = 0.0
        self.finger = (x, y)
        self.dots.queue_draw()
        self.device.press(*point)

    def _drag_update(self, _gesture, dx, dy):
        fit = self._fit()
        if not fit or self.finger is None:
            return
        x, y = self.down_at[0] + dx, self.down_at[1] + dy
        self.moved = max(self.moved, (dx * dx + dy * dy) ** 0.5)
        self.finger = (x, y)
        self.dots.queue_draw()
        self.device.drag(*self._snapped(fit, *fit.clamp(x, y)))

    def _drag_end(self, _gesture, dx, dy):
        fit = self._fit()
        if not fit or self.finger is None:
            return
        self.finger = None
        self.dots.queue_draw()
        if self.device.live_touch:
            self.device.release()
            return
        held = (time.monotonic() - self.press_started) * 1000
        up = (self.down_at[0] + dx, self.down_at[1] + dy)
        gesture = classify(fit, self.down_at, up, held, self.moved)
        if gesture:
            end = self._snapped(fit, *gesture.end) if gesture.end else None
            self.device.gesture(Gesture(gesture.kind, self._snapped(fit, *gesture.start), end, gesture.ms))

    def _middle_drag(self, gesture, dx, dy):
        """Dragged with the middle button: the window moves, as by a title bar."""
        if self.moving or (dx * dx + dy * dy) ** 0.5 < MOVE_SLOP:
            return
        self.moving = True
        ok, x, y = gesture.get_start_point()
        point = self.screen.compute_point(self, Graphene.Point().init(x + dx, y + dy))[1]
        ox, oy = self.get_surface_transform()
        self.get_surface().begin_move(gesture.get_device(), Gdk.BUTTON_MIDDLE, point.x + ox, point.y + oy,
                                      gesture.get_current_event_time())
        gesture.reset()

    def _middle_end(self, _gesture, _dx, _dy):
        if not self.moving:
            self._key("home")
        self.moving = False

    def _screen_resized(self, width: int, height: int):
        """The screen's size in the monitor's pixels: frames are made for it a moment after the
        window settles (not at every step of a resize)."""
        self.shown_at = (width, height)
        if self.resize_timer:
            GLib.source_remove(self.resize_timer)
        self.resize_timer = GLib.timeout_add(150, self._show_at)

    def _show_at(self):
        self.resize_timer = 0
        if self.device and self.shown_at:
            self.device.show_at(*self.shown_at)
        return GLib.SOURCE_REMOVE

    def _sync_shown_size(self):
        """The screen's size as it is now, to the phone's decoder: for the first frame (its
        decoder may have come after the screen last changed size) and after a turn."""
        width, height = self.picture.get_width(), self.picture.get_height()
        if width > 0 and height > 0:
            scale = self.picture.scale()
            self.shown_at = (round(width * scale), round(height * scale))
            self._show_at()
        return GLib.SOURCE_REMOVE

    def _snapped(self, fit: Fit, x: int, y: int) -> tuple[int, int]:
        """A point of this press's path, moved as its start was onto the edge, kept on the screen."""
        (sx, sy), (w, h) = self.snap, fit.screen
        return min(max(x + sx, 0), w - 1), min(max(y + sy, 0), h - 1)

    def _wheel(self, controller, dx, dy):
        if not self.device:
            return False
        unit = controller.get_unit()
        # A mouse wheel turned with Shift held scrolls sideways, as everywhere else.
        if unit == Gdk.ScrollUnit.WHEEL and dx == 0 and controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
            dx, dy = dy, 0.0
        if unit == Gdk.ScrollUnit.SURFACE and self.device.live_touch:
            self._finger_scroll(dx, dy)
            return True
        # A wheel's notch is 1; a touchpad (with no live finger) says pixels.
        per = 1 if unit == Gdk.ScrollUnit.WHEEL else 40
        self.scrolled += dy / per
        self.scrolled_x += dx / per
        if not self.scroll_timer:
            self.scroll_timer = GLib.timeout_add(SCROLL_AFTER_MS, self._scroll_now)
        return True

    def _scroll_now(self):
        self.scroll_timer = 0
        down, right = self.scrolled, self.scrolled_x
        self.scrolled = self.scrolled_x = 0.0
        fit = self._fit()
        if fit and self.device:
            gesture = scroll(fit, self.pointer, down, right)
            if gesture:
                self.device.gesture(gesture)
        return GLib.SOURCE_REMOVE

    def _finger_scroll(self, dx: float, dy: float):
        """A touchpad's scroll as a finger on the phone, live: it goes down where the pointer is
        and moves against the scroll (fingers going up scroll the page down), so the page follows
        the touchpad and a flick flings it. Kept off the screen's edges: there it lifts, and goes
        down again further in."""
        fit = self._fit()
        if not fit:
            return
        shown = self.device.shown
        if self.scroll_finger is None:
            start = fit.to_screen(*self.pointer)
            if start is None:
                return
            self.scroll_finger = inside(shown, *start)
            self.device.press(*self.scroll_finger)
        x, y = self.scroll_finger
        scale = fit.scale or 1.0
        x, y = x - dx / scale, y - dy / scale
        if inside(shown, x, y) != (int(x), int(y)):
            self._lift_scroll()  # at the edge: up, and the next movement starts afresh
            return
        self.scroll_finger = (x, y)
        self.device.drag(int(x), int(y))
        if self.lift_timer:
            GLib.source_remove(self.lift_timer)
        self.lift_timer = GLib.timeout_add(LIFT_AFTER_MS, self._lift_quiet)

    def _lift_quiet(self):
        self.lift_timer = 0  # this timer, firing: nothing to remove
        return self._lift_scroll()

    def _lift_scroll(self):
        if self.lift_timer:
            GLib.source_remove(self.lift_timer)
            self.lift_timer = 0
        if self.scroll_finger is not None:
            self.scroll_finger = None
            if self.device:
                self.device.release()
        return GLib.SOURCE_REMOVE

    def _draw_finger(self, _area, cr, _width, _height):
        if self.finger is None:
            return
        x, y = self.finger
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.arc(x, y, 18, 0, 6.2832)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.arc(x, y, 7, 0, 6.2832)
        cr.fill()

    # ---- The keyboard -----------------------------------------------------------------------

    def _key_pressed(self, _controller, keyval, _keycode, state):
        if not self.device:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False  # the window's own shortcuts
        if not self.device.awake:
            self.device.wake()
            return True
        name = Gdk.keyval_name(keyval) or ""
        if name in SPECIAL_KEYS:
            self._key(SPECIAL_KEYS[name])
            return True
        char = chr(Gdk.keyval_to_unicode(keyval)) if Gdk.keyval_to_unicode(keyval) else ""
        if char and char.isprintable():
            self.typed += char
            if self.type_timer:
                GLib.source_remove(self.type_timer)
            self.type_timer = GLib.timeout_add(TYPE_AFTER_MS, self._type_now)
            return True
        return False

    def _type_now(self):
        self.type_timer = 0
        text, self.typed = self.typed, ""
        if text and self.device and self.device.type(text):
            self.toasts.add_toast(Adw.Toast(title="Only plain letters, digits and punctuation can be typed"))
        return GLib.SOURCE_REMOVE

    def _key(self, name: str):
        if not self.device:
            return
        if not self.device.awake:
            self.device.wake()  # a button on a sleeping phone wakes it first
            return
        if self.type_timer:  # what was typed goes first
            GLib.source_remove(self.type_timer)
            self._type_now()
        self.device.key(name)
