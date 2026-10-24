"""The phone's notifications on the desk, as the desk's own: under the app's name and icon (its
entry, apps.py), opening the app in a window of its own when clicked, and gone from here when
they go on the phone. The sync (appsync.py) runs them for each connected phone.

An Android phone says each notification as it's posted in its event log (notification_enqueue,
notification_canceled), which Handset follows over adb; the notification itself (its title and
text) comes from `dumpsys notification`, that one record only, read on the phone. Nothing is
installed on the phone. Shown are the ones that would sound or pop up there: not the ongoing
ones (music, a download), nor group summaries, silent ones or ones Do Not Disturb holds back.
An iPhone's come over Bluetooth (ancs.py).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio, GLib

from . import apps
from .log import debug

FLAG_ONGOING_EVENT = 0x2
FLAG_FOREGROUND_SERVICE = 0x40
FLAG_GROUP_SUMMARY = 0x200
IMPORTANCE_DEFAULT = 3
# Enqueue statuses: posted anew, updated, or not posted at all.
NEW, UPDATE, IGNORED = 0, 1, 2
RECORD_TRIES = 8  # a quarter of a second apart

EVENT = re.compile(r"^\s*(\d+\.\d+)\s+\d+\s+\d+\s+\w\s+(notification_enqueue|notification_canceled)\s*:\s*\[(.*)\]\s*$")


@dataclass
class Note:
    """One notification, as the desk shows it."""

    phone_id: str
    key: str  # the phone's own name for it
    app: str  # package or bundle id
    title: str
    text: str
    urgent: bool = False
    actions: tuple[tuple[str, str], ...] = ()  # (id, label): the phone's own, besides opening the app


# ---- Android: its event log and its records ---------------------------------------------------

def parse_event(line: str) -> tuple[float, str, str, int] | None:
    """(when, "posted"/"gone", key, status) from an event log line (`logcat -v epoch`)."""
    match = EVENT.match(line)
    if not match:
        return None
    when, tag, body = float(match[1]), match[2], match[3]
    if tag == "notification_canceled":
        return when, "gone", body.split(",", 1)[0], 0
    # uid, pid, pkg, id, tag, userid, then the notification itself (commas and all), status, by app
    head = body.split(",", 6)
    tail = body.rsplit(",", 2)
    if len(head) < 7 or len(tail) < 3:
        return None
    uid, _pid, pkg, number, tag_name, user = head[:6]
    try:
        status = int(tail[1])
    except ValueError:
        status = NEW
    key = f"{user}|{pkg}|{number}|{'null' if tag_name == 'NULL' else tag_name}|{uid}"
    return when, "posted", key, status


def record_command(key: str) -> str:
    """The shell command that prints just this notification's record from `dumpsys notification`."""
    script = ('index($0, "NotificationRecord(") && index($0, "key=" k ":") {on = 1; print; next} '
              'on && index($0, "NotificationRecord(") {exit} on {print}')
    return f"dumpsys notification --noredact | awk -v k={shlex.quote(key)} {shlex.quote(script)}"


VALUE = re.compile(r"^\s+(android\.[\w.]+)=(?:null|\w+ \((.*))$")


def parse_record(text: str) -> dict | None:
    """What a notification record says: its package, flags, importance, whether Do Not Disturb
    holds it back, and its words (android.title, android.text, …)."""
    first = next((line for line in text.splitlines() if "NotificationRecord(" in line), None)
    if not first:
        return None
    found = {"pkg": "", "flags": 0, "importance": IMPORTANCE_DEFAULT, "intercepted": False, "updated": 0.0,
             "extras": {}}
    pkg = re.search(r"pkg=(\S+)", first)
    importance = re.search(r"importance=(-?\d+)", first)
    found["pkg"] = pkg[1] if pkg else ""
    if importance:
        found["importance"] = int(importance[1])
    open_name, open_value = None, ""
    for line in text.splitlines()[1:]:
        if open_name:
            # A value over several lines (a message with line breaks) runs until its ")".
            if line.rstrip().endswith(")") and not VALUE.match(line):
                found["extras"][open_name] = open_value + "\n" + line.rstrip()[:-1]
                open_name = None
                continue
            if VALUE.match(line) or line.strip() == "}":
                found["extras"][open_name] = open_value
                open_name = None
            else:
                open_value += "\n" + line.rstrip()
                continue
        stripped = line.strip()
        if stripped.startswith("flags=") and not found.get("flags_seen"):
            try:
                found["flags"] = int(stripped.split("=", 1)[1], 0)
                found["flags_seen"] = True
            except ValueError:
                pass
        elif stripped.startswith("mIntercept="):
            found["intercepted"] = stripped.endswith("true")
        elif stripped.startswith("mUpdateTimeMs="):
            try:
                found["updated"] = int(stripped.split("=", 1)[1]) / 1000
            except ValueError:
                pass
        match = VALUE.match(line)
        if match and match[2] is not None:
            value = match[2]
            if value.endswith(")"):
                found["extras"][match[1]] = value[:-1]
            else:
                open_name, open_value = match[1], value
    found.pop("flags_seen", None)
    return found


def worth_showing(record: dict) -> bool:
    """Whether the phone would sound or pop up for it: the ones the desk shows."""
    if record["flags"] & (FLAG_ONGOING_EVENT | FLAG_FOREGROUND_SERVICE | FLAG_GROUP_SUMMARY):
        return False
    if record["importance"] < IMPORTANCE_DEFAULT or record["intercepted"]:
        return False
    extras = record["extras"]
    return bool(extras.get("android.title") or extras.get("android.text"))


def note_from(phone_id: str, key: str, record: dict) -> Note:
    extras = record["extras"]
    title = extras.get("android.title") or extras.get("android.conversationTitle") or ""
    text = extras.get("android.bigText") or extras.get("android.text") or extras.get("android.subText") or ""
    return Note(phone_id, key, record["pkg"], title.strip(), text.strip(), urgent=record["importance"] > 4)


class AndroidNotifications:
    """One Android phone's notifications as they're posted and go: `on_note(note)`,
    `on_gone(phone_id, key)`, on GLib's main loop."""

    def __init__(self, phone_id: str, serial: str, on_note: Callable[[Note], None],
                 on_gone: Callable[[str, str], None]):
        self.phone_id = phone_id
        self.serial = serial
        self.on_note = on_note
        self.on_gone = on_gone
        self.process: subprocess.Popen | None = None
        self.stopped = False
        self.shown: dict[str, tuple[str, str]] = {}  # key → (title, text) as last shown

    def start(self):
        threading.Thread(target=self._follow, daemon=True).start()

    def stop(self):
        self.stopped = True
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()
            process.wait()

    def _follow(self):
        from .android import adb_command, shell

        while not self.stopped:
            try:
                since = float(shell(self.serial, "date +%s.%N", timeout=5).strip() or 0)
            except (OSError, subprocess.SubprocessError, ValueError):
                since = 0.0
            command = adb_command(self.serial, "shell", "logcat -b events -v epoch -T 1 "
                                                        "-s notification_enqueue notification_canceled")
            try:
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                text=True, errors="replace")
            except OSError as error:
                debug("notifications of", self.phone_id, "can't be followed:", error)
                return
            for line in self.process.stdout:
                event = parse_event(line)
                if self.stopped:
                    return
                if not event or event[0] < since:
                    continue  # one from before
                when, kind, key, status = event
                if kind == "gone":
                    if self.shown.pop(key, None):
                        GLib.idle_add(self.on_gone, self.phone_id, key)
                elif status != IGNORED:
                    self._posted(key, when)
            if not self.stopped:
                time.sleep(3)  # the link dropped: follow again once it's back

    def _posted(self, key: str, when: float):
        """The notification as it is once posted: the event comes a moment before its record
        has it (a new one isn't there at all yet), so it's read again until the record is as new."""
        from .android import shell

        record = None
        for _ in range(RECORD_TRIES):
            try:
                record = parse_record(shell(self.serial, record_command(key), timeout=15))
            except (OSError, subprocess.SubprocessError):
                return
            if record and record["updated"] >= when - 0.05:
                break
            record = None
            time.sleep(0.25)
        if not record or not worth_showing(record):
            return
        note = note_from(self.phone_id, key, record)
        if self.shown.get(key) == (note.title, note.text):
            return  # an update that changed nothing said
        self.shown[key] = (note.title, note.text)
        GLib.idle_add(self.on_note, note)


# ---- The desk's notifications ------------------------------------------------------------------

class Desk:
    """Notifications on the desk, through its notification service: each under the phone app's
    own entry (its name and icon), and opening that app when clicked."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.ids: dict[tuple[str, str], int] = {}  # (phone, key) → the desk's id for it
        self.notes: dict[int, Note] = {}
        self.on_action: Callable[[Note, str], None] | None = None
        for signal, handler in (("ActionInvoked", self._action), ("NotificationClosed", self._closed)):
            self.bus.signal_subscribe("org.freedesktop.Notifications", "org.freedesktop.Notifications", signal,
                                      "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE, handler)

    def show(self, note: Note, phone_name: str, app_name: str | None = None):
        app = apps.find(note.phone_id, note.app)
        named = apps.named_icon(note.phone_id, note.app) if app else None
        icons = [named] if named else []
        name = app.name if app else (app_name or note.app)
        actions = ["default", "Open"]
        for action_id, label in note.actions:
            actions += [action_id, label]
        hints = {"urgency": GLib.Variant("y", 2 if note.urgent else 1)}
        if app:
            hints["desktop-entry"] = GLib.Variant("s", app.id)
        if icons:
            hints["image-path"] = GLib.Variant("s", icons[0])
        replaces = self.ids.get((note.phone_id, note.key), 0)
        summary = note.title or name
        body = GLib.markup_escape_text(note.text if note.title else "")
        try:
            reply = self.bus.call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                "Notify", GLib.Variant("(susssasa{sv}i)", (f"{name} ({phone_name})", replaces,
                                                           icons[0] if icons else "phone-symbolic",
                                                           summary, body, actions, hints, -1)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as error:
            debug("the desk didn't take a notification:", error.message)
            return
        number = reply.unpack()[0]
        self.ids[(note.phone_id, note.key)] = number
        self.notes[number] = note

    def close(self, phone_id: str, key: str):
        number = self.ids.pop((phone_id, key), None)
        if number is None:
            return
        self.notes.pop(number, None)
        self.bus.call("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                      "org.freedesktop.Notifications", "CloseNotification", GLib.Variant("(u)", (number,)),
                      None, Gio.DBusCallFlags.NONE, 5000, None, None)

    def _action(self, _bus, _sender, _path, _interface, _signal, parameters):
        number, action = parameters.unpack()
        note = self.notes.get(number)
        if note and self.on_action:
            self.on_action(note, action)

    def _closed(self, _bus, _sender, _path, _interface, _signal, parameters):
        number = parameters.unpack()[0]
        note = self.notes.pop(number, None)
        if note:
            self.ids.pop((note.phone_id, note.key), None)


def open_app(note: Note, own_window: bool = True):
    """The app the notification came from, in a window of its own; or the phone's window, for an
    app Handset hasn't listed (one the phone's home screen or drawer doesn't show)."""
    apps.open_window(note.phone_id, note.app if own_window else None)
