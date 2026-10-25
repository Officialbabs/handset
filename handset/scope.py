"""Handset's own systemd scope. Desktops start each app in a transient unit named after the entry
it was started from (Ada: app-ada-<entry>-<pid>.scope, GNOME: app-gnome-…, KDE:
app-<entry>@<random>.service), and count the unit's processes, memory and network use as that
app's. Handset is often started from a phone app's entry (YouTube Music's, from the dock), and
everything it then does, its iPhone's picture over Wi-Fi included, would be counted as that
app's. So the main Handset moves itself into a scope of its own, app-<Handset's id>-<pid>.scope,
unless its launcher already made one; its helpers, started after, follow it.
"""

from __future__ import annotations

import os
import re
import time

from gi.repository import Gio, GLib

from . import APP_ID, NAME
from .log import debug

SYSTEMD = "org.freedesktop.systemd1"


def own_unit(name: str, app_id: str = APP_ID) -> bool:
    """Whether a cgroup's name is a unit a launcher made for Handset itself: the app id standing
    whole (io.github.Officialbabs.Handset.App.p… is a phone app's, not Handset's)."""
    name = name.replace("\\x2d", "-")  # systemd escapes "-" inside the app id
    if not name.startswith("app-"):
        return False
    for suffix in (".scope", ".service"):
        if name.endswith(suffix):
            return bool(re.search(rf"-{re.escape(app_id)}(?:[-@]|$)", name[: -len(suffix)]))
    return False


def current_unit() -> str:
    """The last part of this process's cgroup (cgroup v2): its unit."""
    try:
        with open("/proc/self/cgroup") as file:
            line = next((line for line in file if line.startswith("0::")), "")
    except OSError:
        return ""
    return line.strip().rsplit("/", 1)[-1]


def scope_name(pid: int) -> str:
    """The XDG name for an app's scope: app-<app id>-<pid>.scope, a "-" in the id escaped."""
    return f"app-{APP_ID.replace('-', chr(92) + 'x2d')}-{pid}.scope"


def move_to_own_scope(timeout: float = 0.5) -> str | None:
    """Handset in a scope of its own, unless it has one already, runs in a sandbox (whose
    launcher made one) or there's no systemd user manager. The scope's name, or None."""
    if os.path.exists("/.flatpak-info") or own_unit(current_unit()):
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        name = scope_name(os.getpid())
        properties = [("PIDs", GLib.Variant("au", [os.getpid()])), ("Description", GLib.Variant("s", NAME)),
                      ("Slice", GLib.Variant("s", "app.slice")),
                      ("CollectMode", GLib.Variant("s", "inactive-or-failed"))]
        bus.call_sync(SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "StartTransientUnit",
                      GLib.Variant("(ssa(sv)a(sa(sv)))", (name, "fail", properties, [])), GLib.VariantType("(o)"),
                      Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as error:
        debug("no scope of its own:", error.message)
        return None
    # systemd moves the process once the job runs: waited for, so helpers started next follow.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not own_unit(current_unit()):
        time.sleep(0.02)
    debug("Handset runs in", name)
    return name
