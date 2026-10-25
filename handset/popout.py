"""Apps an app opens, in windows of their own. An Android app's window shows a display of its own
on the phone (android.AppScreen); an app that opens another (a video's link opening the video
app, a mail's attachment opening the files app) opens it on that display, in that window. The
phone's window manager says so in its event log (`wm_create_task`, `wm_task_moved`,
`wm_task_to_front`, `wm_task_removed`, each with the display), so one `logcat` per phone is
followed: when an app's window's display has another app's task on top, the task moves to a
display of its own (`am display move-stack`), in a window of its own; or to the window that
app has already. What an app opens in its own task (a page in its browser tab, a picker) can't
be parted from it, and stays.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import GLib

from .log import debug

EVENTS = ("wm_create_task", "wm_task_moved", "wm_task_to_front", "wm_task_removed")
# The phone's own screens over another app (choosers, permission questions, installers, sign-in
# sheets): they belong to the app that asked, in its window.
STAYS = frozenset({
    "android", "com.android.intentresolver", "com.android.systemui", "com.android.shell",
    "com.android.permissioncontroller", "com.google.android.permissioncontroller",
    "com.android.packageinstaller", "com.google.android.packageinstaller",
    "com.google.android.gms", "com.google.android.gsf",
})
SETTLE_MS = 200  # a launch is a few events: looked at once they're in
EMPTY_AFTER_MS = 800  # a display without tasks this long has closed its app (not one handing to the next)
MOVING_S = 10  # a task on its way to another window isn't sent again while it's still where it was


@dataclass(frozen=True)
class RootTask:
    """One task on a phone: `package` is the app it was started for (its first activity's), the
    one it stays with whatever it shows on top."""

    id: int
    display: int
    package: str
    standard: bool  # an app's own, as big as its display (not the home screen, not in a corner)


def parse_tasks(text: str) -> list[RootTask]:
    """`am stack list`: each display's tasks, topmost first."""
    found = []
    for block in re.split(r"^RootTask ", text, flags=re.M)[1:]:
        head = re.match(r"id=(\d+) .*?displayId=(\d+)", block)
        base = re.search(r"^\s+taskId=\d+: ([\w.]+)/", block, re.M)
        if not head or not base:
            continue
        standard = "mActivityType=standard" in block and "mWindowingMode=fullscreen" in block
        found.append(RootTask(int(head.group(1)), int(head.group(2)), base.group(1), standard))
    return found


def parse_event(line: str) -> tuple[str, int] | None:
    """A window manager event (`logcat -v tag`) as (tag, display), for the ones that say one."""
    match = re.match(r"\s*\w/(wm_\w+)\s*:\s*\[([^\]]*)", line)
    if not match or match.group(1) not in EVENTS:
        return None
    fields = match.group(2).split(",")
    # wm_create_task [user, task, root, display]; wm_task_moved [task, root, display, top, index];
    # wm_task_to_front [user, task, display]; wm_task_removed [task, root, display, reason].
    at = {"wm_create_task": 3, "wm_task_moved": 2, "wm_task_to_front": 2, "wm_task_removed": 2}[match.group(1)]
    try:
        return match.group(1), int(fields[at])
    except (IndexError, ValueError):
        return None


def to_part(tasks: list[RootTask], display: int, package: str) -> RootTask | None:
    """Another app's task on top of `package`'s display, to go to a window of its own."""
    top = next((t for t in tasks if t.display == display), None)
    if top and top.package != package and top.standard and top.package not in STAYS:
        return top
    return None


class TaskWatch:
    """One phone's tasks, as its apps' windows show them. `displays` are the apps' windows'
    displays (display → the app it's for). `on_part(display, task)`: another app came on top
    in that window. `on_empty(display)`: that window's display has no app left."""

    def __init__(self, serial: str, on_part: Callable[[int, RootTask], None], on_empty: Callable[[int], None],
                 adb: Callable[..., list[str]]):
        self.serial = serial
        self.on_part = on_part
        self.on_empty = on_empty
        self.adb = adb  # android.adb_command
        self.displays: dict[int, str] = {}
        self.moving: dict[int, tuple[int, float]] = {}  # task → the display it was sent from, and when
        self.emptied: set[int] = set()
        self.process: subprocess.Popen | None = None
        self.settle = 0
        self.looking = False
        self.again = False

    def watch(self, display: int, package: str):
        self.displays[display] = package
        if not self.process:
            self._start()

    def forget(self, display: int):
        self.displays.pop(display, None)
        if not self.displays:
            self.stop()

    def stop(self):
        process, self.process = self.process, None
        if process:
            process.kill()
        if self.settle:
            GLib.source_remove(self.settle)
            self.settle = 0


    def _start(self):
        command = self.adb(self.serial, "shell", "logcat -b events -v tag -T 1 "
                           + " ".join(f"{tag}:I" for tag in EVENTS) + " '*:S'")
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as error:
            debug("no task watch:", error)
            return
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process: subprocess.Popen):
        for line in process.stdout:
            event = parse_event(line)
            if event:
                GLib.idle_add(self._event, *event)
        process.stdout.close()
        process.wait()
        if process is self.process:
            debug("the task watch ended; again")
            self.process = None
            GLib.timeout_add_seconds(2, self._restart)

    def _restart(self):
        if self.displays and not self.process:
            self._start()
        return GLib.SOURCE_REMOVE

    def _event(self, tag: str, display: int):
        if display not in self.displays:
            return GLib.SOURCE_REMOVE
        if tag == "wm_task_removed":
            self.emptied.add(display)
        if not self.settle:
            self.settle = GLib.timeout_add(EMPTY_AFTER_MS if tag == "wm_task_removed" else SETTLE_MS, self._look)
        return GLib.SOURCE_REMOVE

    def _look(self):
        self.settle = 0
        if self.looking:
            self.again = True
            return GLib.SOURCE_REMOVE
        self.looking = True
        threading.Thread(target=self._list, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _list(self):
        try:
            text = subprocess.run(self.adb(self.serial, "shell", "am stack list"), capture_output=True,
                                  text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            text = None
        GLib.idle_add(self._listed, text)

    def _listed(self, text: str | None):
        self.looking = False
        if self.again:
            self.again = False
            self._look()
        if text is None or not self.process:
            return GLib.SOURCE_REMOVE
        tasks = parse_tasks(text)
        now = GLib.get_monotonic_time() / 1e6
        where = {task.id: task.display for task in tasks}
        # Gone from where it was sent from (arrived, or closed), it's like any other again.
        self.moving = {task: (source, at) for task, (source, at) in self.moving.items()
                       if where.get(task) == source and now - at < MOVING_S}
        for display, package in list(self.displays.items()):
            task = to_part(tasks, display, package)
            if task and task.id not in self.moving:
                self.moving[task.id] = (display, now)
                debug(task.package, "came on top of", package, "on display", display, "(task", task.id, ")")
                self.on_part(display, task)
        emptied, self.emptied = self.emptied, set()
        for display in emptied:
            if display in self.displays and not any(t.display == display for t in tasks):
                self.on_empty(display)
        return GLib.SOURCE_REMOVE
