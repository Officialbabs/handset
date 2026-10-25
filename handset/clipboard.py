"""The clipboard, shared with the phones Handset's windows show: copy on the computer, then click
into a phone's window, and it's on the phone; copy on the phone, and it's on the computer.

The desktop lets only the window in front read or set the clipboard, so it goes with Handset's
windows: whenever one of them comes to the front the computer's text or image goes to its phones
(if it's new). What's copied on a phone is on the computer's clipboard at once, whatever's in
front, where the desktop lets a clipboard manager set it (Wayland's data-control, as wl-copy does);
elsewhere at once if a Handset window is in front, else as soon as one comes forward. Text and images: an
image goes onto the phone's clipboard itself, never into its gallery or files, to paste in any
app that takes one (images come back from an iPhone; from Android, text only). Each phone has
one link while a window shows it: Android's through scrcpy's server and Handset's phone-side jar
(android.ClipboardLink), an iPhone's through Handset's helper (iphone.ClipboardLink).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject  # noqa: E402

from .log import debug  # noqa: E402

# Images as they're copied, in the order they're taken (as they are: no converting).
IMAGE_TYPES = ("image/png", "image/jpeg")
MOST_IMAGE = 20 * 1024 * 1024  # bytes: a bigger one goes as a smaller PNG


class ClipboardSync:
    def __init__(self, app):
        self.app = app
        self.links: dict[str, object] = {}  # phone id → its link
        self.pending: str | None = None  # copied on a phone while no Handset window was in front
        self.pending_image: tuple[str, str] | None = None  # (file, type), the same for an image
        self.sent: str | None = None  # the computer's text the phones have
        self.sent_image: str | None = None  # the image they have (its digest)
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
        link = make_link(lambda text, phone=phone_id: self._phone_copied(phone, text),
                         lambda path, mime, phone=phone_id: self._phone_copied_image(phone, path, mime))
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

    def paste_then(self, phone_id: str, paste):
        """This computer's clipboard on the phone, then `paste()` (Ctrl+V in its window): the
        paste never goes before what it pastes."""
        link = self.links.get(phone_id)
        if not link or not self.clipboard:
            paste()
            return

        def read(clipboard, result):
            try:
                text = clipboard.read_text_finish(result)
            except GLib.Error:
                text = None
            if text:
                self.sent, self.sent_image = text, None
                link.set(text, then=paste)
            else:
                self._read_image(lambda image: self._image_then(link, image, paste))  # or nothing: as it is

        self.clipboard.read_text_async(None, read)

    def _image_then(self, link, image: tuple[str, str] | None, paste):
        if image and hasattr(link, "set_image"):
            self.sent_image, self.sent = image_digest(image[0]), None
            link.set_image(*image, then=paste)
        else:
            paste()

    def _front(self) -> bool:
        return any(window.is_active() for window in self.app.handset_windows())

    # ---- Phone → computer -------------------------------------------------------------------

    def _phone_copied(self, phone_id: str, text: str):
        debug("copied on", phone_id, f"({len(text)} characters)")
        if wl_copy(text.encode(), "text/plain;charset=utf-8"):
            self.sent, self.sent_image = text, None  # the phones' already: not sent back
            self.pending = self.pending_image = None
        elif self._front():
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
        self.sent, self.sent_image = text, None  # it's the phones' already: not sent back
        self.pending = self.pending_image = None
        self.clipboard.set(value)

    def _phone_copied_image(self, phone_id: str, path: str, mime: str):
        debug("an image copied on", phone_id)
        if wl_copy(read_file(path), mime):
            self.sent_image, self.sent = image_digest(path), None
            self.pending = self.pending_image = None
        elif self._front():
            self._image_to_desk(path, mime)
        else:
            self.pending_image, self.pending = (path, mime), None
        for other, link in self.links.items():
            if other != phone_id and hasattr(link, "set_image"):
                link.set_image(path, mime)
        return GLib.SOURCE_REMOVE

    def _image_to_desk(self, path: str, mime: str):
        try:
            texture = Gdk.Texture.new_from_filename(path)
        except GLib.Error as error:
            debug("the phone's image couldn't be read:", error.message)
            return
        self.sent_image, self.sent = image_digest(path), None
        self.pending = self.pending_image = None
        self.clipboard.set_texture(texture)

    # ---- Computer → phone -------------------------------------------------------------------

    def _focus(self, window, _param):
        if not window.is_active() or not self.clipboard:
            return
        if self.pending is not None:
            self._to_desk(self.pending)
        elif self.pending_image is not None:
            self._image_to_desk(*self.pending_image)
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
            text = None  # not text: an image, maybe (files, or nothing, stay here)
        if text is None:
            self._read_image(self._desk_image)
        elif text and text != self.sent:
            self.sent, self.sent_image = text, None
            debug("the computer's clipboard to the phones", f"({len(text)} characters)")
            for link in self.links.values():
                link.set(text)

    def _desk_image(self, image: tuple[str, str] | None):
        if not image or image_digest(image[0]) == self.sent_image:
            return
        self.sent_image, self.sent = image_digest(image[0]), None
        debug("the computer's image to the phones")
        for link in self.links.values():
            if hasattr(link, "set_image"):
                link.set_image(*image)

    # ---- An image on the computer's clipboard ---------------------------------------------------

    def _read_image(self, done):
        """The computer's clipboard image, as it was copied (PNG or JPEG) or else as a PNG, in a
        file only this person can read: done((file, type)), or done(None) when there's none."""
        formats = self.clipboard.get_formats()
        offered = [mime for mime in IMAGE_TYPES if formats.contain_mime_type(mime)]
        if offered:
            self.clipboard.read_async(offered, GLib.PRIORITY_DEFAULT, None, self._image_stream, done)
        elif formats.contain_gtype(Gdk.Texture.__gtype__):
            self.clipboard.read_texture_async(None, self._image_texture, done)
        else:
            done(None)

    def _image_stream(self, clipboard, result, done):
        try:
            stream, mime = clipboard.read_finish(result)
        except GLib.Error:
            done(None)
            return
        into = Gio.MemoryOutputStream.new_resizable()

        def read(_into, spliced):
            try:
                into.splice_finish(spliced)
            except GLib.Error:
                done(None)
                return
            data = into.steal_as_bytes().get_data()
            if len(data) > MOST_IMAGE:
                clipboard.read_texture_async(None, self._image_texture, done)  # smaller, as a PNG
                return
            done((keep_image(data, mime), mime))

        into.splice_async(stream, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
                          GLib.PRIORITY_DEFAULT, None, read)

    def _image_texture(self, clipboard, result, done):
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error:
            done(None)
            return
        if not texture:
            done(None)
            return
        done((keep_image(texture.save_to_png_bytes().get_data(), "image/png"), "image/png"))


def wl_copy(data: bytes | None, mime: str) -> bool:
    """The computer's clipboard set from outside any window, as a clipboard manager does
    (wl-copy, through the compositor's data-control): no Handset window need be in front. False
    where it can't be (not Wayland, no wl-copy), and the window's own way is left."""
    if not data or not os.environ.get("WAYLAND_DISPLAY") or not shutil.which("wl-copy"):
        return False
    try:
        # wl-copy keeps offering it from a process of its own, until something else is copied.
        done = subprocess.run(["wl-copy", "--type", mime], input=data, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        debug("wl-copy:", error)
        return False
    return done.returncode == 0


def read_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as file:
            return file.read()
    except OSError:
        return None


def keep_image(data: bytes, mime: str) -> str:
    """The image in Handset's clipboard folder (only this person's), under a name of its own."""
    from .iphone import clip_folder

    path = os.path.join(clip_folder(), "computer." + ("jpg" if mime == "image/jpeg" else "png"))
    with open(path + ".new", "wb") as file:
        file.write(data)
    os.replace(path + ".new", path)
    return path


def image_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as file:
            return hashlib.sha1(file.read()).hexdigest()
    except OSError:
        return None
