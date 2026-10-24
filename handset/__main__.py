"""Handset: a phone's screen in a window on the desk, through Ada's phone service. One window
per phone: two phones, two windows, side by side.

  handset                 open Handset: the windows open last time (or bring them forward)
  handset --phone NAME    show that phone (its name or id), in a window of its own
  handset --new-window    another window, for a phone no window shows yet
  handset --off           close every window and quit (the same windows open next time)

While Handset runs it keeps a file, $XDG_RUNTIME_DIR/handset.on, so others can show whether
it's on (a switch in the desktop's Control Center, say) without asking it.
"""

from __future__ import annotations

import os
import sys

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
        self.turning_off = False
        self.connect("startup", self._started)
        self.connect("shutdown", lambda _app: say_on(False))

    def _started(self, _app):
        action = Gio.SimpleAction.new("new-window", None)
        action.connect("activate", lambda *_a: self.open_window(new=True))
        self.add_action(action)
        self.set_accels_for_action("app.new-window", ["<Control><Shift>n"])

    # ---- Which window shows which phone -----------------------------------------------------

    def handset_windows(self) -> list:
        from .window import Window

        return [w for w in self.get_windows() if isinstance(w, Window)]

    def shown(self, besides=None) -> set[str]:
        """The phones other windows show (their ids)."""
        return {w.phone.id for w in self.handset_windows() if w is not besides and w.phone}

    def window_for(self, wanted: str):
        """The window that shows the phone `wanted` (its id or name), if any."""
        for window in self.handset_windows():
            if window.phone and wanted in (window.phone.id, window.phone.name):
                return window
        return None

    def open_window(self, wanted: str | None = None, new: bool = False):
        """A window for `wanted`, or (new) for a phone no window shows yet; the one already
        showing it comes forward instead of a second."""
        from .window import Window, choose

        if wanted and (window := self.window_for(wanted)):
            window.present()
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

    def remember_windows(self, closing=None):
        """Which phones have windows, for next time. The last window closing is Handset
        quitting: its phone stays, so it opens again; and so do all of them when it's turned
        off."""
        from .window import save_settings

        if self.turning_off:
            return
        windows = [w for w in self.handset_windows() if w is not closing or len(self.handset_windows()) == 1]
        self.settings["windows"] = [w.phone.id for w in windows if w.phone]
        save_settings(self.settings)

    # ---- Starting -----------------------------------------------------------------------------

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        if "--phone" in args:
            index = args.index("--phone")
            if index + 1 < len(args):
                self.open_window(args[index + 1])
                return 0
        if "--new-window" in args:
            self.open_window(new=True)
            return 0
        if "--off" in args:
            self.turn_off()
            return 0
        self.activate()
        return 0

    def turn_off(self):
        """Every window closes, remembered as they are, and Handset quits. With none open (it had
        stopped without saying so), only the word that it's on goes."""
        self.remember_windows()
        self.turning_off = True
        windows = self.handset_windows()
        for window in windows:
            window.close()
        if not windows:
            say_on(False)

    def do_activate(self):
        windows = self.handset_windows()
        if windows:
            (self.get_active_window() or windows[0]).present()
            return
        for phone_id in self.settings.get("windows") or [None]:
            self.open_window(phone_id)


def main(argv=None) -> int:
    return App().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
