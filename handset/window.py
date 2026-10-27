"""Handset's window: nothing but the phone's screen, edge to edge and live, with what Ada is doing
on it when she is.

The mouse is a finger on the glass (press, drag, let go; the wheel scrolls; the right button goes
back; the middle button goes home, and moves the window when dragged), and the keyboard types on
the phone. The phone's buttons are shortcuts: see SHORTCUTS.

A window can also show one of the phone's apps (apps.py): on Android on a screen of its own
(android.AppScreen), on an iPhone the phone's screen with that app opened in front. It carries
the app's own id, so the dock counts it as that app.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

try:
    # Before any window exists: a surface wrapped before this is loaded lacks its methods.
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland  # noqa: E402
except (ImportError, ValueError):
    GdkWayland = None

from . import NAME  # noqa: E402
from . import body as bodies  # noqa: E402
from .apps import PhoneApp  # noqa: E402
from .gestures import SCROLL_PER_NOTCH, Fit, Gesture, classify, inside, safe_box, scroll, to_edge  # noqa: E402
from .log import debug  # noqa: E402
from .phonebody import PhoneBody  # noqa: E402
from .screen import Screen  # noqa: E402
from .service import Phone, ada_line, set_up_where, status_line, watch  # noqa: E402

# Typed letters wait this long for more, then go to the phone together.
TYPE_AFTER_MS = 120
# An iPhone app's window closing presses Home, and its screen ends this much later, once that's done.
HOME_FIRST_MS = 400
# Ctrl with these goes to the phone as its keyboard's shortcut (select all, copy, paste, cut, redo,
# undo; Command on an iPhone), with Shift too for these (redo; paste as plain text).
EDIT_KEYS = ("a", "c", "v", "x", "y", "z")
EDIT_KEYS_SHIFTED = ("z", "v")
# Without a live finger (Android's `input swipe`), wheel movement gathers this long, then scrolls
# the phone in one swipe (each swipe done before the next: overlapping, they'd fight).
SCROLL_AFTER_MS = 320
# A mouse wheel otherwise is a finger held on the phone: each notch moves it a step, a frame at a
# time; it rests this long before it lifts (a finger lifted still doesn't fling the page), and as
# long when it nears the screen's edge, to go down again further in.
WHEEL_FRAME_MS = 16
WHEEL_STILL_MS = 140
# A pinch: two fingers on the phone this far apart (of its screen's shorter side) to begin with,
# spreading and closing as the touchpad's fingers do; with Ctrl and a mouse wheel, by this much a
# notch (up spreads them: zooms in), lifted once the wheel has rested this long.
PINCH_START = 0.25
PINCH_PER_NOTCH = 1.18
PINCH_REST_MS = 350
# A touchpad's scroll lifts its finger off the phone when no movement has come for this long
# (in case the touchpad never says it stopped).
LIFT_AFTER_MS = 160
# Where the phone scrolls as a mouse wheel does (Android: scroll()), no finger: a wheel's notches
# glide the page, the way left falling by e every WHEEL_GLIDE_S; a touchpad moves it as the fingers
# do, and a flick throws it on at their speed (measured over their last FLICK_MS, at most
# THROW_FASTEST, the window's pixels a second), slowing as a thrown page does (its speed falling by
# e every GLIDE_FADE_S) until it's slower than GLIDE_STOP. Both step with the display's frames.
WHEEL_GLIDE_S = 0.05
FLICK_MS = 120
THROW_FASTEST = 6000.0
GLIDE_FADE_S = 0.32
GLIDE_STOP = 25.0
# After the phone's video ends with the phone still there, try again after this long, and
# twice as long each time after (an iPhone asleep, say) up to the most.
RETRY_MS = 3000
MOST_RETRY_MS = 30000
# An awake phone that sends no picture this long after its video starts (or it wakes) is opened
# again, this many times before Handset says so.
PICTURE_WAIT_MS = 8000
# An iPhone takes longer: found by its cable or on Wi-Fi, a tunnel, then its screen's stream.
IPHONE_PICTURE_WAIT_MS = 25000
PICTURE_TRIES = 3

# A middle-button press that moves further than this drags the window instead of going home.
MOVE_SLOP = 6
# The most of the monitor the window takes either way, leaving room for the desktop's own bars.
SCREEN_SHARE = 0.9
# App windows share one size (and whether they're maximized): the one any was given last.
APPS_SIZE = "apps"
# Sizes kept from this on are the person's own (before, the default was smaller): settings.
SIZES_SINCE = 2
# Above the phone, the room its options pill sits in; the pill opens in a row where the window
# is wider than the row by this much, and closes this long after the pointer leaves the window.
# Full screen, it floats over the screen's top instead, shown while the pointer is this near it.
CONTROLS = 30
FLOAT_REACH = 56
OPTIONS_ROOM = 16
OPTIONS_CLOSE_MS = 1500

CSS = b"""
window.handset-window { background: none; border-radius: 0; outline: none;
                        box-shadow: 0 0 0 10px transparent; }
window.handset-window.maximized, window.handset-window.fullscreen { box-shadow: none; }
.handset-screen-area { background: black; color: white; }
.handset-screen-area statuspage > scrolledwindow > viewport > box { margin: 0 12px; }
.handset-screen-area .dim-label, .handset-screen-area statuspage .description { color: alpha(white, 0.62); }
.handset-screen-area button.pill { background: alpha(white, 0.14); color: white; box-shadow: none; }
.handset-screen-area button.pill:hover { background: alpha(white, 0.22); }
.handset-screen-area button.pill.suggested-action { background: alpha(white, 0.24); font-weight: 700; }
.handset-screen-area dropdown > button { background: alpha(white, 0.14); color: white; }
.handset-ada { border-radius: 999px; padding: 6px 14px; margin-top: 10px; font-weight: 600; }
.handset-asleep { background: alpha(black, 0.6); color: white; }
.handset-asleep .title { font-weight: 700; font-size: 1.15em; }
.handset-away .title { font-weight: 700; }
.handset-hidden { color: alpha(white, 0.85); font-weight: 600; }
window.handset-window.maximized, window.handset-window.fullscreen { background: black; }
.handset-controls { background: alpha(#1c1c20, 0.88); color: white; border-radius: 999px; padding: 2px 3px;
                    box-shadow: 0 2px 6px alpha(black, 0.35), inset 0 0 0 1px alpha(white, 0.09);
                    transition: opacity 160ms ease-out; }
.handset-controls.resting { opacity: 0.6; }
.handset-controls separator { background: alpha(white, 0.14); margin: 6px 3px; min-width: 1px; }
.handset-control { min-width: 24px; min-height: 22px; padding: 0; margin: 0 1px; border-radius: 999px;
                   background: none; box-shadow: none; color: alpha(white, 0.72); -gtk-icon-size: 14px; }
.handset-pill { min-width: 34px; min-height: 16px; -gtk-icon-size: 14px; }
.handset-control:hover { background: alpha(white, 0.14); color: white; }
.handset-control.close:hover { background: #e01b24; color: white; }
.handset-control:checked { background: alpha(white, 0.24); color: white; }
"""

# The phone's buttons and the window's own, as shortcuts: (action, accelerators).
SHORTCUTS = (
    ("win.back", ["<Control>b"]),
    ("win.home", ["<Control>h"]),
    ("win.recents", ["<Control>r"]),
    ("win.notifications", ["<Control>n"]),
    ("win.next-phone", ["<Control>p"]),
    ("win.phone-apps", ["<Control><Shift>a"]),
    ("win.phone-files", ["<Control><Shift>f"]),
    ("win.screenshot", ["<Control><Shift>s", "Print"]),
    ("win.screen-off", ["<Control><Shift>o"]),
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
    """Settings as Handset keeps them since it has a window per phone: each phone's window size
    ("sizes": its screen's longer side) and whether it's maximized ("maximized"). The one-window
    settings before that move over; which phones had windows ("windows") isn't kept any more:
    Handset opens on its main window."""
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
    settings.pop("windows", None)
    if settings.get("sizes_since") != SIZES_SINCE:
        # Sizes kept before windows opened bigger were mostly the old, smaller default, never
        # chosen: windows open at the new one, and keep what they're given from now on.
        sizes, maximized = {}, {}
        settings["sizes_since"] = SIZES_SINCE
    settings["sizes"], settings["maximized"] = sizes, maximized
    return settings


def window_size(shape: bodies.Body, rotation: int, monitor: tuple[int, int],
                longer: float | None = None) -> tuple[int, int]:
    """The window's size: the phone (body.py), its screen's longer side `longer` (as large as fits
    when not given), never more than SCREEN_SHARE of the monitor either way; and under it, the
    room for its controls."""
    scale = bodies.fit_scale(shape, rotation, (monitor[0] * SCREEN_SHARE, monitor[1] * SCREEN_SHARE - CONTROLS))
    if longer:
        scale = min(scale, longer / max(bodies.turned(shape, rotation)))
    width, height = bodies.outer_size(shape, rotation, scale)
    return max(1, int(width)), max(1, int(height)) + CONTROLS


def pad_speed(history: list[tuple[int, float, float]], now: int) -> tuple[float, float] | None:
    """A touchpad's scroll's speed as its fingers lifted, the window's pixels a second: what it
    moved over its last FLICK_MS (as `history` has it: event times in milliseconds, and how far),
    over the time that took. None when it had stopped by then."""
    recent = [entry for entry in history if now - entry[0] <= FLICK_MS]
    if len(recent) < 2 or recent[-1][0] <= recent[0][0]:
        return None
    took = recent[-1][0] - recent[0][0]
    # Each event says how far since the one before: the first one's way came before the span.
    return (sum(entry[1] for entry in recent[1:]) * 1000 / took,
            sum(entry[2] for entry in recent[1:]) * 1000 / took)


class Ring:
    """Ada at work on the phone (or files coming to it): the accent's ring round its screen, drawn
    by the body."""

    def __init__(self, body: PhoneBody):
        self.body = body
        self.shown = False

    def set_visible(self, shown: bool):
        self.shown = shown
        color = Adw.StyleManager.get_default().get_accent_color_rgba() if shown else None
        self.body.set_ring(color)

    def get_visible(self) -> bool:
        return self.shown


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
    def __init__(self, app, wanted: str | None = None, phone_app: PhoneApp | None = None,
                 task: int | None = None):
        super().__init__(application=app, title=phone_app.name if phone_app else NAME)
        self.add_css_class("handset-window")
        self.phone_app = phone_app  # the one app this window shows, if it shows one
        # Another app's window opened this app (popout.py): its task, as it is, comes here.
        self.task = task
        self.parted = task is not None
        self.display: tuple[str, int] | None = None  # (serial, id): an Android app's display on its phone
        self.gave_screen = False  # an iPhone's one screen went to another window (screen_to)
        # An iPhone app's window: whether the iPhone was on its home screen when the app came in
        # front (None: not said), and whether minimizing sent it back there.
        self.home_before: bool | None = None
        self.sent_home = False
        # libadwaita's windows are at least 360 wide: narrower than that is a phone's own shape.
        self.set_size_request(160, 240)
        self.settings = app.settings  # shared by the app's windows
        self.size_timer = 0
        self.wanted = phone_app.phone_id if phone_app else wanted
        self.phones: list[Phone] = []
        self.phone: Phone | None = None
        self.device = None
        self.opening = False
        self.filling = False
        self.android_shape: bodies.Body | None = None  # what the Android phone said of its shape
        self.dialling: str | None = None  # an Android phone being connected over Wi-Fi for this window
        self.dialled_at = -60.0
        self.retry = 0
        self.retries = 0
        self.typed = ""
        self.frames_seen = 0
        self.snapshots = 0  # the debugging snapshot's signal handler, when there's one
        self.picture_timer = 0
        self.picture_tries = 0
        self.type_timer = 0
        self.scrolled = 0.0
        self.scrolled_x = 0.0
        self.wheel_at: tuple[float, float] | None = None  # the wheel's finger, while it's down
        self.wheel_to = (0.0, 0.0)  # where the notches so far take it
        self.wheel_timer = 0
        self.pinch_at: tuple[int, int] | None = None  # a pinch's middle on the phone, while it's on
        self.pinch_scale = 1.0  # how far its fingers are spread, of where they began
        self.pinch_to = 1.0  # Ctrl and the wheel: how far they're going
        self.pinch_timer = 0
        self.scroll_timer = 0
        self.scroll_finger: tuple[float, float] | None = None  # the touchpad's finger on the phone
        self.lift_timer = 0
        self.wheel_left = [0.0, 0.0]  # the wheel's notches still to scroll, in the phone's pixels
        self.thrown: list[float] | None = None  # a flick's speed, the phone's pixels a second
        self.motion_tick = 0  # the frames gliding or throwing the page
        self.motion_at: float | None = None
        self.pad_scrolled = False  # the touchpad's last scroll went as a wheel's: a flick throws it
        self.pad_history: list[tuple[int, float, float]] = []  # its last moves: (event time, dx, dy)
        self.pointer = (0.0, 0.0)
        self.finger: tuple[float, float] | None = None
        self.press_started = 0.0
        self.moved = 0.0
        self.down_at = (0.0, 0.0)
        self.snap = (0, 0)  # how far a press near an edge was moved onto it, in the phone's pixels
        self.moving = False
        self.shown_at: tuple[int, int] | None = None
        self.resize_timer = 0
        GLib.timeout_add(1000, self._check_hidden)
        self.connect_after("map", self._mapped)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self._take_size(self.wanted)
        self.connect("close-request", self._closing)
        self.connect("notify::is-active", self._activated)
        self.connect("notify::fullscreened", lambda *_a: self._fullscreen_changed())
        for name in ("default-width", "default-height", "maximized"):
            self.connect(f"notify::{name}", self._resized)
        self.watch = watch(self._phones_changed, self._service_gone)
        self.watch.start()
        if app.clipboard:
            app.clipboard.watch(self)
        self._show_status("phone-symbolic", "Looking for your phones…", "")

    def _mapped(self, _window):
        """An app's window goes by the app's own id, so the dock counts it as that app; a phone's
        by its phone's (apps.phone_app_id), so each phone is in the dock on its own. Set once it's
        mapped: GTK gives every window the application's id as it maps it, and the compositor takes
        the id it has when the window's first picture comes, just after (a phone chosen later
        tries again)."""
        self._take_id()

    def _take_id(self):
        from .apps import phone_app_id

        phone_id = self.phone.id if self.phone else self.wanted
        wanted = self.phone_app.id if self.phone_app else (phone_app_id(phone_id) if phone_id else None)
        surface = self.get_surface()
        if not wanted or GdkWayland is None or not isinstance(surface, GdkWayland.WaylandToplevel):
            return
        if getattr(self, "app_id", None) == wanted:
            return
        try:
            GdkWayland.WaylandToplevel.set_application_id(surface, wanted)
            self.app_id = wanted
        except (TypeError, AttributeError) as error:
            debug("the window keeps Handset's id:", error)

    def own_screen(self) -> bool:
        """Whether this window shows an app on a screen of its own (no edges to swipe from)."""
        return bool(self.phone_app) and self.device is not None and hasattr(self.device, "package")

    def size_key(self, phone_id: str | None) -> str | None:
        """What this window's size is remembered by: its phone; an app's window, the one size app
        windows share (sized once, every app opens so)."""
        return APPS_SIZE if self.phone_app else phone_id

    # ---- Layout ---------------------------------------------------------------------------

    def _build(self):
        self.picture = Screen(on_resize=self._screen_resized)
        self.dots = Gtk.DrawingArea(can_target=False)
        self.dots.set_draw_func(self._draw_finger)
        # While Ada works on the phone: a ring round its screen (Ring, drawn by the body below), and
        # what she's doing at the top.
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
        # An iPhone's one screen is in another of its windows: this one's last picture, dimmed.
        self.away = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, can_target=False, visible=False,
                            valign=Gtk.Align.END, margin_bottom=24, halign=Gtk.Align.CENTER)
        self.away.add_css_class("handset-away")
        self.away.add_css_class("osd")
        self.away.add_css_class("handset-ada")
        self.away_where = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.away_where.add_css_class("title")
        self.away.append(self.away_where)
        self.away.append(Gtk.Label(label="Click to show it here"))
        # A locked phone's PIN pad shows black: the phone hides it from screen sharing.
        self.hidden = Gtk.Label(label="The phone hides its PIN pad from screen sharing.\n"
                                      "Type your PIN, then press Enter.",
                                justify=Gtk.Justification.CENTER, wrap=True, can_target=False,
                                visible=False, valign=Gtk.Align.CENTER, margin_start=16, margin_end=16)
        self.hidden.add_css_class("handset-hidden")
        self.screen = Gtk.Overlay(child=self.picture, focusable=True)
        for overlay in (self.dots, self.asleep, self.away, self.hidden, self.ada_shown):
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
        self.here_button = Gtk.Button(label="Show Here", halign=Gtk.Align.CENTER, visible=False)
        self.here_button.add_css_class("pill")
        self.here_button.add_css_class("suggested-action")
        self.here_button.connect("clicked", lambda _b: self.take_screen())
        # Setting a phone up (in the desktop's phone settings, or Handset's own steps).
        self.setup_button = Gtk.Button(label="Set Up a Phone…", halign=Gtk.Align.CENTER, visible=False)
        self.setup_button.add_css_class("pill")
        self.setup_button.add_css_class("suggested-action")
        self.setup_button.connect("clicked", lambda _b: self._set_up_phone())
        # A phone switched off in Handset: switched on again here.
        self.connect_button = Gtk.Button(label="Connect", halign=Gtk.Align.CENTER, visible=False)
        self.connect_button.add_css_class("pill")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.connect("clicked", lambda _b: self.phone and
                                    self.get_application().choose_phone(self.phone.id, connected=True))
        self.picker = Gtk.DropDown(visible=False, halign=Gtk.Align.CENTER, tooltip_text="Which phone")
        self.picker.connect("notify::selected", self._picked)
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        choices.append(self.wake_button)
        choices.append(self.here_button)
        choices.append(self.setup_button)
        choices.append(self.connect_button)
        choices.append(self.again)
        choices.append(self.picker)
        self.status.set_child(choices)
        # No title bar: the status page can still be held to move the window.
        status = Gtk.WindowHandle(child=self.status)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_css_class("handset-screen-area")
        self.stack.add_named(status, "status")
        self.stack.add_named(self.frame, "screen")
        # The phone's screen in its rim (there's no title bar: the strip above it moves the window),
        # its corners resizing the window, the rim right-clicked for more.
        self.body = PhoneBody(self.stack)
        self.body.set_vexpand(True)
        self.ring = Ring(self.body)
        self.bezel = self.body  # (not a window handle: drags and double taps are the phone's)
        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", self._bezel_menu)
        self.bezel.add_controller(menu_click)
        # Above it, its options in a pill: clicked, it opens in a row where the window has the room,
        # and as a list over the phone where it hasn't: a screenshot (an app's window: the whole
        # phone first), the phone's own screen off, the phone's menu; then minimizing and closing.
        self.options = Gtk.Box()
        chosen = []
        if self.phone_app:
            chosen.append(self._control("phone-symbolic", "Show the whole phone", "win.whole-phone"))
        chosen.append(self._control("camera-photo-symbolic", "Screenshot (Ctrl+Shift+S)", "win.screenshot"))
        # An Android phone's own screen off while it's shown here (screenoff.py): a switch.
        self.screen_off_button = Gtk.ToggleButton(icon_name="display-brightness-symbolic", action_name="win.screen-off",
                                                  tooltip_text="Phone screen off (Ctrl+Shift+O)", visible=False,
                                                  valign=Gtk.Align.CENTER, can_focus=False, focus_on_click=False)
        self.screen_off_button.add_css_class("handset-control")
        chosen.append(self.screen_off_button)
        for button in chosen:
            self.options.append(button)
        more = self._control("open-menu-symbolic", "More", None)
        more.connect("clicked", lambda button: self._menu_at(button, button.get_width() / 2, button.get_height()))
        self.options.append(more)
        self.options.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.fullscreen_button = self._control("view-fullscreen-symbolic", "Full screen (F11)", "win.fullscreen")
        for button in (self.fullscreen_button,
                       self._control("window-minimize-symbolic", "Minimize", "win.minimize-window"),
                       self._control("window-close-symbolic", "Close", "window.close", "close")):
            self.options.append(button)
            chosen.append(button)
        for button in chosen:  # an option taken, the pill closes again
            button.connect("clicked", lambda _b: GLib.idle_add(self._close_options))
        self.options_shown = Gtk.Revealer(child=self.options, transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
                                          transition_duration=200)
        self.pill = Gtk.ToggleButton(icon_name="view-more-horizontal-symbolic", tooltip_text="Options",
                                     valign=Gtk.Align.CENTER, can_focus=False, focus_on_click=False)
        self.pill.add_css_class("handset-control")
        self.pill.add_css_class("handset-pill")
        self.pill.connect("toggled", self._pill_toggled)
        # Dragged, not clicked, the pill moves the window.
        move = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        move.connect("drag-update", self._pill_dragged)
        self.pill.add_controller(move)
        self.controls = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=2)
        self.controls.add_css_class("handset-controls")
        self.controls.add_css_class("resting")
        self.controls.append(self.pill)
        self.controls.append(self.options_shown)
        self.options_timer = 0
        self.pointer_inside = False
        # The strip it's in is held to move the window too, and right-clicked for the phone's menu.
        self.above = Gtk.WindowHandle(child=self.controls, height_request=CONTROLS)
        above_menu = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        above_menu.connect("pressed", lambda gesture, _n, x, y: (gesture.set_state(Gtk.EventSequenceState.CLAIMED),
                                                                 self._menu_at(self.above, x, y)))
        self.above.add_controller(above_menu)
        # Full screen, the pill floats over the screen's top instead, while the pointer's near it.
        self.floating = Gtk.Revealer(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=8,
                                     transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        self.float_timer = 0
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_a: self._controls_awake())
        hover.connect("leave", lambda *_a: self._pointer_left())
        hover.connect("motion", lambda _c, _x, y: self._hovering(y))
        self.add_controller(hover)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.above)
        content.append(self.bezel)
        layers = Gtk.Overlay(child=content)
        layers.add_overlay(self.floating)
        self.toasts = Adw.ToastOverlay(child=layers)
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
        # A touchpad's pinch, as two fingers pinching on the phone (to zoom a map, a photo).
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._pinch_begin)
        zoom.connect("scale-changed", lambda _g, scale: self._pinch(scale))
        zoom.connect("end", lambda *_a: self._pinch_end())
        zoom.connect("cancel", lambda *_a: self._pinch_end())
        self.screen.add_controller(zoom)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES
                                          | Gtk.EventControllerScrollFlags.KINETIC)
        wheel.connect("scroll", self._wheel)
        wheel.connect("scroll-end", lambda _c: self._lift_scroll())
        wheel.connect("decelerate", self._flicked)
        self.screen.add_controller(wheel)
        # Files dropped on the window go to the phone (its Download folder).
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._dropped)
        drop.connect("enter", lambda *_a: (self.ring.set_visible(True), Gdk.DragAction.COPY)[1])
        drop.connect("leave", lambda *_a: self.phone and self._show_ada(self.phone))
        self.toasts.add_controller(drop)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

        for name in ("back", "home", "recents", "notifications"):
            self._action(name, lambda _a, _p, key=name: self._key(key))
        self._action("next-phone", lambda _a, _p: self._next_phone())
        self._action("phone-apps", lambda _a, _p: self.show_phone_apps())
        self._action("phone-files", lambda _a, _p: self.show_phone_files())
        sound_here = Gio.SimpleAction.new_stateful("sound-here", None, GLib.Variant("b", False))
        sound_here.connect("change-state", self._sound_here)
        self.add_action(sound_here)
        screen_off = Gio.SimpleAction.new_stateful("screen-off", None, GLib.Variant("b", False))
        screen_off.connect("change-state", self._screen_off)
        screen_off.set_enabled(False)  # an Android phone's, once it's shown
        self.add_action(screen_off)
        self._action("pair-iphone", lambda _a, _p: self._pair_iphone())
        self._action("pin", lambda _a, _p: self._pin_this(True))
        self._action("unpin", lambda _a, _p: self._pin_this(False))
        self._action("whole-phone", lambda _a, _p: self.phone and self.get_application().open_window(self.phone.id))
        self._action("screenshot", lambda _a, _p: self.screenshot())
        self._action("minimize-window", lambda _a, _p: self.minimize_window())
        self._action("fullscreen", lambda _a, _p: self.unfullscreen() if self.is_fullscreen() else self.fullscreen())
        for action, accels in SHORTCUTS:
            self.get_application().set_accels_for_action(action, accels)

    @staticmethod
    def _control(icon: str, tip: str, action: str | None, *classes: str) -> Gtk.Button:
        """A small round button under the phone, that never takes the keys from the phone."""
        button = Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action, valign=Gtk.Align.CENTER,
                            can_focus=False, focus_on_click=False)
        button.add_css_class("handset-control")
        for name in classes:
            button.add_css_class(name)
        return button

    def screenshot(self):
        """The phone's screen as a picture at its own size: in Pictures › Screenshots, and on the
        clipboard. The whole phone's from the phone itself (an Android phone's screencap, an
        iPhone's screen capture service); an app's own screen as it's shown here."""
        from . import android, iphone

        phone, device = self.phone, self.device
        if not phone or not device:
            self.say("Nothing to take a picture of yet")
            return
        folder = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
                              or os.path.expanduser("~/Pictures"), "Screenshots")
        os.makedirs(folder, exist_ok=True)
        name = self.phone_app.name if self.phone_app else phone.name
        path = os.path.join(folder, f"{name} {time.strftime('%Y-%m-%d %H.%M.%S')}.png")
        serial = self._serial()
        link = getattr(device, "link", None)

        def take():
            done = False
            if serial and not self.phone_app:
                done = android.screencap(serial, path)
            elif isinstance(link, iphone.Link):
                files = iphone.FilesSession(link)
                answer = files._ask({"op": "screenshot", "to": path})
                files.stop()
                done = isinstance(answer, dict) and answer.get("event") == "screenshot"
            GLib.idle_add(taken, done)

        def taken(done: bool):
            if not done:  # as it's shown here
                texture = self.picture.get_paintable()
                if not isinstance(texture, Gdk.Texture):
                    self.say("The screenshot didn't work")
                    return GLib.SOURCE_REMOVE
                texture.save_to_png(path)
            self._screenshot_taken(path)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=take, daemon=True).start()

    def _screenshot_taken(self, path: str):
        from .clipboard import read_file, wl_copy

        if not wl_copy(read_file(path), "image/png"):
            try:
                self.get_clipboard().set_texture(Gdk.Texture.new_from_filename(path))
            except GLib.Error:
                pass
        toast = Adw.Toast(title="Screenshot saved and copied", button_label="Show")
        toast.connect("button-clicked", lambda _t: Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(os.path.dirname(path)).get_uri(), None))
        self.toasts.add_toast(toast)

    def _controls_awake(self):
        self.pointer_inside = True
        self.controls.remove_css_class("resting")
        if self.options_timer:
            GLib.source_remove(self.options_timer)
            self.options_timer = 0

    def _pointer_left(self):
        self.pointer_inside = False
        self._controls_rest()

    def _controls_rest(self):
        """The pointer left: the pill dims, and a moment later closes, but not while its menu is open."""
        if getattr(self, "menu_open", False):
            return
        self.controls.add_css_class("resting")
        if not self.options_timer:
            self.options_timer = GLib.timeout_add(OPTIONS_CLOSE_MS, self._close_options)

    def _pill_toggled(self, pill: Gtk.ToggleButton):
        """The options pill clicked: open in a row where the window has the room for one, else
        its options as a list over the phone."""
        if not pill.get_active():
            self.options_shown.set_reveal_child(False)
            return
        row = self.options.measure(Gtk.Orientation.HORIZONTAL, -1)[1] + pill.get_width()
        if self.get_width() >= row + OPTIONS_ROOM:
            self.options_shown.set_reveal_child(True)
        else:
            pill.set_active(False)
            self._menu_at(pill, pill.get_width() / 2, pill.get_height(), window=True)

    def _pill_dragged(self, gesture: Gtk.GestureDrag, dx: float, dy: float):
        """The pill dragged, not clicked: the window moves with it (the desktop moving it)."""
        if math.hypot(dx, dy) < MOVE_SLOP:
            return
        native = self.get_native()
        surface = native.get_surface() if native else None
        started, x, y = gesture.get_start_point()
        if not started or not isinstance(surface, Gdk.Toplevel):
            return
        found, point = self.pill.compute_point(native, Graphene.Point().init(x + dx, y + dy))
        if not found:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)  # no click: the pill stays as it was
        sx, sy = native.get_surface_transform()
        surface.begin_move(gesture.get_device(), gesture.get_current_button(), point.x + sx, point.y + sy,
                           gesture.get_current_event_time())

    def _fullscreen_changed(self):
        """Full screen: the phone on black, as large as fits, no rim; its pill floating over the
        screen's top while the pointer's near it. Back, the pill above it again."""
        full = self.is_fullscreen()
        self.body.set_bare(full)
        self.fullscreen_button.set_icon_name("view-restore-symbolic" if full else "view-fullscreen-symbolic")
        self.fullscreen_button.set_tooltip_text("Leave full screen (F11)" if full else "Full screen (F11)")
        if full and self.controls.get_parent() is self.above:
            self.above.set_child(None)
            self.floating.set_child(self.controls)
        elif not full and self.controls.get_parent() is self.floating:
            self.floating.set_child(None)
            self.above.set_child(self.controls)
        self.above.set_visible(not full)
        self.floating.set_reveal_child(full)
        if full:
            self._float_later()

    def _hovering(self, y: float):
        """Full screen, the pointer near the top shows the pill; away, it goes a moment later."""
        if not self.is_fullscreen():
            return
        if y <= FLOAT_REACH:
            self.floating.set_reveal_child(True)
            if self.float_timer:
                GLib.source_remove(self.float_timer)
                self.float_timer = 0
        else:
            self._float_later()

    def _float_later(self):
        if not self.float_timer:
            self.float_timer = GLib.timeout_add(OPTIONS_CLOSE_MS, self._float_away)

    def _float_away(self):
        self.float_timer = 0
        if self.is_fullscreen() and not getattr(self, "menu_open", False) and not self.pill.get_active():
            self.floating.set_reveal_child(False)
        return GLib.SOURCE_REMOVE

    def _close_options(self):
        self.options_timer = 0
        if self.pill.get_active():
            self.pill.set_active(False)
        return GLib.SOURCE_REMOVE

    def _bezel_menu(self, gesture, _n, x, y):
        """Right-click on the phone round its screen (on the screen, it's Back)."""
        box = self.body.screen_box()
        if box and self.stack.get_visible_child_name() == "screen" and \
                box[0] <= x < box[0] + box[2] and box[1] <= y < box[1] + box[3]:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._menu_at(self.bezel, x, y)

    def _menu_at(self, widget: Gtk.Widget, x: float, y: float, window: bool = False):
        """The phone's menu: its apps and files, and more (an app's window: its pin); with
        `window`, full screen, minimizing and closing too (the options pill's, with no room for its row)."""
        from . import apps

        sound = self.get_application().sound
        self.sound_switched(bool(sound and self.phone and sound.is_on(self.phone.id)))
        menu = Gio.Menu()
        if self.phone_app:
            pinned = apps.is_pinned(self.phone_app.phone_id, self.phone_app.app)
            menu.append("Unpin from the Dock" if pinned else "Pin to the Dock", "win.unpin" if pinned else "win.pin")
            menu.append("Show the Whole Phone", "win.whole-phone")
            menu.append("Phone Files…", "win.phone-files")
            menu.append("Take a Screenshot", "win.screenshot")
            menu.append("Play Sound Here", "win.sound-here")
            if self.phone and self.phone.platform == "android":
                menu.append("Phone Screen Off", "win.screen-off")
        else:
            menu.append("Phone Apps…", "win.phone-apps")
            menu.append("Phone Files…", "win.phone-files")
            menu.append("Take a Screenshot", "win.screenshot")
            menu.append("Play Sound Here", "win.sound-here")
            if self.phone and self.phone.platform == "android":
                menu.append("Phone Screen Off", "win.screen-off")
            menu.append("Pair an iPhone for Notifications…", "win.pair-iphone")
            menu.append("New Window", "app.new-window")
            menu.append("Phones and Settings…", "app.phones")
        if window:
            section = Gio.Menu()
            section.append("Leave Full Screen" if self.is_fullscreen() else "Full Screen", "win.fullscreen")
            section.append("Minimize", "win.minimize-window")
            section.append("Close", "window.close")
            menu.append_section(None, section)
        popover = Gtk.PopoverMenu(menu_model=menu, has_arrow=False, halign=Gtk.Align.CENTER if window else Gtk.Align.START)
        popover.set_parent(widget)
        point = Gdk.Rectangle()
        point.x, point.y, point.width, point.height = int(x), int(y), 1, 1
        popover.set_pointing_to(point)
        self.menu_open = True

        def closed(menu_popover):
            self.menu_open = False
            if not self.pointer_inside:
                self._controls_rest()
            GLib.idle_add(menu_popover.unparent)

        popover.connect("closed", closed)
        popover.popup()

    def _pair_iphone(self):
        """This computer, visible to iPhones for a few minutes: one paired from its Bluetooth
        settings shares its notifications here (ancs.py)."""
        from .appsync import ask_to_pair_iphone, start_if_needed

        start_if_needed()
        ask_to_pair_iphone()
        self.say("On the iPhone: Settings › Bluetooth, then pick this computer")

    def show_phone_apps(self):
        """The phone's apps, to open one in a window of its own or pin it to the dock."""
        if self.phone_app or not self.phone:
            return
        from .appsdialog import AppsDialog

        AppsDialog(self, self.phone).present(self)

    def _pin_this(self, wanted: bool):
        from . import apps

        app = self.phone_app
        if not app:
            return
        if not wanted:
            apps.unpin(app.phone_id, app.app)
            self.say(f"{app.name} is out of the dock")
            return
        docked = apps.pin(app, None)  # with the icon its entry has
        self.say(f"{app.name} is in the dock" if docked else "Pinned: it's in your apps, to put in the dock from there")

    def _take_size(self, phone_id: str | None):
        """The size the person gave this phone's (or app's) window last time, as the monitor allows."""
        sizes, maximized = self.settings.get("sizes", {}), self.settings.get("maximized", {})
        key = self.size_key(phone_id)
        saved = sizes.get(key) if key else None
        if not isinstance(saved, int) or saved <= 0:
            saved = None
        shape, rotation = self._shape()
        self.set_default_size(*window_size(shape, rotation, self._monitor(), saved))
        if key and maximized.get(key):
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

    def _monitor_rate(self) -> int:
        """Frames a second the window's monitor shows (60 when it doesn't say): what's asked of
        the phone, so none it could send are left out and none are sent for nothing."""
        display = self.get_display()
        surface = self.get_surface()
        monitor = display.get_monitor_at_surface(surface) if surface else None
        from .android import FRAME_RATE

        rate = round(monitor.get_refresh_rate() / 1000) if monitor else 0
        return rate if rate >= 30 else FRAME_RATE

    def _monitor_scale(self) -> float:
        """The monitor's pixels per window pixel (2 on a HiDPI screen)."""
        surface = self.get_surface()
        monitor = self.get_display().get_monitor_at_surface(surface) if surface else None
        return monitor.get_scale() if monitor else 1.0

    def _action(self, name: str, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _set_up_phone(self):
        """Setting a phone up: in the desktop's phone settings where it has them, else in
        Handset's own window."""
        from .service import open_phone_settings

        if not open_phone_settings():
            self.get_application().show_phones().add_phone()

    def _show_status(self, icon: str, title: str, description: str, again: bool = False,
                     wake: bool = False, setup: bool = False, connect: bool = False):
        debug("window shows:", title, "·", description)
        self.wake_button.set_visible(wake)
        self.connect_button.set_visible(connect)
        self.setup_button.set_visible(setup)
        self.here_button.set_visible(self.gave_screen)
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
        if self.phone_app:
            # An app's window is that phone's, or nothing.
            phone = next((p for p in phones if p.id == self.phone_app.phone_id), None)
            if phone is None:
                self._close_phone()
                self.phone = None
                self._show_status("phone-symbolic", f"{self.phone_app.phone_name} isn't here",
                                  f"Set it up {set_up_where()}, and plug it in.")
                return
            self._show_phone(phone)
            return
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
        self.picker.set_visible(len(self.phones) > 1 and not self.phone_app)
        self.filling = False

    def _next_phone(self):
        """The next phone no other window shows."""
        if self.phone_app:
            return
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
                              f"Plug your phone in and set it up {set_up_where()}.", setup=True)
            return
        changed = self.phone is None or self.phone.id != phone.id
        was_online = bool(self.phone and self.phone.online) and not changed
        self.phone = phone
        if changed:
            self._take_size(phone.id)
            if self.get_mapped():
                self._take_id()  # a phone chosen once the window was open
        index = next(i for i, p in enumerate(self.phones) if p.id == phone.id)
        if self.picker.get_selected() != index:
            self.picker.set_selected(index)
        if self.phone_app:
            self.set_title(f"{self.phone_app.name} – {phone.name}")
        else:
            self.set_title(f"{phone.name} – {status_line(phone)}")
        self._show_ada(phone)
        from .finder import UNTRUSTED
        from .service import CABLE_ONLY, DISCONNECTED

        if phone.problem == DISCONNECTED:
            self._let_go()
            self._show_status("phone-symbolic", f"{phone.name} is disconnected",
                              "Handset leaves it alone: no screen, notifications, clipboard or sound.", connect=True)
        elif phone.problem == CABLE_ONLY:
            self._let_go()
            self._show_status("phone-symbolic", f"{phone.name} is on Wi-Fi", phone.problem)
        elif phone.problem == UNTRUSTED:
            self._close_phone()
            self._show_status("phone-symbolic", f"{phone.name} doesn't trust this computer yet",
                              f"Set it up {set_up_where()}: unlock it, and tap Trust when it asks.", setup=True)
        elif phone.platform == "ios":
            # Handset's helper looks for an iPhone by its cable and on Wi-Fi by itself; Ada's service
            # seeing it come back (plugged in, say) is a good moment to try again, though.
            if changed or (phone.online and not was_online and self.device is None and not self.opening):
                self._open()
        elif not phone.online:
            self._close_phone()
            if phone.platform == "android" and self._dial(phone):
                self._show_status("phone-symbolic", f"Connecting to {phone.name}…", "Over Wi-Fi, where it was last.")
            else:
                self._show_status("phone-symbolic", f"{phone.name} isn't connected",
                                  "Plug it in, or keep it on the same Wi-Fi as this computer.")
        elif changed or not was_online:
            self._open()

    def _dial(self, phone: Phone) -> bool:
        """An Android phone wanted here but not connected: connected where it was last on Wi-Fi,
        as an iPhone's helper reaches its phone when it's wanted (it opens here once the phone's
        service sees it). Whether it's being connected."""
        from . import adbwifi

        if self.dialling == phone.id:
            return True
        if time.monotonic() - self.dialled_at < adbwifi.AGAIN_S or not adbwifi.remembered(phone.id):
            return False
        self.dialling, self.dialled_at = phone.id, time.monotonic()

        def dial():
            GLib.idle_add(self._dialled, phone.id, adbwifi.connect_now(phone.id))

        threading.Thread(target=dial, daemon=True).start()
        return True

    def _dialled(self, phone_id: str, connected: bool):
        self.dialling = None
        if not connected and self.phone and self.phone.id == phone_id and not self.phone.online:
            self._show_status("phone-symbolic", f"{self.phone.name} isn't connected",
                              "Plug it in, or keep it on the same Wi-Fi as this computer.")
        return GLib.SOURCE_REMOVE

    def _let_go(self):
        """The phone's screen closed, and its clipboard and sound let go (unless another window
        shows it)."""
        self._close_phone()
        app = self.get_application()
        showing = {w.phone.id for w in app.handset_windows() if w.phone and w.device and w is not self}
        if app.clipboard:
            app.clipboard.release(showing)
        if app.sound:
            app.sound.release(showing)
        if app.screen_off:
            app.screen_off.release(showing)

    def _show_ada(self, phone: Phone):
        # Ada works on the phone's own screen, not on an app's screen of its own.
        line = "" if (self.phone_app and phone.platform == "android") else ada_line(phone)
        if line:
            self.ada.set_label(line)
        self.ada_shown.set_reveal_child(bool(line))
        self.ring.set_visible(bool(line) and phone.ada_working and not phone.ada_paused)

    # ---- The phone's screen ---------------------------------------------------------------

    def _open(self):
        if self.retry:
            GLib.source_remove(self.retry)
            self.retry = 0
        phone = self.phone
        if not phone or self.opening or self.gave_screen:
            return
        if self.get_application().screen_to(self):
            return  # an iPhone's one screen came here from the window showing it, link and all
        self._close_phone()
        self.opening = True
        self._show_status("phone-symbolic", f"Connecting to {phone.name}…", "")
        from . import android, iphone

        def work():
            shape = None
            if phone.platform == "ios":
                link = iphone.open_link(phone.id, phone.address, phone.model)
            else:
                link = android.open_link(phone.id, phone.address, phone.port)
                if isinstance(link, android.Link):
                    shape = bodies.android_body(phone.id, link.serial, link.natural)
            GLib.idle_add(self._opened, phone.id, link, shape)

        threading.Thread(target=work, daemon=True).start()

    def _opened(self, phone_id: str, link, shape: bodies.Body | None = None):
        from . import android, iphone

        self.opening = False
        if not self.phone or self.phone.id != phone_id:
            return GLib.SOURCE_REMOVE
        if shape:
            self.android_shape = shape
        if isinstance(link, str):
            self._show_status("phone-symbolic", f"Can't reach {self.phone.name}", link, again=True)
            return GLib.SOURCE_REMOVE
        name = self.phone.name
        if isinstance(link, iphone.Link):
            self.device = iphone.IPhone(link, self._frame, self._turned, self._touch_changed,
                                        self._video_ended, self._awake_changed,
                                        on_status=self._helper_status)
            self.device.looks_dark = self.picture.is_dark
            self.device.shared = lambda: bool(self.phone and self.phone.ada_working)
            self._link_clipboard(lambda on_text, on_image: iphone.ClipboardLink(link, on_text, on_image))
            if self.phone_app:
                self.device.on_launched = self._launched
                self.device.on_front = self._front_was
                self.device.launch(self.phone_app.app, front=True)
            self._turned()
            self.device.start()
            self._show_at()
            self._show_status("phone-symbolic", f"Connecting to {name}…", "")
            self._expect_picture(IPHONE_PICTURE_WAIT_MS)
            self._resume_sound()
            self._screen_off_offered(False)
            return GLib.SOURCE_REMOVE
        # The phone's own size when the GPU decodes; otherwise as sharp as the monitor can show
        # it and no more, since decoding on the CPU grows with the size.
        from .decoder import hardware

        if hardware("h264"):
            edge = 0
        else:
            edge = android.stream_edge(round(min(self._monitor()) * self._monitor_scale()), link.natural)
        debug("asking the phone for video", edge or "at its own size")
        self._link_clipboard(lambda on_text, _on_image: android.ClipboardLink(link.serial, on_text))
        GLib.idle_add(self._resume_sound)
        self._screen_off_offered(True)
        if self.phone_app:
            self.device = android.AppScreen(link, self.phone_app.app, self._frame, self._turned,
                                            self._touch_changed, self._video_ended, self._awake_changed,
                                            fps=self._monitor_rate(), task=self.task,
                                            on_display=self._display_made)
            self.task = None  # opened again, it's the app's start
            self._turned()
            self.device.start()
            self._show_at()
            self._show_status("phone-symbolic", f"Opening {self.phone_app.name}…", "")
            self._expect_picture()
            return GLib.SOURCE_REMOVE
        self.device = android.Android(link, self._frame, self._turned, self._touch_changed,
                                      self._video_ended, self._awake_changed, edge=edge,
                                      fps=self._monitor_rate())
        self._turned()
        self.device.start()
        self._show_at()
        if link.awake:
            self._show_status("phone-symbolic", f"Waiting for {name}'s screen…", "")
        else:
            self._show_status("phone-symbolic", f"Waking {name}…", "Its screen was off.")
        self._expect_picture()
        return GLib.SOURCE_REMOVE

    def _serial(self) -> str | None:
        """The adb serial of this window's phone, when it's an Android phone that's open."""
        link = getattr(self.device, "link", None)
        return getattr(link, "serial", None)

    def _files(self):
        """This window's phone's files (android.AdbFiles, iphone.FilesSession), while it's open."""
        from . import android, iphone

        serial = self._serial()
        if serial:
            return android.AdbFiles(serial)
        link = getattr(self.device, "link", None)
        if isinstance(link, iphone.Link):
            return iphone.FilesSession(link)
        return None

    def _dropped(self, _target, value, _x, _y) -> bool:
        if self.phone:
            self._show_ada(self.phone)
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        files = self._files()
        if not paths or not self.phone:
            return False
        if not files:
            self.say(f"{self.phone.name} isn't connected")
            return False
        name = self.phone.name
        count = f"{len(paths)} files" if len(paths) > 1 else os.path.basename(paths[0].rstrip("/"))
        self.say(f"Sending {count} to {name}…")

        def send():
            sent, where, trouble = files.deliver(paths)
            if getattr(files, "opened_shortcuts", False) and \
                    not files.go_back(self.phone_app.app if self.phone_app else None):
                GLib.idle_add(self._back_to_app, files.was)
            files.stop()
            if "set up" in where:
                GLib.idle_add(self._photos_setup)
                where = where.replace("set up", f"Files › On My iPhone, waiting for Photos")
            if sent and not trouble:
                words = f"{count} is on {name}: {where}"
            elif sent:
                words = f"Some went to {name} ({where}); the rest didn't ({trouble})"
            else:
                words = f"{count} didn't go to {name} ({trouble})"
            GLib.idle_add(self.say, words)

        threading.Thread(target=send, daemon=True).start()
        return True

    def _back_to_app(self, was):
        """After a drop put Shortcuts in front, and the phone couldn't open the app it was in
        again (FilesSession.go_back): the home screen if that was in front, else the app used
        before Shortcuts, by the swipe along the bottom."""
        device = self.device
        if device is None or not getattr(device, "live_touch", False):
            return GLib.SOURCE_REMOVE
        device.key("home" if was is None else "previous_app")
        return GLib.SOURCE_REMOVE

    def _photos_setup(self):
        """How to have dropped photos land in the iPhone's Photos: a shortcut, made once."""
        from .iphone import FilesSession

        dialog = Adw.AlertDialog(heading="Put Dropped Photos in Photos", body=(
            "On the iPhone, once:\n"
            "1. Photos › Albums › + › New Album, named “Handset”.\n"
            f"2. Shortcuts › + , named “{FilesSession.PHOTOS_SHORTCUT}”.\n"
            "3. Add “Get Contents of Folder”: On My iPhone › LocalSend › Handset.\n"
            "4. Add “Save to Photo Album”: Handset. Done.\n\n"
            "Then photos and videos dropped here go to that album (keep the iPhone unlocked). "
            "These ones are in Files › On My iPhone › LocalSend › Handset for now."))
        dialog.add_response("ok", "OK")
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def show_phone_files(self):
        """The phone's files: to look through, save here, open, or send to."""
        if not self.phone:
            return
        from .filesdialog import FilesDialog

        FilesDialog(self, self.phone, self._files()).present(self)

    def _resume_sound(self):
        sound = self.get_application().sound
        if sound:
            sound.resume(self)
        return GLib.SOURCE_REMOVE

    def _sound_here(self, action, value):
        """Play Sound Here, switched: the phone's sound comes here, or goes back to the phone."""
        action.set_state(value)
        sound = self.get_application().sound
        if sound and self.phone:
            sound.switch(self, value.get_boolean())
            self.say(f"{self.phone.name}'s sound plays here" if value.get_boolean()
                     else f"{self.phone.name}'s sound plays on the phone")

    def _screen_off(self, action, value):
        """Phone Screen Off, switched: the phone's own screen dark while it's shown here, or lit."""
        screen = self.get_application().screen_off
        if screen and self.phone:
            screen.switch(self, value.get_boolean())
            if value.get_boolean() and screen.is_on(self.phone.id):
                self.say(f"{self.phone.name}'s screen is off")
            elif not value.get_boolean():
                self.say(f"{self.phone.name}'s screen is on")

    def screen_off_switched(self, on: bool):
        """The switch shown as it is (switched in another of the phone's windows, or back on by
        itself)."""
        action = self.lookup_action("screen-off")
        if action and action.get_state().get_boolean() != on:
            action.set_state(GLib.Variant("b", on))

    def _screen_off_offered(self, android: bool):
        """Phone Screen Off, offered for an Android phone's window and shown as that phone's is."""
        action = self.lookup_action("screen-off")
        action.set_enabled(android)
        self.screen_off_button.set_visible(android)
        screen = self.get_application().screen_off
        self.screen_off_switched(bool(android and screen and self.phone and screen.is_on(self.phone.id)))

    def sound_switched(self, on: bool):
        """The switch shown as it is (it went off by itself, say)."""
        action = self.lookup_action("sound-here")
        if action and action.get_state().get_boolean() != on:
            action.set_state(GLib.Variant("b", on))

    def _link_clipboard(self, make_link):
        """The clipboard shared with this window's phone (one link per phone, however many
        windows show it)."""
        clipboard = self.get_application().clipboard
        if clipboard and self.phone:
            clipboard.attach(self.phone.id, make_link)

    def give_screen(self, to) -> bool:
        """The iPhone's one screen goes to another of its windows (`to`), with the link to the
        phone if this window has it, so it shows there at once. This one keeps its last picture,
        dimmed, until it's clicked or comes forward again. Whether the link went."""
        self.gave_screen = True
        if self.retry:
            GLib.source_remove(self.retry)
            self.retry = 0
        if self.picture_timer:
            GLib.source_remove(self.picture_timer)
            self.picture_timer = 0
        device, self.device = self.device, None
        moved = device is not None and not device.stopped and to.device is None
        showing = self.stack.get_visible_child_name() == "screen"
        if moved:
            to.adopt(device, self.picture.get_paintable() if showing else None)
        elif device:
            device.stop()
        self.asleep.set_visible(False)
        self.hidden.set_visible(False)
        where = f"{to.phone_app.name}'s window" if to.phone_app else f"{self.phone.name}'s window"
        if showing and self.picture.get_paintable() is not None:
            self.away_where.set_label(f"In {where}")
            self.away.set_visible(True)
            self.picture.set_opacity(0.5)
        else:
            self._show_status("phone-symbolic", f"{self.phone.name}'s screen is in {where}", "")
        return moved

    def take_screen(self):
        """Show Here: the iPhone's screen back in this window (its app in front, for an app's)."""
        if not self.gave_screen:
            return
        self.gave_screen = False
        self.away.set_visible(False)
        self.picture.set_opacity(1)
        self._open()

    def adopt(self, device, picture):
        """The iPhone's link, from the window that showed it: its pictures, touches and news
        come here now, and its last `picture` shows until the next. An app's window brings its
        app in front."""
        self._close_phone()
        self.device = device
        device.on_frame, device.on_turned, device.on_touch = self._frame, self._turned, self._touch_changed
        device.on_ended, device.on_awake, device.on_status = self._video_ended, self._awake_changed, self._helper_status
        device.on_launched = self._launched
        device.on_front = self._front_was
        device.looks_dark = self.picture.is_dark
        self.picture_tries = self.retries = 0
        self._turned()
        if picture is not None:
            self.frames_seen = 0
            self._frame(picture)
            GLib.idle_add(self._sync_shown_size)  # frames made for this window's size from now
        else:
            self._show_status("phone-symbolic", f"Waiting for {self.phone.name}'s screen…", "")
            self._expect_picture(IPHONE_PICTURE_WAIT_MS)
        self.asleep.set_visible(not device.awake)
        if self.phone_app:
            device.launch(self.phone_app.app, front=True)
        debug("the iPhone's screen moved to", self.phone_app.name if self.phone_app else "its window")

    def _activated(self, *_args):
        """An iPhone's window coming forward shows the iPhone again (its app in front, for an
        app's): the iPhone has one screen, and it goes to the window in use. An app's window sent
        home as it was minimized brings its app back."""
        if self.is_active() and self.gave_screen:
            self.take_screen()
        elif self.is_active() and self.sent_home:
            self.sent_home = False
            if self.device and self.phone_app and getattr(self.device, "live_touch", False):
                self.device.launch(self.phone_app.app, front=True)

    def _front_was(self, home: bool):
        """What this app's window's app came in front of, as the iPhone said: the home screen, or
        an app (where minimizing the window leaves it)."""
        self.home_before = home

    def minimize_window(self):
        """Minimize (the bezel's button): an iPhone app's window whose app was opened over the
        home screen leaves the iPhone on its home screen again. Not a dark iPhone (Home would
        light it), nor one the phone didn't say of."""
        device = self.device
        if (self.phone and self.phone.platform == "ios" and self.phone_app and self.home_before is True
                and device is not None and getattr(device, "live_touch", False) and getattr(device, "awake", True)):
            device.key("home")
            self.sent_home = True
        self.minimize()

    def _launched(self, opened: bool, why: str):
        """The iPhone opened this window's app (or didn't: locked, mostly)."""
        if not opened and self.phone_app:
            self.say(f"{self.phone_app.name} didn't open. {why}")

    def open_app(self, app: str):
        """Opens an app on this window's iPhone, in front."""
        if self.device and hasattr(self.device, "launch"):
            self.device.launch(app)

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
        screen = self.get_application().screen_off
        if not awake and screen and self.phone:
            screen.asleep(self.phone.id)  # asleep: its screen is its own again
        if awake:
            self._expect_picture()
        if self.stack.get_visible_child_name() == "status" and self.device and self.phone:
            name = self.phone.name
            if awake:
                self._show_status("phone-symbolic", f"Waiting for {name}'s screen…", "")
            else:
                self._show_status("phone-symbolic", f"{name}'s screen is off",
                                  "Wake it here, or press its power button.", wake=True)

    def _display_made(self, display: int):
        """This app's display is made: the apps it opens come out of it into windows of their own."""
        serial = self._serial()
        if serial:
            self.get_application().app_display(self, serial, display)

    def _iphone_home(self) -> bool:
        """An iPhone app's window closing: the iPhone back on its home screen, as closing an app's
        window leaves it on an Android phone. Pressed through whichever window has the iPhone's
        screen now; not when the iPhone is dark (Home would light it). When it's this window's
        screen, that ends a moment later, so the press is done first (the helper ends as soon as
        its input does), and is then handed back (True)."""
        app = self.get_application()
        windows = [self] + [w for w in app.handset_windows()
                            if w is not self and w.phone and w.phone.id == self.phone.id]
        device = next((w.device for w in windows if w.device is not None and getattr(w.device, "live_touch", False)),
                      None)
        if device is None or not getattr(device, "awake", True):
            return False
        device.key("home")
        if device is not self.device:
            return False
        self.device = None  # stopped here, not by _close_phone
        GLib.timeout_add(HOME_FIRST_MS, self._stop_after_home, device, app)
        return True

    def _stop_after_home(self, device, app):
        device.stop()
        if not self.gave_screen:
            app.screen_back(self)
        return GLib.SOURCE_REMOVE

    def _close_phone(self):
        if self.wheel_timer:
            GLib.source_remove(self.wheel_timer)
            self.wheel_timer = 0
        self.wheel_at = None  # its finger goes with the phone's screen
        self._stop_gliding()
        if self.pinch_timer:
            GLib.source_remove(self.pinch_timer)
            self.pinch_timer = 0
        self.pinch_at = None
        if self.display:
            self.get_application().app_display_gone(self)
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
        if self.frames_seen == 1 and os.environ.get("HANDSET_SNAPSHOT") and not self.snapshots:
            # Debugging: SIGUSR1 saves the screen as drawn here, to compare with the phone's own
            # (each window's in a file of its own: NAME-android.png, NAME-ios-com.apple.Music.png…).
            root, ext = os.path.splitext(os.environ["HANDSET_SNAPSHOT"])
            which = "-".join([self.phone.platform] + ([self.phone_app.app] if self.phone_app else []))
            self.snapshots = GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 10, self._snapshot, f"{root}-{which}{ext}")
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
        self.frame.set_ratio(width / height)
        # The window takes the phone's shape and turns with it, keeping the size the person gave
        # it as far as the monitor allows, unless they made it fill the screen.
        longer = self._screen_longer()
        shape, rotation = self._shape()
        if not (self.is_maximized() or self.is_fullscreen()):
            self.set_default_size(*window_size(shape, rotation, self._monitor(), longer))
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
        key = self.size_key(self.phone.id)
        if not (maximized or self.is_fullscreen()):
            longer = self._screen_longer()
            if longer:
                self.settings["sizes"][key] = round(longer)
            self._keep_shape()
        self.settings["maximized"][key] = maximized
        save_settings(self.settings)
        return GLib.SOURCE_REMOVE

    def _screen_longer(self) -> float | None:
        """The screen's longer side as the window draws it now."""
        box = self.body.screen_box()
        return max(box[2], box[3]) if box and max(box[2], box[3]) > 1 else None

    def _keep_shape(self):
        """Resized by hand, the window comes to the phone's shape: the phone as it fits now, and
        no more round it."""
        shape = self.body.shape
        if not shape:
            return
        width, height = self.get_default_size()
        wanted = max(1, round(shape.outer[2])), max(1, round(shape.outer[3])) + CONTROLS
        if abs(wanted[0] - width) > 1 or abs(wanted[1] - height) > 1:
            self.set_default_size(*wanted)

    def _shape(self) -> tuple[bodies.Body, int]:
        """The phone's own shape (body.py), turned as its screen shows, put round the screen."""
        phone, device = self.phone, self.device
        shown = tuple(device.shown) if device else None
        rotation = 0
        if phone and phone.platform == "ios":
            from .iphone import SCREENS

            natural = SCREENS.get((phone.model or "").strip()) or (tuple(sorted(shown)) if shown else (1170, 2532))
            shape = bodies.iphone_body(phone.model or "", natural)
            if shown and (shown[0] > shown[1]) != (natural[0] > natural[1]):
                rotation = 90  # turned (which way, an iPhone doesn't say)
        else:
            natural = tuple(device.link.natural) if device else None
            shape = ((self.android_shape if self.android_shape and natural and
                      tuple(self.android_shape.screen) == natural else None)
                     or (bodies.kept(phone.id) if phone else None)
                     or bodies.fallback("android", natural or (1080, 2400)))
            rotation = getattr(device, "rotation", 0) if device else 0
        cutout = True
        if self.own_screen() and shown:
            # An app on a screen of its own: that screen's size, the phone's corners, no cutout.
            grown = min(shown) / min(shape.screen)
            shape = bodies.Body(shown, shape.corner * grown)
            rotation, cutout = 0, False
        self.body.set_body(shape, rotation, cutout)
        return shape, rotation

    def _closing(self, _window):
        if self.size_timer:
            GLib.source_remove(self.size_timer)
        self._remember_size()
        app = self.get_application()
        # An iPhone app's window: the iPhone home first, its screen handed back once that's done.
        later = bool(self.phone and self.phone.platform == "ios" and self.phone_app and self._iphone_home())
        if self.phone and self.phone.platform == "ios" and not self.gave_screen and not later:
            app.screen_back(self)
        others = {w.phone.id for w in app.handset_windows() if w.phone and w is not self}
        if app.clipboard:
            app.clipboard.release(others)
        if app.sound:
            app.sound.release(others)
        if app.screen_off:
            app.screen_off.release(others)
        self.watch.stop()
        self._close_phone()
        return False

    # ---- The mouse --------------------------------------------------------------------------

    def _fit(self) -> Fit | None:
        if not self.device:
            return None
        return Fit((self.screen.get_width(), self.screen.get_height()), self.device.shown)

    def _drag_begin(self, gesture, x, y):
        self._stop_gliding()  # a press stops the page, as a finger on the glass does
        if self.wheel_timer:  # a click while the wheel's finger is down: that finger goes first
            GLib.source_remove(self.wheel_timer)
        self._wheel_lift()
        if self.gave_screen:
            self.take_screen()  # a click on an iPhone's window showing its last picture
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
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
        if self.own_screen():
            point = raw  # an app's own screen has no edges to swipe in from
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

    def _check_hidden(self):
        """The note on a locked phone's black PIN pad: shown while the phone is awake and locked
        and its picture is black."""
        device = self.device
        hidden = bool(device and getattr(device, "locked", False) and device.awake
                      and self.stack.get_visible_child_name() == "screen" and self.picture.is_dark())
        if hidden and self.own_screen() and self.phone:
            # An app's own screen is black while the phone is locked; its PIN goes to the phone.
            self.hidden.set_label(f"{self.phone.name} is locked.\nUnlock it to use {self.phone_app.name} here.")
        if hidden != self.hidden.get_visible():
            debug("PIN pad note", "shown" if hidden else "hidden")
            self.hidden.set_visible(hidden)
        return GLib.SOURCE_CONTINUE

    def _snapped(self, fit: Fit, x: int, y: int) -> tuple[int, int]:
        """A point of this press's path, moved as its start was onto the edge, kept on the screen."""
        (sx, sy), (w, h) = self.snap, fit.screen
        return min(max(x + sx, 0), w - 1), min(max(y + sy, 0), h - 1)

    def _wheel(self, controller, dx, dy):
        if not self.device:
            return False
        unit = controller.get_unit()
        if unit == Gdk.ScrollUnit.WHEEL and controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self._wheel_pinch(dy)  # Ctrl and the wheel: a pinch, as zooming does everywhere else
            return True
        # A mouse wheel turned with Shift held scrolls sideways, as everywhere else.
        if unit == Gdk.ScrollUnit.WHEEL and dx == 0 and controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
            dx, dy = dy, 0.0
        self._stop_throw()  # fingers on the touchpad again stop a thrown page, as on glass
        if unit == Gdk.ScrollUnit.WHEEL and self._wheel_glide(dx, dy):
            return True
        self.pad_scrolled = unit == Gdk.ScrollUnit.SURFACE and self._scroll_by(dx, dy)
        if self.pad_scrolled:
            now = controller.get_current_event_time()
            self.pad_history = [entry for entry in self.pad_history if now - entry[0] <= FLICK_MS] + [(now, dx, dy)]
            return True
        if unit == Gdk.ScrollUnit.SURFACE and self.device.live_touch:
            self._finger_scroll(dx, dy)
            return True
        if unit == Gdk.ScrollUnit.WHEEL and self.device.live_touch:
            self._wheel_notch(dx, dy)
            return True
        # A wheel's notch is 1; a touchpad (with no live finger) says pixels.
        per = 1 if unit == Gdk.ScrollUnit.WHEEL else 40
        self.scrolled += dy / per
        self.scrolled_x += dx / per
        if not self.scroll_timer:
            self.scroll_timer = GLib.timeout_add(SCROLL_AFTER_MS, self._scroll_now)
        return True

    # ---- Scrolling as a mouse wheel does (Android, through scrcpy's server) ---------------------

    def _scroll_point(self, fit: Fit) -> tuple[int, int]:
        """The phone's pixel under the pointer, or the nearest (the pointer on the bezel)."""
        (ox, oy), scale = fit.origin, fit.scale or 1.0
        (sw, sh), (x, y) = fit.screen, self.pointer
        return min(max(int((x - ox) / scale), 0), sw - 1), min(max(int((y - oy) / scale), 0), sh - 1)

    def _scroll_by(self, right: float, down: float, pixels_of_window: bool = True) -> bool:
        """What's under the pointer scrolled by the window's pixels (or the phone's), with no
        finger on the glass; False when the phone can't be scrolled so (an iPhone, say)."""
        fit = self._fit()
        scroll = getattr(self.device, "scroll", None)
        if not fit or not scroll:
            return False
        scale = (fit.scale or 1.0) if pixels_of_window else 1.0
        return scroll(*self._scroll_point(fit), right / scale, down / scale)

    def _wheel_glide(self, right: float, down: float) -> bool:
        """A mouse wheel's notches, as far as SCROLL_PER_NOTCH takes them, glided frame by frame."""
        if not self._scroll_by(0, 0):
            return False
        sw, sh = self.device.shown
        self.wheel_left[0] += right * SCROLL_PER_NOTCH * sh
        self.wheel_left[1] += down * SCROLL_PER_NOTCH * sh
        self._moving()
        return True

    def _moving(self):
        """The page glides or is thrown: stepped with the display's own frames (the screen's frame
        clock), each step as far as the time since the last, so it moves as evenly as they come."""
        if not self.motion_tick:
            self.motion_at = None
            self.motion_tick = self.screen.add_tick_callback(self._motion_frame)

    def _motion_frame(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        elapsed = min(now - self.motion_at, 0.1) if self.motion_at is not None else 1 / 60
        self.motion_at = now
        going = self._wheel_glide_step(elapsed)
        going = self._throw_step(elapsed) or going
        if not going:
            self.motion_tick = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _wheel_glide_step(self, elapsed: float) -> bool:
        """A frame of a wheel's glide: its share of the way left (all of it under a pixel). Whether
        there's more."""
        if not any(self.wheel_left):
            return False
        share = 1 - math.exp(-elapsed / WHEEL_GLIDE_S)
        steps = [left if abs(left) < 1 else left * share for left in self.wheel_left]
        self.wheel_left = [left - step for left, step in zip(self.wheel_left, steps)]
        if not self._scroll_by(*steps, pixels_of_window=False):
            self.wheel_left = [0.0, 0.0]
            return False
        return any(self.wheel_left)

    def _flicked(self, controller, _speed_x: float, _speed_y: float):
        """The touchpad's fingers lifted moving: the page, scrolled as a wheel does, is thrown on
        at the speed they had (with a finger, the phone throws it itself). That speed's measured
        here, from their last moments, as GTK measures its own: what this signal says of it isn't
        taken, its unit being in doubt (pixels a millisecond, its documents say)."""
        fit = self._fit()
        if not self.pad_scrolled or not fit or not self.device:
            return
        self.pad_scrolled = False
        now = controller.get_current_event_time() if controller else (self.pad_history[-1][0] if self.pad_history else 0)
        speed = pad_speed(self.pad_history, now)
        self.pad_history = []
        if not speed:
            return
        scale = fit.scale or 1.0
        self.thrown = [min(max(each, -THROW_FASTEST), THROW_FASTEST) / scale for each in speed]
        self._moving()

    def _throw_step(self, elapsed: float) -> bool:
        """A frame of a thrown page: the way gone as its speed fell over the frame. Whether it
        goes on."""
        fit = self._fit()
        if self.thrown is None or not fit:
            self.thrown = None
            return False
        fade = math.exp(-elapsed / GLIDE_FADE_S)
        way = [speed * GLIDE_FADE_S * (1 - fade) for speed in self.thrown]
        self.thrown = [speed * fade for speed in self.thrown]
        slow = math.hypot(*self.thrown) * (fit.scale or 1.0) < GLIDE_STOP
        if not self._scroll_by(*way, pixels_of_window=False) or slow:
            self.thrown = None
            return False
        return True

    def _stop_throw(self):
        self.thrown = None  # its next frame ends it

    def _stop_gliding(self):
        self.thrown = None
        self.wheel_left = [0.0, 0.0]

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

    def _wheel_notch(self, right: float, down: float):
        """A mouse wheel's notch: the finger held for the wheel goes down where the pointer is (if
        it isn't down already), and is to move a step against the scroll."""
        fit = self._fit()
        if not fit:
            return
        sw, sh = self.device.shown
        if self.wheel_at is None:
            start = fit.to_screen(*self.pointer)
            if start is None:
                return
            x, y = inside((sw, sh), *start)
            self.wheel_at = self.wheel_to = (float(x), float(y))
            self.device.press(x, y)
        x, y = self.wheel_to
        self.wheel_to = (x - right * SCROLL_PER_NOTCH * sw, y - down * SCROLL_PER_NOTCH * sh)
        self._wheel_go(WHEEL_FRAME_MS)

    def _wheel_go(self, after: int):
        if self.wheel_timer:
            GLib.source_remove(self.wheel_timer)
        self.wheel_timer = GLib.timeout_add(after, self._wheel_step)

    def _wheel_step(self):
        """A frame of the wheel's finger: a part of the way to where the notches take it, kept off
        the screen's edges; there, still, then up, and down again on the far side for the rest;
        arrived, still, then up."""
        self.wheel_timer = 0
        if self.wheel_at is None or not self.device:
            return GLib.SOURCE_REMOVE
        (x, y), (tx, ty) = self.wheel_at, self.wheel_to
        left, top, right, bottom = safe_box(self.device.shown)
        cx, cy = min(max(tx, left), right), min(max(ty, top), bottom)
        nx, ny = x + (cx - x) * 0.35, y + (cy - y) * 0.35
        if abs(cx - nx) < 1.5 and abs(cy - ny) < 1.5:
            nx, ny = cx, cy
        if (round(nx), round(ny)) != (round(x), round(y)):
            self.wheel_at = (nx, ny)
            self.device.drag(round(nx), round(ny))
            self._wheel_go(WHEEL_FRAME_MS)
        elif (cx, cy) != (tx, ty):
            self.wheel_timer = GLib.timeout_add(WHEEL_STILL_MS, self._wheel_again)  # the rest from further in
        else:
            self.wheel_timer = GLib.timeout_add(WHEEL_STILL_MS, self._wheel_lift)
        return GLib.SOURCE_REMOVE

    def _wheel_again(self):
        """At the edge, still: up, and down again on the far side, for what's left of the way."""
        self.wheel_timer = 0
        if self.wheel_at is None or not self.device:
            return GLib.SOURCE_REMOVE
        (x, y), (tx, ty) = self.wheel_at, self.wheel_to
        left, top, right, bottom = safe_box(self.device.shown)
        sx = right if tx < left else left if tx > right else x
        sy = bottom if ty < top else top if ty > bottom else y
        self.device.release()
        self.wheel_at, self.wheel_to = (float(sx), float(sy)), (sx + tx - x, sy + ty - y)
        self.device.press(round(sx), round(sy))
        self._wheel_go(WHEEL_FRAME_MS)
        return GLib.SOURCE_REMOVE

    # ---- Pinching ------------------------------------------------------------------------------

    def _pinch_fingers(self, scale: float) -> list[tuple[int, int]]:
        """The pinch's two fingers, either side of its middle, kept on the screen."""
        sw, sh = self.device.shown
        half = PINCH_START * min(sw, sh) * scale / 2
        cx, cy = self.pinch_at
        return [(round(min(max(cx - half, 0), sw - 1)), cy), (round(min(max(cx + half, 0), sw - 1)), cy)]

    def _pinch_start(self, x: float, y: float) -> bool:
        """Two fingers down on the phone either side of the window's point (x, y)."""
        fit = self._fit()
        device = self.device
        if not fit or not device or not getattr(device, "live_touch", False) or not hasattr(device, "fingers"):
            return False
        at = fit.to_screen(x, y)
        if at is None:
            return False
        self._wheel_lift()  # one gesture at a time
        self.pinch_at, self.pinch_scale = at, 1.0
        device.fingers(self._pinch_fingers(1.0))
        return True

    def _pinch_begin(self, gesture, _sequence):
        found, x, y = gesture.get_bounding_box_center()
        if not found or not self._pinch_start(x, y):
            gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _pinch(self, scale: float):
        if self.pinch_at is not None and self.device:
            self.pinch_scale = min(max(scale, 0.2), 5.0)
            self.device.fingers(self._pinch_fingers(self.pinch_scale))

    def _pinch_end(self):
        if self.pinch_timer:
            GLib.source_remove(self.pinch_timer)
            self.pinch_timer = 0
        if self.pinch_at is not None:
            self.pinch_at = None
            if self.device:
                self.device.fingers([])
        return GLib.SOURCE_REMOVE

    def _wheel_pinch(self, dy: float):
        """Ctrl and a notch of the wheel: the fingers (put down where the pointer is, the first
        time) spread or close a step, a frame at a time, and lift once the wheel rests."""
        if self.pinch_at is None:
            if not self._pinch_start(*self.pointer):
                return
            self.pinch_to = 1.0
        self.pinch_to = min(max(self.pinch_to * PINCH_PER_NOTCH ** -dy, 0.2), 5.0)
        if self.pinch_timer:
            GLib.source_remove(self.pinch_timer)
        self.pinch_timer = GLib.timeout_add(WHEEL_FRAME_MS, self._pinch_step)

    def _pinch_step(self):
        self.pinch_timer = 0
        if self.pinch_at is None or not self.device:
            return GLib.SOURCE_REMOVE
        step = (self.pinch_to - self.pinch_scale) * 0.35
        if abs(self.pinch_to - self.pinch_scale) < 0.01:
            self.pinch_timer = GLib.timeout_add(PINCH_REST_MS, self._pinch_end)  # still, then up
            return GLib.SOURCE_REMOVE
        self.pinch_scale += step
        self.device.fingers(self._pinch_fingers(self.pinch_scale))
        self.pinch_timer = GLib.timeout_add(WHEEL_FRAME_MS, self._pinch_step)
        return GLib.SOURCE_REMOVE

    def _wheel_lift(self):
        self.wheel_timer = 0
        if self.wheel_at is not None:
            self.wheel_at = None
            if self.device:
                self.device.release()
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
        if state & Gdk.ModifierType.CONTROL_MASK and not state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            letter = (Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or "").lower()
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if letter in EDIT_KEYS and (not shift or letter in EDIT_KEYS_SHIFTED):
                self._edit(letter, shift)
                return True
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

    def _edit(self, letter: str, shift: bool):
        """Select all, copy, cut, paste, undo and redo on the phone, as its keyboard's own shortcuts
        (Command on an iPhone): what's copied there comes onto this computer's clipboard, and a
        paste puts this computer's clipboard on the phone first."""
        device = self.device
        if not hasattr(device, "shortcut"):
            return
        if not device.awake:
            device.wake()
            return
        if self.type_timer:  # what was typed goes first
            GLib.source_remove(self.type_timer)
            self._type_now()
        clipboard = self.get_application().clipboard
        if letter == "v" and clipboard and self.phone:
            clipboard.paste_then(self.phone.id, lambda: self.device is device and device.shortcut(letter, shift))
        else:
            device.shortcut(letter, shift)

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
