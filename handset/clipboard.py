"""The clipboard, shared with the phones Handset's windows show: copy on the computer, then click
into a phone's window, and it's on the phone; copy on the phone, and it's on the computer.

The desktop lets only the window in front read or set the clipboard, so it goes with Handset's
windows: whenever one of them comes to the front the computer's text goes to its phones (if it's
new), and what was copied on a phone meanwhile comes onto the computer's clipboard. Copied on a
phone while a Handset window is in front, it's there at once. Text only. Each phone has one link
while a window shows it: Android's through scrcpy's server (android.ClipboardLink), an iPhone's
through Handset's helper (iphone.ClipboardLink).
"""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject  # noqa: E402

from .log import debug  # noqa: E402


class ClipboardSync:
    def __init__(self, app):
        self.app = app
        self.links: dict[str, object] = {}  # phone id → its link
        self.pending: str | None = None  # copied on a phone while no Handset window was in front
        self.sent: str | None = None  # the computer's text the phones have
        display = Gdk.Display.get_default()
        self.clipboard = display.get_clipboard() if display else None
        if self.clipboard:
            self.clipboard.connect("changed", self._desk_changed)

    def watch(self, window):
        """A Handset window: its coming to the front is when the clipboard goes both ways."""
        window.connect("notify::is-active", self._focus)

    def attach(self, phone_id: str, make_link):
        """A phone a window shows: linked, once (`make_link(on_text)` gives its link)."""
        if phone_id in self.links or not self.clipboard:
            return
        link = make_link(lambda text, phone=phone_id: self._phone_copied(phone, text))
        self.links[phone_id] = link
        link.start()
        debug("clipboard linked to", phone_id)

    def release(self, showing: set[str]):
        """The phones no window shows any more: unlinked."""
        for phone_id in [p for p in self.links if p not in showing]:
            self.links.pop(phone_id).stop()
            debug("clipboard unlinked from", phone_id)

    def stop(self):
        self.release(set())

    def _front(self) -> bool:
        return any(window.is_active() for window in self.app.handset_windows())

    # ---- Phone → computer -------------------------------------------------------------------

    def _phone_copied(self, phone_id: str, text: str):
        debug("copied on", phone_id, f"({len(text)} characters)")
        if self._front():
            self._to_desk(text)
        else:
            self.pending = text  # onto the computer's clipboard when a Handset window comes forward
        # The other phones have it too.
        for other, link in self.links.items():
            if other != phone_id:
                link.set(text)
        return GLib.SOURCE_REMOVE

    def _to_desk(self, text: str):
        value = GObject.Value(GObject.TYPE_STRING, text)
        self.sent = text  # it's the phones' already: not sent back
        self.pending = None
        self.clipboard.set(value)

    # ---- Computer → phone -------------------------------------------------------------------

    def _focus(self, window, _param):
        if not window.is_active() or not self.clipboard:
            return
        if self.pending is not None:
            self._to_desk(self.pending)
        else:
            self._read_desk()

    def _desk_changed(self, _clipboard):
        if self._front() and not self.clipboard.is_local():
            self._read_desk()

    def _read_desk(self):
        if not self.links:
            return
        self.clipboard.read_text_async(None, self._desk_read)

    def _desk_read(self, clipboard, result):
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            return  # not text (a picture, files), or nothing
        if text and text != self.sent:
            self.sent = text
            debug("the computer's clipboard to the phones", f"({len(text)} characters)")
            for link in self.links.values():
                link.set(text)
