"""Play Sound Here: a phone's sound on this computer instead of on the phone, switched on and off
from its window (the bezel's menu), and remembered for next time ("sound_here" in Handset's
settings). An Android phone's comes over adb (android.SoundLink: scrcpy's server capturing its
media sound, the phone silent meanwhile); an iPhone's over Bluetooth, its calls too
(bluetooth.BluetoothSound). Each phone's plays while a window shows it.
"""

from __future__ import annotations

from .log import debug


class SoundSwitch:
    def __init__(self, app):
        self.app = app
        self.links: dict[str, object] = {}  # phone id → what plays its sound here

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
        if on:
            self.resume(window)
        else:
            self._stop(phone.id)

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
        self._stop(phone_id)
        if window.phone and window.phone.id == phone_id:
            window.say(why)
            window.sound_switched(False)
        from .window import save_settings

        self.app.settings["sound_here"] = [p for p in self.wanted() if p != phone_id]
        save_settings(self.app.settings)

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
