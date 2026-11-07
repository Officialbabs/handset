"""The phones' files in the desktop's search, over org.gnome.Shell.SearchProvider2 (what the
desktop's launcher asks apps for results with): each connected phone's files and folders by name.

A search never waits on a phone: it looks through an index of names kept here, made by asking a
phone for its whole storage at once (an Android phone's `find`, the iPhone helper's walk) when its
folder first comes (phonefs.py), and again only when it's searched and its index has grown old:
the phone is left alone meanwhile. A result opens from that
folder, in the app that opens its kind of file (the file fetched as it's opened). A phone away is
searched too, in the index kept from when it was here: its results say it's not connected, and
one opened opens once its folder is back.

The sync serves it, under a bus name of its own (install.sh tells the desktop which).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio, GLib

from . import APP_ID
from .log import debug

BUS_NAME = APP_ID + ".Sync"
PATH = "/io/github/Officialbabs/Handset/SearchProvider"
LIMIT = 10
INDEX_EVERY_S = 30 * 60  # an index this old is made again, when the phones are searched
# A result opened while its phone is away opens when the phone's folder is back, within this long.
OPEN_WHEN_BACK_S = 120
IPHONE_MOST = 100_000  # names asked of an iPhone, at most, for each place

XML = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""

# An Android phone's storage in one go: its files and folders, each with its size and time, the
# hidden ones and Android/ (apps' own data) left out. ".?*" rather than ".*", which is "." too.
ANDROID_FIND = ("cd /sdcard && find . -name '.?*' -prune -o -path ./Android -prune -o "
                "-type d -printf 'd\\t0\\t%T@\\t%p\\n' -o -type f -printf 'f\\t%s\\t%T@\\t%p\\n'")


@dataclass(frozen=True)
class Found:
    """A file or folder on a phone: its path from the phone's top as its folder here shows it
    ("/DCIM/Camera/PXL_1.jpg", "/On My iPhone/VLC/film.mp4")."""

    path: str
    folder: bool = False
    size: int = 0
    modified: float = 0.0

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def parse_find(text: str) -> list[Found]:
    """ANDROID_FIND's lines: kind, size, time and path, a tab between each."""
    found = []
    for line in text.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4 or not parts[3].startswith("./"):
            continue
        kind, size, modified, path = parts
        try:
            found.append(Found(path[1:], kind == "d", int(size), float(modified)))
        except ValueError:
            continue
    return found


def android_files(serial: str) -> list[Found] | None:
    from .android import shell

    try:
        return parse_find(shell(serial, ANDROID_FIND, timeout=180))
    except (OSError, subprocess.SubprocessError) as error:
        debug("the phone's files weren't listed:", error)
        return None


def iphone_files(session) -> list[Found] | None:
    """An iPhone's media folders and the files of the apps that share theirs, through a
    FilesSession of its own (so the walk doesn't hold up the phone's folder)."""

    def walk(path: str) -> list[dict] | None:
        answer = session._ask({"op": "find", "path": path, "text": "", "most": IPHONE_MOST})
        return answer.get("entries", []) if isinstance(answer, dict) else None

    media = walk("/")
    if media is None:
        return None
    found = [Found("/" + e["path"].lstrip("/"), bool(e.get("folder")), int(e.get("size") or 0),
                   float(e.get("modified") or 0)) for e in media if e.get("path")]
    top = session.apps_root
    for bundle, name in session.sharing().items():
        found.append(Found(f"{top}/{name}", True))
        for entry in walk(f"{top}/{bundle}") or []:
            path = entry.get("path", "")
            if path.startswith(f"{top}/{bundle}/"):
                found.append(Found(f"{top}/{name}/{path[len(top) + len(bundle) + 2:]}", bool(entry.get("folder")),
                                   int(entry.get("size") or 0), float(entry.get("modified") or 0)))
    return found


def lister(backend) -> Callable[[], list[Found] | None]:
    """How to list all of a phone's files, for its folder's backend (phonefs.Android or IPhone)."""
    from . import phonefs

    if isinstance(backend, phonefs.Android):
        return lambda: android_files(backend.serial)
    if isinstance(backend, phonefs.IPhone):
        def walk():
            from .iphone import FilesSession

            session = FilesSession(backend.session.link)
            try:
                return iphone_files(session)
            finally:
                session.stop()

        return walk
    return lambda: None


def index_path(phone_id: str) -> str:
    from .phonefs import cache_name

    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "search", cache_name(phone_id) + ".json")


def save_index(phone_id: str, found: list[Found]):
    path = index_path(phone_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".new", "w") as file:
        json.dump({"time": time.time(), "files": [[f.path, f.folder, f.size, f.modified] for f in found]}, file)
    os.replace(path + ".new", path)


def load_index(phone_id: str) -> tuple[list[Found], float]:
    try:
        with open(index_path(phone_id)) as file:
            saved = json.load(file)
        return [Found(*entry) for entry in saved.get("files", [])], float(saved.get("time", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return [], 0.0


def score(terms: list[str], name: str) -> int | None:
    """How well a name (folded) answers the terms (folded): None, not at all (each term must be
    in it); 3 it's the name (less its ending); 2 it starts so; 1 a word in it does; 0 it's in it."""
    if not all(term in name for term in terms):
        return None
    query = " ".join(terms)
    if name == query or name.rsplit(".", 1)[0] == query:
        return 3
    if name.startswith(terms[0]):
        return 2
    if re.search(r"(?:^|[\W_])" + re.escape(terms[0]), name):
        return 1
    return 0


class Index:
    """One phone's names, folded once, for searching as fast as typing."""

    def __init__(self, found: list[Found], made: float):
        self.found = found
        self.folded = [f.name.casefold() for f in found]
        self.made = made
        self.by_path = {f.path: f for f in found}

    def search(self, terms: list[str]) -> list[tuple[int, Found]]:
        hits = []
        for folded, found in zip(self.folded, self.found):
            points = score(terms, folded)
            if points is not None:
                hits.append((points, found))
        return hits


def result_id(phone_id: str, path: str) -> str:
    return f"{phone_id}\n{path}"


def split_id(identifier: str) -> tuple[str, str]:
    phone_id, _, path = identifier.partition("\n")
    return phone_id, path


def icon_for(found: Found) -> str:
    if found.folder:
        return Gio.ThemedIcon.new("folder").to_string()
    kind = Gio.content_type_guess(found.name, None)[0]
    return Gio.content_type_get_icon(kind).to_string()


class PhoneSearch:
    """Answers the desktop's search with the connected phones' files. `phones()` gives each
    phone whose folder is here: its id → (its name, its folder here, how to list it all: a
    function answering [Found] or None when the phone didn't)."""

    def __init__(self, phones: Callable[[], dict[str, tuple[str, str, Callable[[], list[Found] | None]]]],
                 wanted: Callable[[], None] | None = None, known: Callable[[], dict[str, str]] | None = None):
        self.phones = phones
        self.wanted = wanted  # a search came: phones not connected now are wanted (adbwifi)
        # Every phone Handset knows, here or not: its id → its name. One away a moment (a flaky
        # Wi-Fi link, a phone across the room) is searched in the index kept from when it was
        # here; its results say so, and one opened opens once the phone is back.
        self.known = known
        self.waiting: dict[str, tuple[str, float]] = {}  # an away phone → a result to open, since when
        self.indexes: dict[str, Index] = {}
        self.indexing: set[str] = set()
        self.bus: Gio.DBusConnection | None = None
        self.registered = 0
        self.owner = 0

    def start(self):
        self.refresh()

    def refresh(self):
        """On the bus only while there's something to search: a phone's folder here, or one away
        a moment. Otherwise (the phones switched off in Handset, or forgotten) the desktop's search
        finds no one to ask (its entry says not to start Handset for it) and doesn't wait."""
        if self.phones() or self._away({}):
            if not self.owner:
                self.owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                              self._on_bus, None, None)
        elif self.owner:
            self.stop()

    def stop(self):
        if self.bus and self.registered:
            self.bus.unregister_object(self.registered)
        self.registered = 0
        if self.owner:
            Gio.bus_unown_name(self.owner)
        self.owner = 0

    def _on_bus(self, bus: Gio.DBusConnection, _name: str):
        self.bus = bus
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self.registered = bus.register_object(PATH, info, self._on_call, None, None)

    # -- the index

    def phone_here(self, phone_id: str):
        """A phone's folder came: its index from before, at once; one made if there's none, or
        it's old."""
        self._kept(phone_id)
        self.freshen(phone_id)
        identifier, since = self.waiting.pop(phone_id, ("", 0.0))
        if identifier and time.monotonic() - since < OPEN_WHEN_BACK_S:
            GLib.idle_add(self.activate, identifier)
        self.refresh()

    def _kept(self, phone_id: str) -> Index | None:
        """A phone's index, read from where it was kept if it isn't in memory yet."""
        if phone_id not in self.indexes:
            found, made = load_index(phone_id)
            if found:
                self.indexes[phone_id] = Index(found, made)
        return self.indexes.get(phone_id)

    def let_go(self):
        """Memory is short: the indexes leave memory (about 36 MB for a phone of 100,000 files),
        each read again from where it was kept when it's next searched."""
        self.indexes.clear()

    def _away(self, here: dict) -> dict[str, str]:
        """The phones known but not here now: id → name."""
        return {phone_id: name for phone_id, name in (self.known() if self.known else {}).items()
                if phone_id not in here}

    def freshen(self, phone_id: str):
        index = self._kept(phone_id)
        if not index or time.time() - index.made >= INDEX_EVERY_S:
            self.index(phone_id)

    def index(self, phone_id: str):
        listing = self.phones().get(phone_id)
        if not listing or phone_id in self.indexing:
            return
        self.indexing.add(phone_id)

        def made():
            started = time.monotonic()
            found = listing[2]()
            if found is not None:
                try:
                    save_index(phone_id, found)
                except OSError as error:
                    debug("the phone's index wasn't kept:", error)
                debug(f"{listing[0]}'s files indexed: {len(found)} in {time.monotonic() - started:.1f} s")
            GLib.idle_add(self._indexed, phone_id, found)

        threading.Thread(target=made, daemon=True).start()

    def _indexed(self, phone_id: str, found: list[Found] | None):
        self.indexing.discard(phone_id)
        if found is not None:
            self.indexes[phone_id] = Index(found, time.time())
        return GLib.SOURCE_REMOVE

    # -- searching

    def search(self, terms: list[str]) -> list[str]:
        terms = [t.casefold() for t in terms if t.strip()]
        if not terms:
            return []
        if self.wanted:
            self.wanted()  # one away answers the next searches, once it's back
        hits = []
        here = self.phones()
        for phone_id in here:
            self.freshen(phone_id)  # for the next search: this one answers from the index as it is
            index = self.indexes.get(phone_id)
            if index:
                hits += [(points, found, phone_id) for points, found in index.search(terms)]
        for phone_id in self._away(here):
            index = self._kept(phone_id)
            if index:
                hits += [(points, found, phone_id) for points, found in index.search(terms)]
        # Surest first, then files before folders, newest first.
        hits.sort(key=lambda hit: (-hit[0], hit[1].folder, -hit[1].modified))
        return [result_id(phone_id, found.path) for _points, found, phone_id in hits[:LIMIT]]

    def _found(self, identifier: str) -> tuple[str, str | None, Found] | None:
        """A result's phone's name, its folder here (None while the phone is away) and what it
        is (only what was offered: anything on the bus can ask)."""
        phone_id, path = split_id(identifier)
        here = self.phones()
        if phone_id in here:
            name, folder = here[phone_id][0], here[phone_id][1]
        else:
            away = self._away(here)
            if phone_id not in away:
                return None
            name, folder = away[phone_id], None
        index = self._kept(phone_id)
        found = index.by_path.get(path) if index else None
        if not found:
            return None
        return name, folder, found

    def metas(self, identifiers: list[str]) -> list[dict]:
        metas = []
        for identifier in identifiers:
            known = self._found(identifier)
            if not known:
                continue
            name, folder, found = known
            where = [part for part in found.path.split("/")[:-1] if part]
            phone = name if folder else f"{name} (not connected)"
            metas.append({
                "id": GLib.Variant("s", identifier),
                "name": GLib.Variant("s", found.name),
                "description": GLib.Variant("s", " › ".join([phone] + where)),
                "gicon": GLib.Variant("s", icon_for(found)),
            })
        return metas

    def activate(self, identifier: str):
        known = self._found(identifier)
        if not known:
            return GLib.SOURCE_REMOVE
        name, folder, found = known
        if folder:
            open_here(os.path.join(folder, found.path.lstrip("/")), found.folder)
            return GLib.SOURCE_REMOVE
        phone_id, _path = split_id(identifier)
        self.waiting[phone_id] = (identifier, time.monotonic())
        if self.wanted:
            self.wanted()
        self.tell(f"{found.name} opens when {name} is connected again",
                  "It opens by itself as soon as the phone is back on this computer's Wi-Fi or cable.")
        return GLib.SOURCE_REMOVE

    def tell(self, summary: str, body: str):
        """A desktop notification, Handset's."""
        if not self.bus:
            return
        self.bus.call("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                      "org.freedesktop.Notifications", "Notify",
                      GLib.Variant("(susssasa{sv}i)", ("Handset", 0, APP_ID, summary, body, [],
                                                        {"desktop-entry": GLib.Variant("s", APP_ID)}, 8000)),
                      None, Gio.DBusCallFlags.NONE, -1, None, None, None)

    def launch_search(self):
        """The search's own page: the phones' folders, in the file manager."""
        from .phonefs import phones_folder

        folders = [folder for _name, folder, _list in self.phones().values()]
        open_here(folders[0] if len(folders) == 1 else phones_folder(), True)
        return GLib.SOURCE_REMOVE

    def _on_call(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation):
        args = params.unpack()
        if method == "GetInitialResultSet":
            invocation.return_value(GLib.Variant("(as)", (self.search(args[0]),)))
        elif method == "GetSubsearchResultSet":
            invocation.return_value(GLib.Variant("(as)", (self.search(args[1]),)))
        elif method == "GetResultMetas":
            invocation.return_value(GLib.Variant("(aa{sv})", (self.metas(args[0]),)))
        elif method == "ActivateResult":
            invocation.return_value(None)
            GLib.idle_add(self.activate, args[0])
        elif method == "LaunchSearch":
            invocation.return_value(None)
            GLib.idle_add(self.launch_search)
        else:
            invocation.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method)


def open_here(path: str, folder: bool):
    """A file (or folder) in the app that opens its kind: chosen by its name, so the phone's file
    isn't read here first; it's fetched as that app opens it."""
    kind = "inode/directory" if folder else Gio.content_type_guess(os.path.basename(path), None)[0]
    app = Gio.AppInfo.get_default_for_type(kind, False)
    try:
        if app:
            app.launch([Gio.File.new_for_path(path)], None)
        else:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(os.path.dirname(path)).get_uri(), None)
    except GLib.Error as error:
        debug("the phone's file didn't open:", error)
