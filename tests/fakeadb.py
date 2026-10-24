#!/usr/bin/env python3
"""A stand-in for adb in Handset's tests (HANDSET_ADB): one phone, FAKE7, whose screen is
GStreamer's test picture as H.264 and whose uinput and input commands are written down. It
keeps its state in FAKEADB_DIR: `rotation` (degrees, to turn the screen), `wakefulness` (Awake,
Dozing… as `dumpsys power` says; the wake key wakes it), `log.jsonl` (what it
was asked) and `uinput.jsonl` (the virtual touchscreen's commands). Never a real phone."""

import json
import os
import re
import subprocess
import sys

HERE = os.environ.get("FAKEADB_DIR", ".")
SERIAL = "FAKE7"
NATURAL = (1080, 2400)


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


def main(args):
    if args[:1] == ["-s"]:
        serial, args = args[1], args[2:]
        if serial != SERIAL:
            print(f"adb: device '{serial}' not found", file=sys.stderr)
            return 1
    log(args)
    if args == ["devices"]:
        print(f"List of devices attached\n{SERIAL}\tdevice\n")
        return 0
    if args[:1] == ["exec-out"] and "screenrecord" in " ".join(args[1:]):
        screenrecord(" ".join(args[1:]))
    if args[:2] == ["shell", "-T"] and args[2:] == ["uinput", "-"]:
        uinput()
        return 0
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
