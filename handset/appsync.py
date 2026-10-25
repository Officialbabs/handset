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

import dataclasses
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time

from gi.repository import Gio, GLib

from . import APP_ID, DEFAULT_APP_ID, apps
from .apps import PhoneApp
from .log import debug
from .media import NowPlaying, PhonePlayer
from .notifications import Desk, Note
from .service import Phone, Watch

# A connected phone's apps are listed again this often, for the ones put on it or taken off.
RELIST_S = 30 * 60
RETRY_S = 2 * 60  # when listing them didn't work
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


def pair_request_path() -> str:
    return os.path.join(os.path.dirname(lock_path()), "handset-pair-iphone")


def ask_to_pair_iphone():
    """`handset --pair-iphone`: the running sync makes the computer visible for a few minutes."""
    GLib.file_set_contents(pair_request_path(), b"")


def notification_state_path() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "handset", "notifications.json")


def known_iphones() -> set[str]:
    """The Bluetooth addresses of the iPhones paired for their notifications."""
    try:
        with open(notification_state_path()) as file:
            return set(json.load(file).get("iphones", []))
    except (OSError, ValueError, AttributeError):
        return set()


def notifications_wanted() -> bool:
    """Handset's settings may say no ("phone_notifications": false)."""
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config, "handset", "settings.json")) as file:
            return json.load(file).get("phone_notifications", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


class AppSync:
    def __init__(self):
        self.watch = Watch(self._phones_changed)
        self.desk: Desk | None = None
        self.notifiers: dict[str, object] = {}  # an Android phone's id → its notifications
        self.players: dict[str, PhonePlayer] = {}  # a phone's id → what it plays, on the desk
        self.media: dict[str, object] = {}  # an Android phone's id → what follows its media sessions
        self.autopause = None  # one thing playing at a time (autopause.AutoPause)
        self.looking: set[tuple[str, str, str]] = set()  # covers being looked up
        self.ancs = None
        self.phone_names: dict[str, str] = {}
        self.online: dict[str, Phone] = {}
        self.listed: dict[str, float] = {}  # phone id → when its apps were last listed
        self.busy: set[str] = set()
        self.seen = False  # a first state from the service: entries left from before are checked
        self.lock = threading.Lock()  # entries are written by one at a time

    def stop(self):
        """Its readers on the phones stopped (adb's would go on without it), its players gone."""
        for follower in list(self.notifiers.values()) + list(self.media.values()):
            follower.stop()
        if self.ancs:
            self.ancs.stop()
        if self.autopause:
            self.autopause.stop()
        for player in self.players.values():
            player.stop()

    def start(self):
        self.watch.start()
        from . import autopause

        if autopause.wanted():
            self.autopause = autopause.AutoPause(lambda: self.players)
            self.autopause.start()
        GLib.timeout_add_seconds(60, self._relist)
        apps.forget_old_icons()
        GLib.timeout_add_seconds(6 * 3600, lambda: (apps.forget_old_icons(), True)[1])
        if notifications_wanted():
            self._start_notifications()

    # ---- Notifications ---------------------------------------------------------------------

    def _start_notifications(self):
        try:
            self.desk = Desk()
            self.desk.on_action = self._action
        except GLib.Error as error:
            debug("no notifications on the desk:", error.message)  # what plays still shows
        from .ancs import Ancs

        self.ancs = Ancs(self._iphone_note, lambda phone_id, key: self.desk and self.desk.close(phone_id, key),
                         known_iphones, self._remember_iphone, self._iphone_media, self._iphone_app)
        self.ancs.start()
        folder = Gio.File.new_for_path(os.path.dirname(pair_request_path()))
        self.pair_monitor = folder.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.pair_monitor.connect("changed", self._pair_asked)
        if os.path.exists(pair_request_path()):  # asked before this sync was there to hear it
            os.unlink(pair_request_path())
            self.ancs.pair()

    def _pair_asked(self, _monitor, file, _other, event):
        if file.get_path() == pair_request_path() and event == Gio.FileMonitorEvent.CREATED:
            os.unlink(file.get_path())
            debug("pairing an iPhone for notifications")
            if self.ancs:
                self.ancs.pair()

    # ---- What the phones play ------------------------------------------------------------------

    def _show_media(self, phone_id: str, now: NowPlaying | None, control):
        """A phone's now playing on the desk (or gone from it), its controls worked by `control`."""
        player = self.players.get(phone_id)
        if player is None:
            if now is None:
                return
            player = self.players[phone_id] = PhonePlayer(phone_id, self.phone_names.get(phone_id, "Phone"), control)
        player.phone_name = self.phone_names.get(phone_id, player.phone_name)
        started = bool(now and now.playing) and not (player.now and player.now.playing)
        player.show(now)
        if started and self.autopause:
            self.autopause.phone_started(phone_id)

    def _iphone_media(self, now: NowPlaying | None):
        if self.ancs and self.ancs.media:
            self._show_media(self.ancs.phone_id, self._with_cover(now), self.ancs.media.control)

    def _with_cover(self, now: NowPlaying | None) -> NowPlaying | None:
        """An iPhone's song with its cover (looked up once in Apple's catalog: covers.py); while
        it's being looked up, without, and shown again with it once it's here."""
        if now is None or now.art:
            return now
        from . import covers

        path = covers.kept(now.title, now.artist, now.album)
        if os.path.exists(path):
            return dataclasses.replace(now, art=path)
        key = (now.title, now.artist, now.album)
        if key not in self.looking:
            self.looking.add(key)

            def look():
                found = covers.cover(*key)
                GLib.idle_add(self._cover_came, key, found)

            threading.Thread(target=look, daemon=True).start()
        return now

    def _cover_came(self, key, found: str | None):
        self.looking.discard(key)
        shown = self.ancs.media.shown if self.ancs and self.ancs.media else None
        if found and shown and (shown.title, shown.artist, shown.album) == key:
            self._iphone_media(shown)
        return GLib.SOURCE_REMOVE

    def _iphone_app(self, name: str) -> str:
        """The iPhone's app called `name` (the media service names its player only so)."""
        from .ancs import plain_name

        wanted = plain_name(name).casefold()
        phone_id = self.ancs.phone_id if self.ancs else ""
        return next((app.app for app in apps.entries(phone_id) if app.name.casefold() == wanted), "")

    def _android_media(self, phone_id: str, now: NowPlaying | None):
        follower = self.media.get(phone_id)
        if follower:
            self._show_media(phone_id, now, follower.control)

    def _remember_iphone(self, address: str):
        known = known_iphones()
        if address and address not in known:
            os.makedirs(os.path.dirname(notification_state_path()), exist_ok=True)
            GLib.file_set_contents(notification_state_path(),
                                   json.dumps({"iphones": sorted(known | {address})}).encode())

    def _iphone_note(self, note: Note):
        if self.desk:
            self.desk.show(note, self.phone_names.get(note.phone_id, "iPhone"), self.ancs.app_name(note.app))

    def _android_note(self, note: Note):
        if self.desk:
            self.desk.show(note, self.phone_names.get(note.phone_id, "phone"))

    def _action(self, note: Note, action: str):
        from .notifications import open_app

        if action in ("ancs-positive", "ancs-negative") and self.ancs:
            self.ancs.act(note.key, action == "ancs-positive")
        elif action == "default":
            open_app(note, own_window=apps.find(note.phone_id, note.app) is not None)

    def _follow(self, phone_id: str, serial: str):
        """An Android phone's notifications and what it plays, followed while it's connected."""
        if phone_id not in self.online:
            return GLib.SOURCE_REMOVE
        if self.desk and phone_id not in self.notifiers:
            from .notifications import AndroidNotifications

            notifier = AndroidNotifications(phone_id, serial, self._android_note, self.desk.close)
            self.notifiers[phone_id] = notifier
            notifier.start()
        if phone_id not in self.media:
            from .androidmedia import MediaFollower

            follower = MediaFollower(serial, lambda now, p=phone_id: self._android_media(p, now))
            self.media[phone_id] = follower
            follower.start()
        return GLib.SOURCE_REMOVE

    def _phones_changed(self, phones: list[Phone]):
        online = {p.id: p for p in phones if p.online}
        self.phone_names = {p.id: p.name for p in phones}
        iphones = [p.id for p in phones if p.platform == "ios"]
        if self.ancs and len(iphones) == 1 and self.ancs.phone_id != iphones[0]:
            # Its notes and what it plays go with its apps' entries.
            player = self.players.pop(self.ancs.phone_id, None)
            if player:
                player.stop()
            self.ancs.phone_id = iphones[0]
            if self.ancs.media:
                self._iphone_media(self.ancs.media.shown)
        gone = set(self.online) - set(online)
        came = set(online) - set(self.online)
        first, self.seen = not self.seen, True
        self.online = online
        if gone or came or first:
            debug("phones connected:", sorted(p.name for p in online.values()))
            self._drop_unconnected()
        for phone_id in gone:
            self.listed.pop(phone_id, None)  # listed afresh when it's back
            notifier = self.notifiers.pop(phone_id, None)
            if notifier:
                notifier.stop()
            follower = self.media.pop(phone_id, None)
            if follower:
                follower.stop()
                player = self.players.pop(phone_id, None)
                if player:
                    player.stop()
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
        listed = False
        try:
            listed = self._sync_iphone(phone) if phone.platform == "ios" else self._sync_android(phone)
        except Exception as error:  # noqa: BLE001 - a phone that misbehaves is tried again later
            debug("listing", phone.name, "failed:", error)
        finally:
            GLib.idle_add(self._done, phone.id, bool(listed))

    def _done(self, phone_id: str, listed: bool):
        """Listed, it's listed again in a while; not (asleep, say), it's tried again soon."""
        self.busy.discard(phone_id)
        if phone_id in self.online:
            self.listed[phone_id] = time.monotonic() - (0 if listed else RELIST_S - RETRY_S)
        return GLib.SOURCE_REMOVE

    def _sync_android(self, phone: Phone):
        from . import android

        serial = android.find_serial(phone.id, phone.address, phone.port)
        if not serial:
            return False
        GLib.idle_add(self._follow, phone.id, serial)
        listed = android.list_apps(serial)
        if isinstance(listed, str):
            debug(phone.name, "didn't list its apps:", listed)
            return False
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
            return True
        try:
            worker = subprocess.Popen([apps.launcher(), "--sync-icons", serial, folder], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as error:
            debug("the icon worker didn't start:", error)
            return True
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
        return True

    def _sync_iphone(self, phone: Phone):
        from . import iphone

        link = iphone.open_link(phone.id, phone.address, phone.model)
        if not isinstance(link, iphone.Link):
            return False
        listed = iphone.list_apps(link, apps.cache_dir(phone.id))
        if isinstance(listed, str):
            debug(phone.name, "didn't list its apps:", listed)
            return False
        self._write(phone.id, [(PhoneApp(phone.id, phone.name, "ios", bundle, name), read_file(icon))
                               for name, bundle, icon in listed], whole=True)
        return True

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
    sync = AppSync()
    sync.start()
    loop.run()
    sync.stop()
    held.close()
    return 0
