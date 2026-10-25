"""Handset: a phone's screen in a window on the desk, through Ada's phone service. One window
per phone: two phones, two windows, side by side.

  handset                 open Handset: its main window, the phones a click away in it
  handset --phone NAME    show that phone (its name or id), in a window of its own
  handset --phone ID --app APP
                          one of its apps in a window of its own (what a pinned app runs)
  handset --new-window    another window, for a phone no window shows yet
  handset --phones        Handset's phones (adding one, each one's page) and its settings
  handset --off           close every window and quit
  handset --sync          keep connected phones' apps among the desk's apps (from sign-in)
  handset --forget-apps   take every phone app off the desk and out of the dock
  handset --pair-iphone   show this computer to iPhones for a few minutes, to pair one for its
                          notifications (Settings › Bluetooth on the iPhone)

While Handset runs it keeps a file, $XDG_RUNTIME_DIR/handset.on, so others can show whether
it's on (a switch in the desktop's Control Center, say) without asking it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from . import APP_ID  # noqa: E402


def on_file() -> str:
    """Where Handset says it's on: the runtime folder always exists, so a watcher can watch it
    before Handset first runs."""
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/ada-{os.getuid()}", "handset.on")


def say_on(on: bool):
    path = on_file()
    try:
        if on:
            with open(path, "w") as file:
                file.write(f"{os.getpid()}\n")
        elif os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        from .window import load_settings, migrated

        self.settings = migrated(load_settings())
        self.connect("startup", self._started)
        self.clipboard = None  # the clipboard shared with the phones (clipboard.py), once started
        self.sound = None  # each phone's sound, here or on it (sound.py)
        self.task_watches = {}  # adb serial → popout.TaskWatch, while its apps have windows
        self.connect("shutdown", self._shutdown)

    def _shutdown(self, _app):
        say_on(False)
        if self.clipboard:
            self.clipboard.stop()
        if self.sound:
            self.sound.stop()
        for watch in self.task_watches.values():
            watch.stop()

    def _started(self, _app):
        from .appsync import start_if_needed
        from .clipboard import ClipboardSync
        from .scope import move_to_own_scope

        move_to_own_scope()  # counted as Handset, whichever phone app's entry started it

        self.clipboard = ClipboardSync(self)
        from .sound import SoundSwitch

        self.sound = SoundSwitch(self)
        start_if_needed()  # the phones' apps among the desk's, whether or not it ran at sign-in
        action = Gio.SimpleAction.new("new-window", None)
        action.connect("activate", lambda *_a: self.open_window(new=True))
        self.add_action(action)
        self.set_accels_for_action("app.new-window", ["<Control><Shift>n"])
        action = Gio.SimpleAction.new("phones", None)
        action.connect("activate", lambda *_a: self.show_phones())
        self.add_action(action)
        self.set_accels_for_action("app.phones", ["<Control>comma"])

    # ---- Which window shows which phone -----------------------------------------------------

    def handset_windows(self, apps: bool = True) -> list:
        """Handset's windows: the phones', and (`apps`) the ones showing one app each."""
        from .window import Window

        return [w for w in self.get_windows() if isinstance(w, Window) and (apps or not w.phone_app)]

    def shown(self, besides=None) -> set[str]:
        """The phones other windows show (their ids)."""
        return {w.phone.id for w in self.handset_windows(apps=False) if w is not besides and w.phone}

    def window_for(self, wanted: str):
        """The window that shows the phone `wanted` (its id or name), if any."""
        for window in self.handset_windows(apps=False):
            if window.phone and wanted in (window.phone.id, window.phone.name):
                return window
        return None

    def open_app(self, phone_id: str, app: str, phone_app=None):
        """One app of a phone in a window of its own (the one showing it comes forward, the app
        in front again). An iPhone has one screen: the app's window takes it over from the
        others showing that iPhone (screen_to)."""
        from .apps import PhoneApp, find, write_entry
        from .window import Window

        for window in self.handset_windows():
            if window.phone_app and (window.phone_app.phone_id, window.phone_app.app) == (phone_id, app):
                window.present()
                if window.gave_screen:
                    window.take_screen()
                else:
                    window.open_app(app)
                return window
        phone_app = phone_app or find(phone_id, app) or PhoneApp(phone_id, phone_id, "", app, app)
        if phone_app.platform:
            # An entry of its own, so its window counts as that app (in the dock, say).
            write_entry(phone_app, None, replace=False)
        window = Window(self, phone_app=phone_app)
        window.present()
        say_on(True)
        return window

    # ---- Apps an app opens (popout.py) ------------------------------------------------------

    def app_display(self, window, serial: str, display: int):
        """An Android app's window has its display: another app coming on it gets a window."""
        from .android import adb_command
        from .popout import TaskWatch

        if self.settings.get("pop_out_apps", True) is False:
            return
        watch = self.task_watches.get(serial)
        if not watch:
            watch = TaskWatch(serial, lambda shown, task: self._app_opened(serial, shown, task),
                              lambda shown: self._app_left(serial, shown), adb_command)
            self.task_watches[serial] = watch
        window.display = (serial, display)
        watch.watch(display, window.phone_app.app)

    def app_display_gone(self, window):
        serial, display = window.display
        window.display = None
        if serial in self.task_watches:
            self.task_watches[serial].forget(display)

    def _app_opened(self, serial: str, display: int, task):
        """Another app came on top in an app's window: to its own window (the one it has, or a
        new one)."""
        from .android import shell
        from .apps import PhoneApp, find
        from .window import Window

        opener = next((w for w in self.handset_windows() if w.display == (serial, display)), None)
        if not opener or not opener.phone:
            return
        phone = opener.phone
        has = next((w for w in self.handset_windows() if w.phone_app and w.phone and w.phone.id == phone.id
                    and w.phone_app.app == task.package), None)
        if has:
            if has.display:
                def move(to: int):
                    try:
                        shell(serial, f"am display move-stack {task.id} {to}", timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        pass
                threading.Thread(target=move, args=(has.display[1],), daemon=True).start()
            has.present()
            return
        phone_app = find(phone.id, task.package) or PhoneApp(phone.id, phone.name, "", task.package, task.package)
        window = Window(self, phone_app=phone_app, task=task.id)
        window.present()
        say_on(True)

    def _app_left(self, serial: str, display: int):
        """An app's window that another app opened has nothing left on it: it closes, as the app did."""
        window = next((w for w in self.handset_windows() if w.display == (serial, display)), None)
        if window and window.parted:
            window.close()

    def choose_phone(self, phone_id: str, connected: bool | None = None, wifi: bool | None = None):
        """A phone switched on or off in Handset, or kept to its cable or let on Wi-Fi: saved, and
        every window here (and the sync, through the settings file) goes by it at once."""
        from .service import refresh_all
        from .window import save_settings

        for key, wanted in (("disconnected", connected), ("cable_only", wifi)):
            if wanted is None:
                continue
            listed = [p for p in self.settings.get(key) or [] if p != phone_id]
            self.settings[key] = listed if wanted else listed + [phone_id]
        save_settings(self.settings)
        refresh_all()

    def is_connected(self, phone_id: str) -> bool:
        return phone_id not in (self.settings.get("disconnected") or [])

    def over_wifi(self, phone_id: str) -> bool:
        return phone_id not in (self.settings.get("cable_only") or [])

    def show_phones(self):
        """Handset's own window (phones.py): the one open comes forward."""
        from .phones import PhonesWindow

        window = next((w for w in self.get_windows() if isinstance(w, PhonesWindow)), None) or PhonesWindow(self)
        window.present()
        say_on(True)
        return window

    def screen_to(self, window) -> bool:
        """An iPhone's one screen, to `window`: the other windows showing that iPhone give it up,
        and the one with the link to it hands that over. Whether a link came (if not, `window`
        makes its own)."""
        if not window.phone or window.phone.platform != "ios":
            return False
        came = False
        for other in self.handset_windows():
            if other is not window and other.phone and other.phone.id == window.phone.id and not other.gave_screen:
                came = other.give_screen(window) or came
        return came

    def screen_back(self, closing):
        """An iPhone's screen, from a window going, to a window that gave it up (the phone's own
        window first)."""
        waiting = [w for w in self.handset_windows() if w is not closing and w.gave_screen and w.phone
                   and closing.phone and w.phone.id == closing.phone.id]
        if waiting:
            min(waiting, key=lambda w: bool(w.phone_app)).take_screen()

    def open_window(self, wanted: str | None = None, new: bool = False):
        """A window for `wanted`, or (new) for a phone no window shows yet; the one already
        showing it comes forward instead of a second."""
        from .window import Window, choose

        if wanted and (window := self.window_for(wanted)):
            window.present()
            if window.gave_screen:
                window.take_screen()
            return window
        if new and not wanted:
            known = next((w.phones for w in self.handset_windows() if w.phones), [])
            if known and not choose(known, None, taken=self.shown(), free_only=True):
                active = self.get_active_window()
                if active:
                    active.say("Every phone already has a window")
                    active.present()
                return active
        window = Window(self, wanted)
        window.present()
        say_on(True)
        return window

    # ---- Starting -----------------------------------------------------------------------------

    def do_command_line(self, command_line):
        try:
            return self._command_line(command_line.get_arguments()[1:])
        finally:
            # A traceback a signal handler printed meanwhile (a window being made, say) keeps its
            # frames, this call's among them, and so the command line: the process that sent it
            # would wait for it for good.
            sys.last_type = sys.last_value = sys.last_traceback = None
            if hasattr(sys, "last_exc"):
                sys.last_exc = None

    def _command_line(self, args: list[str]) -> int:
        if "--phone" in args:
            index = args.index("--phone")
            if index + 1 < len(args):
                app = args.index("--app") if "--app" in args else -1
                if 0 <= app < len(args) - 1:
                    self.open_app(args[index + 1], args[app + 1])
                else:
                    self.open_window(args[index + 1])
                return 0
        if "--new-window" in args:
            self.open_window(new=True)
            return 0
        if "--phones" in args:
            self.show_phones()
            return 0
        if "--off" in args:
            self.turn_off()
            return 0
        self.activate()
        return 0

    def turn_off(self):
        """Every window closes, and Handset quits. With none open (it had stopped without saying
        so), only the word that it's on goes."""
        from .phones import PhonesWindow

        for window in [w for w in self.get_windows() if isinstance(w, PhonesWindow)]:
            window.close()
        windows = self.handset_windows()
        for window in windows:
            window.close()
        if not windows:
            say_on(False)

    def do_activate(self):
        """Opening Handset (its menu entry, its icon): its main window, in front, the phones a
        click away in it (each one's Show)."""
        self.show_phones()


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    if "--sync" in argv[1:]:
        from .appsync import main as sync

        return sync()
    if argv[1:2] == ["--sync-icons"] and len(argv) == 4:
        from .appsync import icons_worker

        return icons_worker(argv[2], argv[3])
    if "--pair-iphone" in argv[1:]:
        from .appsync import ask_to_pair_iphone, start_if_needed

        start_if_needed()
        ask_to_pair_iphone()
        print("Open Settings › Bluetooth on the iPhone and pick this computer from Other Devices.")
        return 0
    if "--forget-apps" in argv[1:]:
        from .appsync import forget_all

        forget_all()
        return 0
    return App().run(argv)


if __name__ == "__main__":
    sys.exit(main())
