"""Keeps the apps of connected phones among the desk's apps (apps.py): an entry for each, in search
and in the apps list, made when a phone connects and gone when it goes, but for the ones pinned to
the dock. It runs on its own, `handset --sync`, from sign-in (an autostart entry), one at a time;
Handset starts it too when it isn't running. It needs no window and loads no GTK.

An Android phone lists its apps through scrcpy's server; their icons follow, read out of their
APKs one by one (slow, so a letter stands in meanwhile), and are kept in ~/.cache/handset/apps
with the version of the app each came from (versions.json): only a new or updated app's icon is
read again. That reading takes memory the sync would keep for good, so a worker of its own does it
(`handset --sync-icons SERIAL FOLDER`, the packages on its standard input) and goes. An
iPhone's icons come with its list, through Handset's helper.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time

from gi.repository import GLib

from . import APP_ID, DEFAULT_APP_ID, apps
from .apps import PhoneApp
from .log import debug
from .service import Phone, Watch

# A connected phone's apps are listed again this often, for the ones put on it or taken off.
RELIST_S = 30 * 60
# Icons go into their entries this many at a time: the desk reads all its apps again each time.
ICON_BATCH = 12
ICON_SIZE = 192


def lock_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/handset-{os.getuid()}"
    return os.path.join(runtime, "handset-sync.lock")


def take_lock():
    """The lock only one sync holds (an open file), or None when another has it."""
    os.makedirs(os.path.dirname(lock_path()), exist_ok=True)
    file = open(lock_path(), "w")
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        return None
    return file


def running() -> bool:
    held = take_lock()
    if held is None:
        return True
    held.close()
    return False


def start_if_needed():
    """The sync, started on its own when none is running (from Handset, say). A development or
    test copy of Handset (its own HANDSET_APP_ID) starts none: it would keep the real desk's."""
    if APP_ID != DEFAULT_APP_ID or running():
        return
    try:
        subprocess.Popen([apps.launcher(), "--sync"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        debug("the app sync didn't start:", error)


class AppSync:
    def __init__(self):
        self.watch = Watch(self._phones_changed)
        self.online: dict[str, Phone] = {}
        self.listed: dict[str, float] = {}  # phone id → when its apps were last listed
        self.busy: set[str] = set()
        self.seen = False  # a first state from the service: entries left from before are checked
        self.lock = threading.Lock()  # entries are written by one at a time

    def start(self):
        self.watch.start()
        GLib.timeout_add_seconds(60, self._relist)

    def _phones_changed(self, phones: list[Phone]):
        online = {p.id: p for p in phones if p.online}
        gone = set(self.online) - set(online)
        came = set(online) - set(self.online)
        first, self.seen = not self.seen, True
        self.online = online
        if gone or came or first:
            debug("phones connected:", sorted(p.name for p in online.values()))
            self._drop_unconnected()
        for phone_id in gone:
            self.listed.pop(phone_id, None)  # listed afresh when it's back
        self._relist()

    def _relist(self):
        for phone in self.online.values():
            due = time.monotonic() - self.listed.get(phone.id, -RELIST_S) >= RELIST_S
            if due and phone.id not in self.busy:
                self.busy.add(phone.id)
                threading.Thread(target=self._sync, args=(phone,), daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _drop_unconnected(self):
        """The entries of phones that aren't connected go, but for the ones in the dock."""
        with self.lock:
            docked = set(apps.docked())
            for app in apps.entries():
                if app.phone_id not in self.online and f"{app.id}.desktop" not in docked:
                    debug("away with", app.title)
                    apps.remove_entry(app.phone_id, app.app)

    # ---- One phone's apps (off the main loop) ------------------------------------------------

    def _sync(self, phone: Phone):
        try:
            if phone.platform == "ios":
                self._sync_iphone(phone)
            else:
                self._sync_android(phone)
        except Exception as error:  # noqa: BLE001 - a phone that misbehaves is tried again later
            debug("listing", phone.name, "failed:", error)
        finally:
            GLib.idle_add(self._done, phone.id)

    def _done(self, phone_id: str):
        self.busy.discard(phone_id)
        if phone_id in self.online:
            self.listed[phone_id] = time.monotonic()
        return GLib.SOURCE_REMOVE

    def _sync_android(self, phone: Phone):
        from . import android

        serial = android.find_serial(phone.id, phone.address, phone.port)
        if not serial:
            return
        listed = android.list_apps(serial)
        if isinstance(listed, str):
            debug(phone.name, "didn't list its apps:", listed)
            return
        folder = apps.cache_dir(phone.id)
        os.makedirs(folder, exist_ok=True)
        found = [PhoneApp(phone.id, phone.name, "android", package, name) for name, package in listed]
        self._write(phone.id, [(app, read_file(apps.cached_icon(folder, app.app))) for app in found], whole=True)
        versions = android.app_versions(serial)
        kept = load_versions(folder)
        missing = {}
        for app in found:
            have = os.path.isfile(apps.cached_icon(folder, app.app))
            if have and app.app not in kept and versions.get(app.app):
                kept[app.app] = versions[app.app]  # an icon from before versions were kept: as it is now
            elif not have or kept.get(app.app, versions.get(app.app)) != versions.get(app.app):
                missing[app.app] = app
        if not missing:
            save_versions(folder, kept)
            return
        try:
            worker = subprocess.Popen([apps.launcher(), "--sync-icons", serial, folder], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as error:
            debug("the icon worker didn't start:", error)
            return
        worker.stdin.write("".join(f"{package}\n" for package in missing))
        worker.stdin.close()
        batch = []
        for line in worker.stdout:  # each icon it kept, as it keeps it
            app = missing.get(line.strip())
            if app is None:
                continue
            if versions.get(app.app):
                kept[app.app] = versions[app.app]
            batch.append((app, read_file(apps.cached_icon(folder, app.app))))
            if len(batch) >= ICON_BATCH or phone.id not in self.online:
                self._write(phone.id, batch)
                batch = []
            if phone.id not in self.online:
                worker.kill()
                break
        if batch:
            self._write(phone.id, batch)
        worker.wait()
        save_versions(folder, kept)

    def _sync_iphone(self, phone: Phone):
        from . import iphone

        link = iphone.open_link(phone.id, phone.address, phone.model)
        if not isinstance(link, iphone.Link):
            return
        listed = iphone.list_apps(link, apps.cache_dir(phone.id))
        if isinstance(listed, str):
            debug(phone.name, "didn't list its apps:", listed)
            return
        self._write(phone.id, [(PhoneApp(phone.id, phone.name, "ios", bundle, name), read_file(icon))
                               for name, bundle, icon in listed], whole=True)

    def _write(self, phone_id: str, found: list[tuple[PhoneApp, bytes | None]], whole: bool = False):
        """Entries for these apps of a phone (their icons, where given); `whole`: they're all
        it has, so any others it had go (but for the pinned)."""
        with self.lock:
            if phone_id not in self.online:
                return
            changed = sum(apps.write_entry(app, icon) for app, icon in found)
            if whole:
                there = {app.app for app, _ in found}
                docked = set(apps.docked())
                for app in apps.entries(phone_id):
                    if app.app not in there and f"{app.id}.desktop" not in docked:
                        apps.remove_entry(app.phone_id, app.app)
                        changed += 1
            debug(len(found), "apps of", phone_id, "written,", changed, "changed")


def load_versions(folder: str) -> dict[str, str]:
    """The app versions the kept icons came from."""
    try:
        with open(os.path.join(folder, "versions.json")) as file:
            versions = json.load(file)
        return versions if isinstance(versions, dict) else {}
    except (OSError, ValueError):
        return {}


def save_versions(folder: str, versions: dict[str, str]):
    GLib.file_set_contents(os.path.join(folder, "versions.json"), json.dumps(versions, sort_keys=True).encode())


def read_file(path: str | None) -> bytes | None:
    if not path:
        return None
    try:
        with open(path, "rb") as file:
            return file.read()
    except OSError:
        return None


def icons_worker(serial: str, folder: str) -> int:
    """`handset --sync-icons`: the icons of the packages on standard input, read from the phone
    and kept in `folder`, each package said on standard output once its icon is there."""
    from . import apkicon

    try:
        os.nice(10)
    except OSError:
        pass
    for line in sys.stdin:
        package = line.strip()
        read = apkicon.read_app(serial, package, size=ICON_SIZE) if package else None
        if read and read[1]:
            GLib.file_set_contents(apps.cached_icon(folder, package), read[1])
            print(package, flush=True)
    return 0


def forget_all():
    """Every phone app's entry and icon gone, and out of the dock (when Handset goes)."""
    for app in apps.entries():
        apps.unpin(app.phone_id, app.app)
        apps.remove_entry(app.phone_id, app.app)


def main() -> int:
    held = take_lock()
    if held is None:
        return 0  # one is running already
    try:
        os.nice(10)  # icons are read when nothing else wants the time
    except OSError:
        pass
    loop = GLib.MainLoop()
    for number in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, loop.quit)
    AppSync().start()
    loop.run()
    held.close()
    return 0
