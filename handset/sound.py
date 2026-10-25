"""Play Sound Here: a phone's sound on this computer instead of on the phone, switched on and off
from its window (the bezel's menu), and remembered for next time ("sound_here" in Handset's
settings). An Android phone's comes over adb (android.SoundLink: scrcpy's server capturing its
media sound, the phone silent meanwhile); an iPhone's over Bluetooth, its calls too
(bluetooth.BluetoothSound). Each phone's plays while a window shows it.

The choice is kept until the person changes it: a phone whose sound can't come for now (out of
reach, its link gone) is said so once, and its sound is asked for again in a while, by the link
itself (Bluetooth's) or from here.
"""

from __future__ import annotations

from gi.repository import GLib

from .log import debug

# A phone whose sound didn't come is asked again after this long (its link doesn't keep trying).
RETRY_S = 20


class SoundSwitch:
    def __init__(self, app):
        self.app = app
        self.links: dict[str, object] = {}  # phone id → what plays its sound here
        self.told: set[str] = set()  # phones the window has said can't play here for now

    def wanted(self) -> list[str]:
        wanted = self.app.settings.get("sound_here")
        return wanted if isinstance(wanted, list) else []

    def is_on(self, phone_id: str) -> bool:
        return phone_id in self.wanted()

    def switch(self, window, on: bool):
        """The person's choice for the window's phone, kept, and carried out."""
        from .window import save_settings

        phone = window.phone
        if not phone:
            return
        wanted = [p for p in self.wanted() if p != phone.id] + ([phone.id] if on else [])
        self.app.settings["sound_here"] = wanted
        save_settings(self.app.settings)
        self.told.discard(phone.id)
        if on:
            self.resume(window)
        else:
            self._stop(phone.id)

    def choose(self, phone_id: str, on: bool, window=None):
        """The choice made away from the phone's window (Handset's phones window): kept, and
        carried out in `window` when it's the one showing the phone."""
        if window is not None and window.phone and window.phone.id == phone_id:
            self.switch(window, on)
            window.sound_switched(on)
            return
        from .window import save_settings

        self.app.settings["sound_here"] = [p for p in self.wanted() if p != phone_id] + ([phone_id] if on else [])
        save_settings(self.app.settings)

    def resume(self, window):
        """The window's phone's sound, here if the person wants it so (when its window opens)."""
        phone = window.phone
        if not phone or not self.is_on(phone.id) or phone.id in self.links:
            return
        link = self._link(window)
        if link is None:
            return
        self.links[phone.id] = link
        link.start()
        debug("the sound of", phone.name, "plays here")

    def _link(self, window):
        phone = window.phone
        ended = lambda why, w=window, p=phone.id: self._ended(w, p, why)  # noqa: E731
        if phone.platform == "ios":
            from .appsync import known_iphones
            from .bluetooth import BluetoothSound

            return BluetoothSound(phone.name, known_iphones(), ended)
        serial = window._serial()
        if not serial:
            return None  # opened later, once the phone is
        from .android import SoundLink

        return SoundLink(serial, ended)

    def _ended(self, window, phone_id: str, why: str):
        """The sound can't come for now: said once, and asked for again in a while. The choice
        stays as it was, the switch with it."""
        if phone_id not in self.told and window.phone and window.phone.id == phone_id:
            self.told.add(phone_id)
            window.say(why)
        link = self.links.get(phone_id)
        if link is not None and getattr(link, "keeps_trying", False):
            return
        self._stop(phone_id)
        GLib.timeout_add_seconds(RETRY_S, self._again, window, phone_id)

    def _again(self, window, phone_id: str):
        if window.phone and window.phone.id == phone_id and self.is_on(phone_id):
            self.resume(window)
        return GLib.SOURCE_REMOVE

    def _stop(self, phone_id: str):
        link = self.links.pop(phone_id, None)
        if link:
            link.stop()
            debug("the sound of", phone_id, "plays on the phone again")

    def release(self, showing: set[str]):
        """The phones no window shows any more: their sound goes back to them."""
        for phone_id in [p for p in self.links if p not in showing]:
            self._stop(phone_id)

    def stop(self):
        self.release(set())
