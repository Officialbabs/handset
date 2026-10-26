"""Phone Screen Off: an Android phone's own screen dark while Handset shows and works it
(android.ScreenOff), switched from its windows (the bezel's button and menu, Ctrl+Shift+O). The
phone keeps its own screen-off time: worked from here it stays awake, left alone it sleeps. One
switch per phone, shown alike in all its windows.

It isn't remembered: the screen comes back on when it's switched off, when no window shows the
phone any more, when its link goes, and when Handset quits; scrcpy's server puts it back too if
Handset itself is stopped. A phone that sleeps (its power button, its screen-off time) has its
screen its own again, dark until it wakes. A phone is never left dark while it's awake.
"""

from __future__ import annotations

import threading

from .log import debug


class ScreenSwitch:
    def __init__(self, app):
        self.app = app
        self.links: dict[str, object] = {}  # phone id → what keeps its screen off

    def is_on(self, phone_id: str) -> bool:
        return phone_id in self.links

    def switch(self, window, on: bool):
        """The window's phone's own screen off, or on again."""
        phone = window.phone
        if not phone or phone.platform != "android":
            return
        if not on:
            self._stop(phone.id)
            return
        if phone.id in self.links:
            return
        serial = window._serial()
        if not serial:
            window.say(f"{phone.name} isn't connected")
            self._shown(phone.id, False)
            return
        from .android import ScreenOff

        link = ScreenOff(serial, lambda why, p=phone.id: self._ended(p, why))
        self.links[phone.id] = link
        link.start()
        self._shown(phone.id, True)
        debug(phone.name, "'s own screen goes off")

    def _shown(self, phone_id: str, on: bool):
        """Every window showing the phone shows the switch as it is."""
        for window in self.app.handset_windows():
            if window.phone and window.phone.id == phone_id:
                window.screen_off_switched(on)

    def _ended(self, phone_id: str, why: str):
        """It ended by itself (the link went, or it never started): said in the phone's window."""
        if self.links.pop(phone_id, None) is None:
            return
        self._shown(phone_id, False)
        window = next((w for w in self.app.handset_windows() if w.phone and w.phone.id == phone_id), None)
        if window:
            window.say(why)

    def _stop(self, phone_id: str, wait: bool = False, light: bool = True):
        link = self.links.pop(phone_id, None)
        if link is None:
            return
        self._shown(phone_id, False)
        debug(phone_id, "'s own screen goes on again" if light else "is asleep: its screen is its own again")
        if wait:
            link.stop(light)
        else:
            threading.Thread(target=link.stop, args=(light,), daemon=True).start()  # it waits on the phone a moment

    def asleep(self, phone_id: str):
        """The phone went to sleep (its power button, or its own screen-off time): its screen is its
        own again, and stays dark."""
        self._stop(phone_id, light=False)

    def release(self, showing: set[str]):
        """The phones no window shows any more: their screens on again."""
        for phone_id in [p for p in self.links if p not in showing]:
            self._stop(phone_id)

    def stop(self):
        """Handset quitting: every phone's screen on again before it goes."""
        for phone_id in list(self.links):
            self._stop(phone_id, wait=True)
