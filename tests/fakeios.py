#!/usr/bin/env python3
"""A stand-in for handset-ios in Handset's tests (HANDSET_IOS_HELPER): it speaks the helper's
lines, sends GStreamer's test picture as its screen (H.264, the encoder there is here, and it
says so), writes every command it gets to FAKEIOS_DIR/commands.jsonl and answers "launch"; its
`apps` lists APPS, with an icon for the first. FAKEIOS_DARK_AFTER
(seconds) stops the pictures then, as a dark iPhone does. Never a real phone."""

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.environ.get("FAKEIOS_DIR", ".")


def say(**event):
    print(json.dumps(event), file=sys.stderr, flush=True)


APPS = [("Fake Chat", "org.example.chat"), ("Maps", "com.apple.Maps")]
# A 1×1 PNG, as the phone's icons come.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d49444154789c63f8cfc0f01f0005000201a5f1b6e80000000049454e44ae426082")


def apps(args):
    folder = args[args.index("--icons") + 1] if "--icons" in args else None
    for index, (name, bundle) in enumerate(APPS):
        icon = None
        if folder and index == 0:
            icon = os.path.join(folder, f"{bundle}.png")
            with open(icon, "wb") as file:
                file.write(PNG)
        print(json.dumps({"name": name, "bundle": bundle, "icon": icon}), flush=True)
    return 0


def clipboard():
    """The phone's clipboard: FAKEIOS_DIR/phone_clipboard, said when it changes; sets written to
    commands.jsonl and put in it."""
    path = os.path.join(HERE, "phone_clipboard")
    said = [open(path).read() if os.path.exists(path) else None]

    def watch():
        while True:
            time.sleep(0.1)
            text = open(path).read() if os.path.exists(path) else None
            if text is not None and text != said[0]:
                said[0] = text
                print(json.dumps({"event": "clipboard", "text": text}), flush=True)

    threading.Thread(target=watch, daemon=True).start()
    say(event="clipboard", ready=True)
    with open(os.path.join(HERE, "commands.jsonl"), "a") as log:
        for line in sys.stdin:
            log.write(line)
            log.flush()
            command = json.loads(line)
            if command.get("op") == "set":
                said[0] = command["text"]
                with open(path, "w") as file:
                    file.write(command["text"])
    return 0


def files():
    """The phone's media files: FAKEIOS_DIR/media, a command per line as the helper takes them."""
    import shutil

    root = os.path.join(HERE, "media")
    say(event="files", ready=True)
    for line in sys.stdin:
        command = json.loads(line)
        op, path = command.get("op"), command.get("path", "")
        here = os.path.join(root, path.strip("/"))
        try:
            if op == "list":
                entries = [{"name": n, "folder": os.path.isdir(os.path.join(here, n)),
                            "size": os.path.getsize(os.path.join(here, n)),
                            "modified": int(os.path.getmtime(os.path.join(here, n)))} for n in os.listdir(here)]
                answer = {"event": "list", "path": path, "entries": entries}
            elif op == "fetch":
                shutil.copyfile(here, command["to"])
                answer = {"event": "fetched", "path": path}
            elif op == "send":
                shutil.copyfile(command["from"], here)
                answer = {"event": "sent", "path": path}
            else:
                continue
        except OSError as error:
            answer = {"event": "error", "op": op, "path": path, "message": str(error)}
        print(json.dumps(answer), flush=True)
    return 0


def main(args):
    if args[:1] == ["apps"]:
        return apps(args)
    if args[:1] == ["files"]:
        return files()
    if args[:1] == ["clipboard"]:
        return clipboard()
    if args[:1] != ["stream"] or "--pairing" not in args or "--udid" not in args:
        say(event="error", message=f"usage: stream --pairing FILE --udid UDID, not {args}")
        return 2
    with open(os.path.join(HERE, "args.json"), "w") as file:
        json.dump(args, file)
    say(event="status", text="Looking for the iPhone on its cable and on Wi-Fi")
    say(event="video", codec="h264")
    width, height = os.environ.get("FAKEIOS_SIZE", "390x844").split("x")
    video = subprocess.Popen(
        ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true", "pattern=ball", "!",
         f"video/x-raw,width={width},height={height},framerate=30/1", "!", "videoconvert", "!",
         "openh264enc", "!", "h264parse", "config-interval=-1", "!",
         "video/x-h264,stream-format=byte-stream", "!", "fdsink", "fd=1"],
        stdout=sys.stdout, stderr=subprocess.DEVNULL)
    say(event="input", ready=True)
    dark_after = float(os.environ.get("FAKEIOS_DARK_AFTER") or 0)
    if dark_after:
        # The screen going dark: no more pictures, the helper still there.
        threading.Timer(dark_after, video.terminate).start()
    with open(os.path.join(HERE, "commands.jsonl"), "a") as log:
        for line in sys.stdin:
            log.write(line)
            log.flush()
            try:
                command = json.loads(line)
            except ValueError:
                continue
            if command.get("op") == "launch":
                opened = command.get("bundle") in [app[1] for app in APPS]
                say(event="launch", ok=opened, bundle=command.get("bundle"),
                    **({} if opened else {"message": "FBSOpenApplicationErrorDomain error 7: not unlocked"}))
    video.terminate()
    video.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
