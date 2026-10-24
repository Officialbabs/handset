"""The phone's files, in a dialog: its folders to look through, a file to save in Downloads here
or to open with this computer's apps, and files from here to send into the folder looked at.
Android's storage over adb (android.AdbFiles), an iPhone's media files (photos, Downloads,
recordings) through Handset's helper (iphone.FilesSession).
"""

from __future__ import annotations

import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .service import Phone  # noqa: E402

def downloads_dir() -> str:
    return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~/Downloads")


def opened_dir(phone_id: str) -> str:
    """Where files opened from the phone are kept a while."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "opened", "".join(c if c.isalnum() else "_" for c in phone_id))


def size_words(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


class FilesDialog(Adw.Dialog):
    def __init__(self, window, phone: Phone, files):
        super().__init__(title=f"Files on {phone.name}", content_width=440, content_height=620)
        self.window = window
        self.phone = phone
        self.files = files  # android.AdbFiles or iphone.FilesSession; None: not here
        self.top = files.top if files else "/"
        self.folder = self.top
        self.connect("closed", lambda _d: self.files and threading.Thread(target=self.files.stop, daemon=True).start())
        self.listing = 0  # each listing asked for; an older one coming late is let go

        self.back = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Up a folder", sensitive=False)
        self.back.connect("clicked", lambda _b: self.open_folder(os.path.dirname(self.folder)))
        send = Gtk.Button(icon_name="document-send-symbolic", tooltip_text="Send files here from this computer")
        send.connect("clicked", self._choose_to_send)
        header = Adw.HeaderBar()
        header.pack_start(self.back)
        header.pack_end(send)
        self.place = Gtk.Label(xalign=0, ellipsize=3, margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)
        self.place.add_css_class("dim-label")
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(Adw.Clamp(child=self.list, margin_start=12, margin_end=12, margin_top=6, margin_bottom=12))
        self.problem = Adw.StatusPage(icon_name="folder-symbolic")
        loading = Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(loading, "loading")
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.problem, "problem")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.place)
        self.set_child(view)
        if files:
            self.open_folder(self.top)
        else:
            send.set_sensitive(False)
            self.problem.set_title("Not connected")
            self.problem.set_description(f"{phone.name}'s files open here once its screen shows in this window.")
            self.stack.set_visible_child_name("problem")

    def say(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text))

    # ---- A folder ---------------------------------------------------------------------------

    def open_folder(self, folder: str):
        self.folder = folder
        self.listing += 1
        number = self.listing
        self.back.set_sensitive(folder.rstrip("/") != self.top.rstrip("/"))
        inside = folder[len(self.top.rstrip("/")):]
        self.place.set_label(" › ".join([self.phone.name] + [part for part in inside.split("/") if part]))
        self.stack.set_visible_child_name("loading")

        def work():
            found = self.files.list_folder(folder)
            GLib.idle_add(self._listed, number, found)

        threading.Thread(target=work, daemon=True).start()

    def _listed(self, number: int, found):
        if number != self.listing:
            return GLib.SOURCE_REMOVE
        self.list.remove_all()
        if isinstance(found, str) or not found:
            self.problem.set_title("Nothing here" if not isinstance(found, str) else "Can't open it")
            self.problem.set_description(found if isinstance(found, str) else "")
            self.stack.set_visible_child_name("problem")
            return GLib.SOURCE_REMOVE
        for item in found:
            row = Adw.ActionRow(title=GLib.markup_escape_text(item.name), activatable=True)
            when = time.strftime("%-d %b %Y, %H:%M", time.localtime(item.modified))
            row.set_subtitle(when if item.folder else f"{size_words(item.size)} · {when}")
            content_type, _uncertain = Gio.content_type_guess(item.name, None)
            icon = "folder-symbolic" if item.folder else None
            image = Gtk.Image.new_from_icon_name(icon) if icon else Gtk.Image.new_from_gicon(
                Gio.content_type_get_symbolic_icon(content_type))
            row.add_prefix(image)
            if item.folder:
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, path=item.path: self.open_folder(path))
            else:
                save = Gtk.Button(icon_name="folder-download-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="Save in Downloads")
                save.add_css_class("flat")
                save.connect("clicked", lambda _b, path=item.path: self._fetch(path, downloads_dir(), False))
                row.add_suffix(save)
                row.set_tooltip_text("Open it here")
                row.connect("activated", lambda _r, path=item.path: self._fetch(path, opened_dir(self.phone.id), True))
            self.list.append(row)
        self.stack.set_visible_child_name("list")
        return GLib.SOURCE_REMOVE

    # ---- Files both ways ----------------------------------------------------------------------

    def _fetch(self, path: str, folder: str, open_it: bool):
        name = os.path.basename(path)
        self.say(f"Fetching {name}…")

        def work():
            got = self.files.fetch_file(path, folder)
            GLib.idle_add(self._fetched, got, name, open_it)

        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, got: str | None, name: str, open_it: bool):
        if not got:
            self.say(f"{name} didn't come")
        elif open_it:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(got).get_uri(), None)
        else:
            self.say(f"{os.path.basename(got)} is in Downloads")
        return GLib.SOURCE_REMOVE

    def _choose_to_send(self, _button):
        dialog = Gtk.FileDialog(title=f"Send to {self.phone.name}", accept_label="Send")
        dialog.open_multiple(self.window, None, self._chosen)

    def _chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # not chosen
        paths = [files.get_item(i).get_path() for i in range(files.get_n_items()) if files.get_item(i).get_path()]
        if not paths:
            return
        folder = self.folder
        self.say(f"Sending {len(paths)} file{'s' if len(paths) > 1 else ''}…")

        def work():
            sent, trouble = self.files.send_files(paths, folder)
            GLib.idle_add(self._sent, folder, len(sent), trouble)

        threading.Thread(target=work, daemon=True).start()

    def _sent(self, folder: str, count: int, trouble: str | None):
        self.say(f"{count} sent" if not trouble else f"{count} sent; the rest didn't go ({trouble})")
        if folder == self.folder:
            self.open_folder(folder)
        return GLib.SOURCE_REMOVE
