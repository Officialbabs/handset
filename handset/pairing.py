"""Setting a phone up where the desktop has no phone service to do it (finder.py finds them).

An Android phone by its cable needs only USB debugging allowed on it; over Wi-Fi it's paired
once with Wireless debugging's pairing code (`adb pair`), then connected (`adb connect`) each
time it's back: its pairing and connecting services are found on the network through Avahi, or
typed from what the phone shows.

An iPhone is set up by its cable, a step at a time, through Handset's helper (`handset-ios setup
STEP`): this computer trusted (the phone asks), Developer Mode on (the phone restarts), Apple's
developer disk image (the screen's input service lives in it, signed for that phone by Apple's
server) and, if wanted, a pairing for Wi-Fi.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Callable

from gi.repository import GLib

from .avahi import Browser
from .log import debug

PAIRING = "_adb-tls-pairing._tcp"
CONNECTING = "_adb-tls-connect._tcp"
LOOK_FOR_S = 30
CODE = re.compile(r"^\d{6}$")
ADDRESS = re.compile(r"^\s*([\w.:\[\]-]+?):(\d{2,5})\s*$")


def parse_address(text: str) -> tuple[str, int] | None:
    """"192.168.1.5:37123" (as the phone shows it) as (address, port)."""
    match = ADDRESS.match(text or "")
    return (match.group(1).strip("[]"), int(match.group(2))) if match else None


def paired(said: str) -> bool:
    """`adb pair` says "Successfully paired to …" when it worked (and exits 0 when it didn't)."""
    return "Successfully paired" in said


class AndroidWifi:
    """Pairing one Android phone over Wi-Fi, from the code its Wireless debugging shows.
    `on_step(words)` tells how it's going; `on_done(problem)` ends it (None: it's connected)."""

    def __init__(self, on_step: Callable[[str], None], on_done: Callable[[str | None], None],
                 adb: Callable[..., list[str]] | None = None):
        self.on_step = on_step
        self.on_done = on_done
        self.adb = adb
        self.pairing_at: tuple[str, int] | None = None  # the phone's pairing service, once found
        self.connecting: dict[str, tuple[str, int]] = {}  # address → its connect service
        self.browsers: list[Browser] = []
        self.done = False

    def _command(self, *args: str) -> list[str]:
        if self.adb:
            return self.adb(None, *args)
        from .android import adb_command

        return adb_command(None, *args)

    def look(self) -> bool:
        """Starts looking for the phone on the network; False when it can't (no Avahi): the
        address has to be typed."""
        pairing = Browser(PAIRING, self._pairing_found)
        connect = Browser(CONNECTING, self._connect_found)
        if not pairing.start():
            return False
        connect.start()
        self.browsers = [pairing, connect]
        return True

    def stop(self):
        self.done = True
        for browser in self.browsers:
            browser.stop()
        self.browsers = []

    def _pairing_found(self, _name: str, address: str, port: int):
        self.pairing_at = (address, port)
        debug("a phone pairing at", address, port)

    def _connect_found(self, _name: str, address: str, port: int):
        self.connecting[address] = (address, port)

    def pair(self, code: str, typed: str = ""):
        """Pairs with `code` at the typed address, or the one found."""
        code = code.strip().replace(" ", "")
        if not CODE.match(code):
            self.on_done("The code is the six digits the phone shows.")
            return
        at = parse_address(typed) if typed.strip() else self.pairing_at
        if at is None:
            self.on_done("Type the IP address and port the phone shows under the code." if typed.strip() or
                         not self.browsers else "The phone isn't on this Wi-Fi yet: keep its pairing code showing.")
            return
        self.on_step("Pairing…")
        threading.Thread(target=self._pair, args=(at, code), daemon=True).start()

    def _pair(self, at: tuple[str, int], code: str):
        address, port = at
        try:
            result = subprocess.run(self._command("pair", f"{address}:{port}", code), capture_output=True,
                                    text=True, timeout=30)
            said = result.stdout + result.stderr
        except (OSError, subprocess.SubprocessError) as error:
            said = str(error)
        if not paired(said):
            debug("adb pair:", said.strip())
            GLib.idle_add(self._finish, "The phone didn't pair: check the code, or pair again with a new one.")
            return
        GLib.idle_add(self.on_step, "Paired. Connecting…")
        # Its connect service (another port), found on the network or from adb's own look.
        for _ in range(LOOK_FOR_S * 2):
            if self.done:
                return
            found = self.connecting.get(address)
            if found:
                problem = connect(found, self._command)
                GLib.idle_add(self._finish, problem)
                return
            threading.Event().wait(0.5)
        GLib.idle_add(self._finish, "Paired, but the phone didn't show up to connect: keep Wireless debugging "
                                    "on, and type the IP address and port on its Wireless debugging page.")

    def connect_typed(self, typed: str):
        """Connects to the address Wireless debugging's page shows (once paired)."""
        at = parse_address(typed)
        if at is None:
            self.on_done("Type the IP address and port Wireless debugging shows.")
            return
        threading.Thread(target=lambda: GLib.idle_add(self._finish, connect(at, self._command)), daemon=True).start()

    def _finish(self, problem: str | None):
        if not self.done:
            self.stop()
            self.on_done(problem)
        return GLib.SOURCE_REMOVE


def connect(at: tuple[str, int], command: Callable[..., list[str]]) -> str | None:
    """`adb connect`; None once it's connected, else why not."""
    address, port = at
    try:
        result = subprocess.run(command("connect", f"{address}:{port}"), capture_output=True, text=True, timeout=20)
        said = result.stdout + result.stderr
    except (OSError, subprocess.SubprocessError) as error:
        said = str(error)
    if "connected to" in said and "failed" not in said.lower() and "cannot" not in said.lower():
        return None
    debug("adb connect:", said.strip())
    return "The phone didn't connect: keep Wireless debugging on, and try again."


def wifi_by_cable(serial: str) -> tuple[str, int] | str:
    """adb on Wi-Fi as well, turned on while the phone's plugged in (`adb tcpip`, which lasts
    until it restarts): where it's reachable, or why not."""
    from .android import adb, shell

    try:
        said = shell(serial, "ip -o -4 addr show wlan0", timeout=10)
    except (OSError, subprocess.SubprocessError):
        said = ""
    found = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", said)
    if not found:
        return "The phone isn't on Wi-Fi: join a network on it, and try again."
    address = found.group(1)
    try:
        adb(serial, "tcpip", "5555", timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return f"The phone didn't turn on Wi-Fi debugging ({error})."
    from .android import adb_command

    for _ in range(10):  # adb on the phone restarts to listen there: a moment
        threading.Event().wait(1)
        if connect((address, 5555), lambda *args: adb_command(None, *args)) is None:
            return address, 5555
    return "The phone didn't answer over Wi-Fi: are it and this computer on the same network?"


# ---- An iPhone, through the helper -----------------------------------------------------------------

def ddi_folder() -> str | None:
    """Apple's developer disk image, as kept here (Handset's download, or one already on this
    computer), or None."""
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    candidates = [os.path.join(cache, "handset", "ddi")]
    ada = os.path.join(cache, "ada", "phones")
    if os.path.isdir(ada):
        candidates += sorted(os.path.join(ada, name) for name in os.listdir(ada) if name.startswith("ddi-"))
    for folder in candidates:
        if all(os.path.isfile(os.path.join(folder, name)) for name in DDI_FILES):
            return folder
    return None


DDI_FILES = ("BuildManifest.plist", "Image.dmg", "Image.dmg.trustcache", "Image.dmg.cryptex_info",
             "Image.dmg.root_hash")
# Xcode's personalized developer disk image (iOS 17 and later), from a public copy of Xcode's at
# a fixed commit, each file checked against what it was then.
DDI_URL = ("https://raw.githubusercontent.com/doronz88/DeveloperDiskImage/f0a9fb8ef81df9217fb2d2bcb4e3351af903408b/"
           "PersonalizedImages/Xcode_iOS_DDI_Cryptex/")
DDI_SHA256 = {
    "BuildManifest.plist": "27385d7582b03b36bb3104e22b520aee0c47d72fecb4e8ecfe12ef5d966c7012",
    "Image.dmg": "873097f695a8b9734e2abc54f795a8874d40ff6fd11208ecb01ef29534c7c176",
    "Image.dmg.trustcache": "f7f21986074eee03a215aca16ecfc78d6bf183600d8a0d2fb691f9896782e6f0",
    "Image.dmg.cryptex_info": "edf49aef55aacc063d4d7be05b713bb545ce2993b3f62bcc15eccd75e610ee6c",
    "Image.dmg.root_hash": "3543fad2805b88119695c417e12679380b3b5a2742994bbcc839c8e2de5d7302",
}
DDI_SIZE_MB = 17


def fetch_ddi(on_step: Callable[[str], None], url: str | None = None) -> str | None:
    """The developer disk image, fetched into ~/.cache/handset/ddi and checked; None when it's
    there, else why not. HANDSET_DDI_URL fetches from elsewhere (the tests)."""
    import hashlib
    import urllib.request

    url = url or os.environ.get("HANDSET_DDI_URL") or DDI_URL
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    folder = os.path.join(cache, "handset", "ddi")
    os.makedirs(folder, exist_ok=True)
    for number, name in enumerate(DDI_FILES, 1):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and sha256(path) == DDI_SHA256[name]:
            continue
        on_step(f"Downloading {number} of {len(DDI_FILES)}…")
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(url + name, timeout=30) as reply, open(path + ".part", "wb") as file:
                for chunk in iter(lambda: reply.read(1 << 16), b""):
                    digest.update(chunk)
                    file.write(chunk)
        except (OSError, ValueError) as error:
            return f"It didn't download ({error}): check the internet connection, and try again."
        if digest.hexdigest() != DDI_SHA256[name]:
            os.remove(path + ".part")
            return f"The download wasn't the file it should be ({name}): nothing was kept."
        os.replace(path + ".part", path)
    return None


def sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class IPhoneStep:
    """One step of setting an iPhone up, run by the helper: `on_event(event)` for each thing it
    says ({"event": "waiting"|"done"|"error"|"status", "message": …}), on the main loop."""

    def __init__(self, helper: str, udid: str, step: str, on_event: Callable[[dict], None], extra: list[str] = ()):
        self.command = [helper, "setup", step, "--udid", udid, *extra]
        self.on_event = on_event
        self.process: subprocess.Popen | None = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()

    def _run(self):
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, text=True)
        except OSError as error:
            GLib.idle_add(self.on_event, {"event": "error", "message": str(error)})
            return
        self.process = process
        ended = False
        with process.stderr:
            for line in process.stderr:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    ended = ended or event.get("event") in ("done", "error")
                    GLib.idle_add(self.on_event, event)
        process.wait()
        if not ended and self.process is process:  # not stopped: it ended without a word
            GLib.idle_add(self.on_event, {"event": "error", "message": "Handset's helper stopped."})
