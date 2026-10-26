#!/usr/bin/env python3
"""A stand-in for adb in Handset's tests (HANDSET_ADB): one phone, FAKE7, whose screen is
GStreamer's test picture as H.264 and whose uinput and input commands are written down. It
keeps its state in FAKEADB_DIR: `rotation` (degrees, to turn the screen), `wakefulness` (Awake,
Dozing… as `dumpsys power` says; the wake key wakes it), `log.jsonl` (what it
was asked) and `uinput.jsonl` (the virtual touchscreen's commands). scrcpy's server is played too: listing apps
(APPS), and an app's own screen (new_display=…, its display 7, 8…): the test picture as its
video, every control message written to `control.jsonl`. `tasks` is what `am stack list` says,
and `am display move-stack` moves one there (saying so in `events`, the event log). `others`, if there, lists more
connections adb has, one per line: "<serial> <serial number> <seconds to answer getprop>" (another
phone, or FAKE7 over Wi-Fi); they answer nothing else. Never a real phone."""

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.environ.get("FAKEADB_DIR", ".")
SERIAL = "FAKE7"
NATURAL = (1080, 2400)
# The apps scrcpy's server lists: (name, package, the system's).
APPS = [("Settings", "com.android.settings", True), ("Fake Chat", "org.example.chat", False),
        ("A Very Long Application Name Indeed", "org.example.long", False)]
# Listed by scrcpy's server, but with no place in the app drawer (a system tool).
HIDDEN = ("Sound Thing", "com.example.hidden", True)


def log(entry):
    with open(os.path.join(HERE, "log.jsonl"), "a") as file:
        file.write(json.dumps(entry) + "\n")


def rotation():
    try:
        with open(os.path.join(HERE, "rotation")) as file:
            return int(file.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def wakefulness():
    try:
        with open(os.path.join(HERE, "wakefulness")) as file:
            return file.read().strip() or "Awake"
    except OSError:
        return "Awake"


def screenrecord(command):
    size = re.search(r"--size (\d+)x(\d+)", command)
    w, h = (int(size[1]), int(size[2])) if size else NATURAL
    pipeline = (f"videotestsrc is-live=true pattern=ball ! video/x-raw,width={w},height={h},framerate=30/1 "
                "! videoconvert ! openh264enc ! h264parse config-interval=-1 "
                "! video/x-h264,stream-format=byte-stream ! fdsink fd=1")
    os.execvp("gst-launch-1.0", ["gst-launch-1.0", "-q"] + pipeline.split())


def uinput():
    path = os.path.join(HERE, "uinput.jsonl")
    for line in sys.stdin:
        with open(path, "a") as file:
            file.write(line)
        try:
            command = json.loads(line)
        except ValueError:
            continue
        if command.get("command") == "sync":
            print(json.dumps({"id": command.get("id"), "result": "sync",
                              "syncToken": command.get("syncToken")}), flush=True)


def scrcpy(command):
    """scrcpy's server: its app list, or an app on a screen of its own."""
    options = dict(word.split("=", 1) for word in command.split()[4:] if "=" in word)
    if options.get("list_apps") == "true":
        print("[server] INFO: List of apps:")
        for name, package, system in APPS + [HIDDEN]:
            mark = "*" if system else "-"
            print(f" {mark} {name}\n   {package}" if len(name) >= 30 else f" {mark} {name:<30} {package}")
        return 0
    with open(os.path.join(HERE, f"forward_{options['scid']}")) as file:
        port = int(file.read())
    if options.get("video") == "false":
        return clipboard_server(port, options)
    width, height = (int(n) for n in options.get("new_display", f"{NATURAL[0]}x{NATURAL[1]}").split("/")[0].split("x"))
    server = socket.create_server(("127.0.0.1", port))
    video, _ = server.accept()
    video.sendall(b"\0" + struct.pack(">III", 0x68323634, width, height))
    control = server.accept()[0] if options.get("control") == "true" else None
    if "new_display" in options:
        print(f"[server] INFO: New display: {width}x{height}/425 (id={new_display()})", flush=True)
    pipeline = (f"videotestsrc is-live=true pattern=ball ! video/x-raw,width={width},height={height},framerate=10/1 "
                "! videoconvert ! openh264enc ! h264parse config-interval=-1 "
                "! video/x-h264,stream-format=byte-stream ! fdsink fd=1")
    encoders = []

    def encode():
        """The test picture, encoded from its start (so with its first keyframe)."""
        encoder = subprocess.Popen(["gst-launch-1.0", "-q"] + pipeline.split(), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        encoders.append(encoder)
        return encoder

    def send(encoder):
        try:
            for chunk in iter(lambda: encoder.stdout.read1(65536), b""):
                if encoder is not encoders[-1]:
                    break
                video.sendall(chunk)
        except OSError:
            pass

    if control is None:  # the phone's own screen: video only, until the window goes
        send(encode())
        encoders[-1].kill()
        return 0
    threading.Thread(target=send, args=(encode(),), daemon=True).start()
    sizes = {0: 14, 2: 32, 3: 21, 4: 2, 17: 1}

    def take(count):
        data = b""
        while len(data) < count:
            chunk = control.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    try:
        while True:
            kind = take(1)[0]
            if kind == 1:
                (length,) = struct.unpack(">I", take(4))
                entry = {"type": "text", "text": take(length).decode()}
            elif kind == 16:
                package = take(take(1)[0]).decode()
                entry = {"type": "start_app", "package": package}
                if package not in [app[1] for app in APPS]:
                    print(f"[server] WARN: No app found for package \"{package}\"", flush=True)
            elif kind in sizes:
                rest = take(sizes[kind] - 1)
                if kind == 2:
                    action, _pointer, x, y, w, h, _pressure, _b1, _b2 = struct.unpack(">BqiiHHHii", rest)
                    entry = {"type": "touch", "action": action, "x": x, "y": y, "size": [w, h]}
                elif kind == 3:
                    x, y, w, h, right, up, _buttons = struct.unpack(">iiHHhhi", rest)
                    entry = {"type": "scroll", "x": x, "y": y, "size": [w, h],
                             "right": right / 32768 * 16, "down": -up / 32768 * 16}
                elif kind == 0:
                    action, code, _repeat, _meta = struct.unpack(">Biii", rest)
                    entry = {"type": "key", "action": action, "code": code}
                elif kind == 17:
                    entry = {"type": "reset_video"}  # the video starts afresh, keyframe first
                    encoders[-1].kill()
                    threading.Thread(target=send, args=(encode(),), daemon=True).start()
                else:
                    entry = {"type": "back", "action": rest[0]}
            else:
                entry = {"type": "unknown", "kind": kind}
            with open(os.path.join(HERE, "control.jsonl"), "a") as file:
                file.write(json.dumps(entry) + "\n")
    except (EOFError, OSError):
        pass
    for encoder in encoders:
        encoder.kill()
    return 0


def new_display() -> int:
    """The next display's id (7, 8…): `displays` keeps the count."""
    path = os.path.join(HERE, "displays")
    try:
        with open(path) as file:
            made = int(file.read())
    except (OSError, ValueError):
        made = 6
    with open(path, "w") as file:
        file.write(str(made + 1))
    return made + 1


def move_task(task: str, display: str):
    """`am display move-stack`: the task in `tasks` (as `am stack list` says them) to the display,
    said in the event log as the phone says it."""
    path = os.path.join(HERE, "tasks")
    try:
        with open(path) as file:
            text = file.read()
    except OSError:
        text = ""
    moved, count = re.subn(rf"^(RootTask id={task} .*?displayId=)\d+", rf"\g<1>{display}", text, flags=re.M)
    if not count:
        print("Exception occurred while executing 'display':")
        print(f"java.lang.IllegalArgumentException: moveRootTaskToTaskDisplayArea: Unknown rootTaskId={task}")
        return 255
    with open(path, "w") as file:
        file.write(moved)
    with open(os.path.join(HERE, "events"), "a") as file:
        file.write(f"I/wm_task_moved: [{task},{task},{display},1,0]\n")
    return 0


def track():
    """`adb track-devices -l`: the list in FAKEADB_DIR/track (adb's lines, one per connection),
    framed as adb frames it, again each time it changes."""
    path = os.path.join(HERE, "track")
    said = None
    while True:
        try:
            with open(path) as file:
                listed = file.read()
        except OSError:
            listed = ""
        if listed != said:
            data = listed.encode()
            sys.stdout.buffer.write(b"%04x" % len(data) + data)
            sys.stdout.flush()
            said = listed
        time.sleep(0.05)


def events():
    """The phone's event log, followed: the lines put in FAKEADB_DIR/events, as they're put."""
    path = os.path.join(HERE, "events")
    at = 0
    while True:
        if os.path.exists(path):
            with open(path) as file:
                file.seek(at)
                for line in file:
                    print(line.rstrip("\n"), flush=True)
                at = file.tell()
        time.sleep(0.1)


def notification(command):
    """One notification's record (dumpsys, cut by awk on the phone): FAKEADB_DIR/records/<key>."""
    import shlex

    words = shlex.split(command)
    key = next((word[2:] for word in words if word.startswith("k=")), "")
    path = os.path.join(HERE, "records", key.replace("|", "_"))
    if os.path.exists(path):
        with open(path) as file:
            print(file.read(), end="")
    return 0


def storage(path: str) -> str:
    """The phone's /sdcard: FAKEADB_DIR/sdcard."""
    return os.path.join(HERE, "sdcard", path.removeprefix("/sdcard").strip("/"))


def listing(command):
    """`find FOLDER -mindepth 1 -maxdepth 1 -printf '%M\t%s\t%T@\t%f\0'`, of the stand-in storage."""
    import shlex

    folder = storage(shlex.split(command)[1])
    if not os.path.isdir(folder):
        return 1
    out = b""
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        mode = "drwxrwx---" if os.path.isdir(path) else "-rw-rw----"
        out += f"{mode}\t{os.path.getsize(path)}\t{os.path.getmtime(path):.6f}\t{name}".encode() + b"\0"
    sys.stdout.buffer.write(out)
    return 0


def versions() -> dict[str, int]:
    """The apps' versions (FAKEADB_DIR/versions.json, to say one was updated)."""
    try:
        with open(os.path.join(HERE, "versions.json")) as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


def clipboard_server(port, options):
    """scrcpy's server with no video: the clipboard (FAKEADB_DIR/phone_clipboard is the phone's,
    sent on when it changes; what's set on it is written to control.jsonl, and becomes the
    phone's), and the display's power (written down too, as is the clean-up that turns it back on
    when the server ends, unless told not to clean up). FAKEADB_DIR/unplugged ends it, as a phone
    unplugged would."""
    server = socket.create_server(("127.0.0.1", port))
    control, _ = server.accept()
    control.sendall(b"\0")
    log = os.path.join(HERE, "control.jsonl")
    if "screen_off_timeout" in options:
        with open(log, "a") as file:
            file.write(json.dumps({"type": "session", "screen_off_timeout": options["screen_off_timeout"],
                                   "cleanup": options.get("cleanup", "true")}) + "\n")
    display_off = [False]
    path = os.path.join(HERE, "phone_clipboard")
    said = [open(path).read() if os.path.exists(path) else None]  # what's there already isn't news

    def watch():
        while True:
            time.sleep(0.1)
            if os.path.exists(os.path.join(HERE, "unplugged")):  # the link goes: the server ends
                try:
                    control.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            text = open(path).read() if os.path.exists(path) else None
            if text is not None and text != said[0]:
                said[0] = text
                data = text.encode()
                try:
                    control.sendall(bytes([0]) + struct.pack(">I", len(data)) + data)
                except OSError:
                    return

    threading.Thread(target=watch, daemon=True).start()

    def take(count):
        data = b""
        while len(data) < count:
            chunk = control.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    try:
        while True:
            kind = take(1)[0]
            if kind == 9:
                sequence, paste, length = struct.unpack(">QBI", take(13))
                text = take(length).decode()
                said[0] = text  # its own: not sent back
                with open(path, "w") as file:
                    file.write(text)
                with open(os.path.join(HERE, "control.jsonl"), "a") as file:
                    file.write(json.dumps({"type": "set_clipboard", "text": text, "paste": paste}) + "\n")
                if sequence:  # acknowledged, as scrcpy's server does when asked
                    control.sendall(bytes([1]) + struct.pack(">Q", sequence))
            elif kind == 10:
                on = take(1)[0] == 1
                display_off[0] = not on
                with open(log, "a") as file:
                    file.write(json.dumps({"type": "display_power", "on": on}) + "\n")
    except (EOFError, OSError):
        pass
    if display_off[0] and options.get("cleanup", "true") != "false":
        with open(log, "a") as file:
            file.write(json.dumps({"type": "cleanup", "display_power": True}) + "\n")
    return 0


def media_watch():
    """The loop that watches the media sessions: `media_session` (lines as dumpsys gives them),
    said with the phone's uptime first, again each time the file changes; never-ending."""
    path = os.path.join(HERE, "media_session")
    said = None
    while True:
        try:
            with open(path) as file:
                lines = file.read()
        except OSError:
            lines = ""
        if lines != said:
            print(f"{time.monotonic():.2f} 1.00")
            print(lines.rstrip("\n"))
            print("--", flush=True)
            said = lines
        time.sleep(0.05)


def media_helper():
    """Handset's media helper on the phone: `media_helper.jsonl`'s lines, said again each time
    the file changes; what it's told, in `helper_commands.jsonl`; never-ending."""
    path = os.path.join(HERE, "media_helper.jsonl")

    def listen():
        for line in sys.stdin:
            with open(os.path.join(HERE, "helper_commands.jsonl"), "a") as file:
                file.write(line)

    threading.Thread(target=listen, daemon=True).start()
    said = None
    while True:
        try:
            with open(path) as file:
                lines = file.read()
        except OSError:
            lines = ""
        if lines != said:
            sys.stdout.write(lines if lines.endswith("\n") or not lines else lines + "\n")
            sys.stdout.flush()
            said = lines
        time.sleep(0.05)


def others() -> dict[str, tuple[str, float]]:
    try:
        with open(os.path.join(HERE, "others")) as file:
            lines = [line.split() for line in file if line.strip()]
    except OSError:
        return {}
    return {serial: (number, float(seconds)) for serial, number, seconds in lines}


def main(args):
    if args[:1] == ["-s"]:
        serial, args = args[1], args[2:]
        if serial in others():
            number, seconds = others()[serial]
            if args == ["shell", "getprop ro.serialno"]:
                time.sleep(seconds)
                print(number)
            return 0
        if serial != SERIAL:
            print(f"adb: device '{serial}' not found", file=sys.stderr)
            return 1
    log(args)
    if args == ["devices"]:
        listed = "".join(f"{serial}\tdevice\n" for serial in others())
        print(f"List of devices attached\n{SERIAL}\tdevice\n{listed}")
        return 0
    if args == ["track-devices", "-l"]:
        return track()
    if args[:1] == ["pair"]:  # Wireless debugging's code: 123456 is right
        if args[2:] == ["123456"]:
            print(f"Successfully paired to {args[1]} [guid=adb-{SERIAL}-AbCdEf]")
        else:
            print("Failed: Wrong password or connection was dropped.")
        return 0
    if args[:1] == ["connect"]:
        print(f"connected to {args[1]}")
        return 0
    if args[:1] == ["tcpip"]:
        print(f"restarting in TCP mode port: {args[1]}")
        return 0
    if args[:1] == ["exec-out"] and "screenrecord" in " ".join(args[1:]):
        screenrecord(" ".join(args[1:]))
    if args[:2] == ["shell", "-T"] and args[2:] == ["uinput", "-"]:
        uinput()
        return 0
    if args[:1] == ["push"] and args[2] == "/data/local/tmp/handset-media.jar":
        if os.path.exists(os.path.join(HERE, "media_helper.jsonl")):
            return 0
        print("adb: error: no room", file=sys.stderr)  # a phone the helper can't go to
        return 1
    if args[:1] == ["shell"] and "HandsetClip" in " ".join(args[1:]):
        command = " ".join(args[1:]).split()
        with open(os.path.join(HERE, "control.jsonl"), "a") as file:  # the image on the phone's clipboard
            file.write(json.dumps({"type": "set_clip", "uri": command[-2], "mime": command[-1]}) + "\n")
        print("set")
        return 0
    if args[:1] == ["shell"] and "HandsetMedia" in " ".join(args[1:]):
        return media_helper()
    if args[:1] == ["push"] and args[2].startswith("/sdcard"):
        target = storage(args[2])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(args[1], target)
        return 0
    if args[:1] == ["push"]:
        return 0
    if args[:2] == ["pull", "-a"]:
        if not os.path.exists(storage(args[2])):
            print(f"adb: error: failed to stat remote object '{args[2]}'", file=sys.stderr)
            return 1
        shutil.copy2(storage(args[2]), args[3])
        return 0
    if args[:1] == ["exec-out"] and args[1].startswith("find "):
        return listing(args[1])
    if args[:2] == ["forward", "tcp:0"] and args[2].startswith("localabstract:scrcpy_"):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with open(os.path.join(HERE, f"forward_{args[2].removeprefix('localabstract:scrcpy_')}"), "w") as file:
            file.write(str(port))
        print(port)
        return 0
    if args[:2] == ["forward", "--remove"]:
        return 0
    if args[:1] == ["shell"] and "com.genymobile.scrcpy.Server" in " ".join(args[1:]):
        return scrcpy(" ".join(args[1:]))
    if args[:1] == ["shell"] and " ".join(args[1:]).startswith("logcat -b events"):
        return events()
    if args[:1] == ["shell"] and " ".join(args[1:]).startswith("dumpsys notification"):
        return notification(" ".join(args[1:]))
    if args[:1] == ["shell"] and "dumpsys media_session" in " ".join(args[1:]):
        return media_watch()
    if args[:1] == ["shell"] and " ".join(args[1:]).startswith("dumpsys bluetooth_manager"):
        try:
            with open(os.path.join(HERE, "avrcp")) as file:
                print(file.read())
        except OSError:
            pass
        return 0
    if args == ["shell", "am stack list"]:
        try:
            with open(os.path.join(HERE, "tasks")) as file:
                print(file.read(), end="")
        except OSError:
            pass
        return 0
    if args[:1] == ["shell"] and " ".join(args[1:]).startswith("am display move-stack "):
        return move_task(*" ".join(args[1:]).split()[3:5])
    if args[:1] == ["shell"] and " ".join(args[1:]) == "date +%s.%N":
        print(f"{time.time():.6f}")
        return 0
    if args[:1] == ["shell"]:
        command = " ".join(args[1:])
        if command == "getprop ro.serialno":
            print(SERIAL)
        elif command.startswith("getprop ro.serialno; getprop ro.product.model"):
            print(f"{SERIAL}\nPixel 7\nFake Pixel\n17")  # who it is: its number, model, name and Android
        elif command == "dumpsys battery":
            print("Current Battery Service state:\n  USB powered: true\n  level: 80\n  scale: 100")
        elif command.startswith("cmd package query-activities --brief --components"):
            for _name, package, _system in APPS:
                print(f"{package}/.Main")
        elif command == "pm list packages --show-versioncode":
            for _name, package, _system in APPS:
                print(f"package:{package} versionCode:{versions().get(package, 1)}")
        elif command == "ip -o -4 addr":
            # Its addresses: FAKEADB_DIR/wifi's lines ("<n>: <interface>    inet <address>/24 … scope
            # global …") after its own loopback; none there, it's not on Wi-Fi.
            print("1: lo    inet 127.0.0.1/8 scope host lo")
            if os.path.exists(os.path.join(HERE, "wifi")):
                with open(os.path.join(HERE, "wifi")) as file:
                    print(file.read(), end="")
        elif command in ("wm size", "wm size; wm density"):
            print(f"Physical size: {NATURAL[0]}x{NATURAL[1]}")
            if "density" in command:
                print("Physical density: 420\nOverride density: 425")
        elif command.startswith("cd /sdcard && find ."):
            # The phone's whole storage, as phonesearch.ANDROID_FIND lists it.
            top = storage("/sdcard")
            print(f"d\t0\t{os.path.getmtime(top)}\t.")
            for folder, names, files in os.walk(top):
                names[:] = [n for n in names if not n.startswith(".") and (folder != top or n != "Android")]
                for name in names + [f for f in files if not f.startswith(".")]:
                    full = os.path.join(folder, name)
                    kind, size = ("d", 0) if os.path.isdir(full) else ("f", os.path.getsize(full))
                    print(f"{kind}\t{size}\t{os.path.getmtime(full)}\t./{os.path.relpath(full, top)}")
        if "Viewport INTERNAL" in command:
            r = rotation()
            print(f"  Viewport INTERNAL: displayId=0, uniqueId=local:1, port=1, orientation={r // 90}, "
                  "logicalFrame=[0, 0, 1080, 2400], physicalFrame=[0, 0, 1080, 2400], "
                  "deviceSize=[1080, 2400], isActive=[1]")
        if "mWakefulness" in command:
            print(f"  mWakefulness={wakefulness()}")
        if "isKeyguardShowing" in command:
            print(f"    isKeyguardShowing={str(os.path.exists(os.path.join(HERE, 'locked'))).lower()}")
        if command == "input keyevent KEYCODE_WAKEUP":
            with open(os.path.join(HERE, "wakefulness"), "w") as file:
                file.write("Awake")
        return 0
    print(f"fakeadb: no idea about {args}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
