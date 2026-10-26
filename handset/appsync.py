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
from .service import Phone, watch

# A connected phone's apps are listed again this often, for the ones put on it or taken off.
RELIST_S = 30 * 60
RETRY_S = 2 * 60  # when listing them didn't work
# Icons go into their entries this many at a time: the desk reads all its apps again each time.
ICON_BATCH = 12
# An iPhone not connected (by cable or Wi-Fi) this long: its notifications stop until it is again.
AWAY_FOR_S = 60
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


def settings_file() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "handset", "settings.json")


def phone_files_wanted() -> bool:
    """Handset's settings may say no to the phones' folders ("phone_files": false)."""
    try:
        with open(settings_file()) as file:
            return json.load(file).get("phone_files", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def notifications_wanted() -> bool:
    """Handset's settings may say no ("phone_notifications": false)."""
    try:
        with open(settings_file()) as file:
            return json.load(file).get("phone_notifications", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


class AppSync:
    def __init__(self):
        self.watch = watch(self._phones_changed)
        self.desk: Desk | None = None
        self.notifiers: dict[str, object] = {}  # an Android phone's id → its notifications
        self.players: dict[str, PhonePlayer] = {}  # a phone's id → what it plays, on the desk
        self.media: dict[str, object] = {}  # an Android phone's id → what follows its media sessions
        self.autopause = None  # one thing playing at a time (autopause.AutoPause)
        self.looking: set[tuple[str, str, str]] = set()  # covers being looked up
        self.ancs = None
        self.away_timer = 0  # the iPhones went: their notifications stop when it's done
        self.mounts: dict[str, object] = {}  # a phone's id → its folder here (phonefs.FileSystem)
        self.search = None  # the phones' files in the desktop's search (phonesearch.PhoneSearch)
        self.reconnect = None  # Android phones connected again over Wi-Fi (adbwifi.WifiReconnect)
        self.mounting: set[str] = set()
        self.phones_now: list[Phone] = []
        self.phone_names: dict[str, str] = {}
        self.online: dict[str, Phone] = {}
        self.listed: dict[str, float] = {}  # phone id → when its apps were last listed
        self.packages: dict[str, dict[str, str]] = {}  # an Android phone's id → its packages when it was
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
        for mounted in self.mounts.values():
            mounted.stop()
        self.mounts = {}
        if self.search:
            self.search.stop()
        if self.reconnect:
            self.reconnect.stop()

    def start(self):
        self.watch.start()
        from . import autopause

        if autopause.wanted():
            self.autopause = autopause.AutoPause(lambda: self.players)
            self.autopause.start()
        GLib.timeout_add_seconds(60, self._relist)
        apps.forget_old_icons()
        GLib.timeout_add_seconds(6 * 3600, lambda: (apps.forget_old_icons(), True)[1])
        self.notifying = notifications_wanted()
        if self.notifying:
            self._start_notifications()
        # Switches flipped in Handset's window come here (its settings file, saved anew).
        os.makedirs(os.path.dirname(settings_file()), exist_ok=True)
        folder = Gio.File.new_for_path(os.path.dirname(settings_file()))
        self.settings_monitor = folder.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.settings_monitor.connect("changed", self._settings_changed)

    def _settings_changed(self, _monitor, file, _other, event):
        if file.get_path() != settings_file() or event not in (Gio.FileMonitorEvent.CREATED,
                                                                Gio.FileMonitorEvent.CHANGES_DONE_HINT):
            return
        from . import autopause

        if autopause.wanted() and not self.autopause:
            self.autopause = autopause.AutoPause(lambda: self.players)
            self.autopause.start()
        elif not autopause.wanted() and self.autopause:
            self.autopause.stop()
            self.autopause = None
        from .service import refresh_all

        refresh_all()  # a phone switched off or on, or kept to its cable, in Handset's window
        self._mount_phones(self.phones_now)  # the phones' folders switched on or off
        if notifications_wanted() != self.notifying:
            # Notifications reach every phone's followers: this sync starts again, as it is now.
            debug("notifications switched", "on" if not self.notifying else "off", "- the sync starts again")
            GLib.idle_add(self._start_again)

    def _start_again(self):
        self.stop()
        os.execv(sys.executable, [sys.executable] + sys.argv)

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
        self._iphones_here(any(p.platform == "ios" for p in self.online.values()))
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
        was, playing = bool(player.now and player.now.playing), bool(now and now.playing)
        player.show(now)
        if self.autopause and playing and not was:
            self.autopause.phone_started(phone_id)
        elif self.autopause and was and not playing:
            self.autopause.phone_stopped(phone_id)  # what paused for it plays again

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
        self.phones_now = phones
        if self.reconnect:
            self.reconnect.check()
        self._phone_entries(phones)
        self._mount_phones(phones)
        iphones = [p.id for p in phones if p.platform == "ios"]
        if self.ancs and len(iphones) == 1 and self.ancs.phone_id != iphones[0]:
            # Its notes and what it plays go with its apps' entries.
            player = self.players.pop(self.ancs.phone_id, None)
            if player:
                player.stop()
            self.ancs.phone_id = iphones[0]
            if self.ancs.media:
                self._iphone_media(self.ancs.media.shown)
        from .service import DISCONNECTED

        iphones = [p for p in phones if p.platform == "ios"]
        # Switched off in Handset: its notifications stop at once, not after a while.
        self._iphones_here(any(p.online for p in iphones),
                           at_once=bool(iphones) and all(p.problem == DISCONNECTED for p in iphones))
        gone = set(self.online) - set(online)
        came = set(online) - set(self.online)
        first, self.seen = not self.seen, True
        self.online = online
        if gone or came or first:
            debug("phones connected:", sorted(p.name for p in online.values()))
            self._drop_unconnected()
        for phone_id in gone:
            self.listed.pop(phone_id, None)  # listed afresh when it's back
            self.packages.pop(phone_id, None)
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

    # ---- The phones' folders (phonefs.py) --------------------------------------------------------

    def _mount_phones(self, phones: list[Phone]):
        """Each connected phone's files as a folder here (~/Phones/NAME), while it's connected."""
        from .finder import UNTRUSTED

        # Connected (a phone's service may say more of it: not on Wi-Fi, say), and trusting this computer.
        wanted = {p.id: p for p in phones if p.online and p.problem != UNTRUSTED} if phone_files_wanted() else {}
        for phone_id in [p for p in self.mounts if p not in wanted]:
            threading.Thread(target=self.mounts.pop(phone_id).stop, daemon=True).start()
        for phone in wanted.values():
            if phone.id not in self.mounts and phone.id not in self.mounting:
                self.mounting.add(phone.id)
                threading.Thread(target=self._mount, args=(phone,), daemon=True).start()

    def _mount(self, phone: Phone):
        from . import android, iphone, phonefs

        backend = None
        if phone.platform == "ios":
            link = iphone.open_link(phone.id, phone.address, phone.model)
            backend = phonefs.IPhone(link) if isinstance(link, iphone.Link) else None
        else:
            serial = android.find_serial(phone.id, phone.address, phone.port)
            backend = phonefs.Android(serial) if serial else None
        mounted = None
        if backend:
            cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
            mounted = phonefs.FileSystem(backend, os.path.join(phonefs.phones_folder(), phonefs.folder_name(phone.name)),
                                         os.path.join(cache, "handset", "files", phonefs.cache_name(phone.id)))
            problem = mounted.start()
            if problem:
                debug(phone.name, "has no folder here:", problem)
                mounted = None
            else:
                debug(phone.name, "is a folder here:", mounted.mountpoint)
        GLib.idle_add(self._mounted, phone.id, mounted)

    def _mounted(self, phone_id: str, mounted):
        self.mounting.discard(phone_id)
        if mounted is None:
            return GLib.SOURCE_REMOVE
        if phone_id not in self.online or not phone_files_wanted():  # gone meanwhile
            threading.Thread(target=mounted.stop, daemon=True).start()
        else:
            self.mounts[phone_id] = mounted
            if self.search:
                self.search.phone_here(phone_id)
        return GLib.SOURCE_REMOVE

    def to_reconnect(self) -> set[str]:
        """The Android phones Handset uses that aren't connected now (by their serial): not the
        ones switched off in Handset, nor those kept to their cable."""
        from .service import CABLE_ONLY, DISCONNECTED

        return {p.id for p in self.phones_now
                if p.platform == "android" and not p.online and p.problem not in (DISCONNECTED, CABLE_ONLY)}

    def searchable(self) -> dict:
        """The phones whose folders are here, for the desktop's search: each one's id → its
        name, its folder, and how to list all its files."""
        from .phonesearch import lister

        return {phone_id: (self.online[phone_id].name if phone_id in self.online else "Phone", mounted.mountpoint,
                           lister(mounted.phone))
                for phone_id, mounted in self.mounts.items()}

    def _phone_entries(self, phones: list[Phone]):
        """Each phone an entry of its own, opening its window (so the dock shows each under its own
        icon); a phone the service no longer knows goes."""
        try:
            for phone in phones:
                apps.write_phone_entry(phone.id, phone.name, phone.platform)
            for phone_id in set(apps.phone_entries()) - {p.id for p in phones}:
                apps.remove_phone_entry(phone_id)
        except (OSError, GLib.Error) as error:
            debug("the phones' entries weren't written:", error)

    def _iphones_here(self, here: bool, at_once: bool = False):
        """An iPhone's notifications only while it's connected by cable or Wi-Fi: on as soon as one
        is, off once none has been for a while (a moment's drop, a phone waking, doesn't count), or
        at once (it was switched off in Handset)."""
        if not self.ancs:
            return
        if here or at_once:
            if self.away_timer:
                GLib.source_remove(self.away_timer)
                self.away_timer = 0
            self.ancs.allow(here)
        elif self.ancs.allowed and not self.away_timer:
            self.away_timer = GLib.timeout_add_seconds(AWAY_FOR_S, self._iphones_away)

    def _iphones_away(self):
        self.away_timer = 0
        if self.ancs:
            self.ancs.allow(False)
        return GLib.SOURCE_REMOVE

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
        # Listing its apps costs the phone a while (scrcpy's server, every app's name): only when
        # its packages changed since (a quick list of them, with their versions).
        versions = android.app_versions(serial)
        if versions and versions == self.packages.get(phone.id):
            return True
        listed = android.list_apps(serial)
        if isinstance(listed, str):
            debug(phone.name, "didn't list its apps:", listed)
            return False
        folder = apps.cache_dir(phone.id)
        os.makedirs(folder, exist_ok=True)
        found = [PhoneApp(phone.id, phone.name, "android", package, name) for name, package in listed]
        self._write(phone.id, [(app, read_file(apps.cached_icon(folder, app.app))) for app in found], whole=True)
        self.packages[phone.id] = versions
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
    """Every phone app's entry and icon gone, and out of the dock (when Handset goes); the phones'
    own entries too."""
    for app in apps.entries():
        apps.unpin(app.phone_id, app.app)
        apps.remove_entry(app.phone_id, app.app)
    for phone_id in apps.phone_entries():
        apps.dock(f"{apps.phone_app_id(phone_id)}.desktop", False)
        apps.remove_phone_entry(phone_id)


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
    from .phonesearch import PhoneSearch

    sync.search = PhoneSearch(sync.searchable, lambda: sync.reconnect and sync.reconnect.now())
    sync.search.start()
    from .adbwifi import WifiReconnect

    sync.reconnect = WifiReconnect(sync.to_reconnect)
    sync.reconnect.start()
    loop.run()
    sync.stop()
    held.close()
    return 0
