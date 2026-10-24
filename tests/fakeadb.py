#!/usr/bin/env python3
"""A stand-in for adb in Handset's tests (HANDSET_ADB): one phone, FAKE7, whose screen is
GStreamer's test picture as H.264 and whose uinput and input commands are written down. It
keeps its state in FAKEADB_DIR: `rotation` (degrees, to turn the screen), `wakefulness` (Awake,
Dozing… as `dumpsys power` says; the wake key wakes it), `log.jsonl` (what it
was asked) and `uinput.jsonl` (the virtual touchscreen's commands). scrcpy's server is played too: listing apps
(APPS), and an app's own screen (new_display=…): the test picture as its video, every control
message written to `control.jsonl`. `others`, if there, lists more
connections adb has, one per line: "<serial> <serial number> <seconds to answer getprop>" (another
phone, or FAKE7 over Wi-Fi); they answer nothing else. Never a real phone."""

import json
import os
import re
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
        for name, package, system in APPS:
            mark = "*" if system else "-"
            print(f" {mark} {name}\n   {package}" if len(name) >= 30 else f" {mark} {name:<30} {package}")
        return 0
    with open(os.path.join(HERE, f"forward_{options['scid']}")) as file:
        port = int(file.read())
    width, height = (int(n) for n in options.get("new_display", f"{NATURAL[0]}x{NATURAL[1]}").split("/")[0].split("x"))
    server = socket.create_server(("127.0.0.1", port))
    video, _ = server.accept()
    video.sendall(b"\0" + struct.pack(">III", 0x68323634, width, height))
    control = server.accept()[0] if options.get("control") == "true" else None
    if "new_display" in options:
        print(f"[server] INFO: New display: {width}x{height} (id=7)", flush=True)
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
    sizes = {0: 14, 2: 32, 4: 2, 17: 1}

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
    if args[:1] == ["exec-out"] and "screenrecord" in " ".join(args[1:]):
        screenrecord(" ".join(args[1:]))
    if args[:2] == ["shell", "-T"] and args[2:] == ["uinput", "-"]:
        uinput()
        return 0
    if args[:1] == ["push"]:
        return 0
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
    if args[:1] == ["shell"]:
        command = " ".join(args[1:])
        if command == "getprop ro.serialno":
            print(SERIAL)
        elif command == "wm size":
            print(f"Physical size: {NATURAL[0]}x{NATURAL[1]}")
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
