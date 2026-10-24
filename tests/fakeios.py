#!/usr/bin/env python3
"""A stand-in for handset-ios in Handset's tests (HANDSET_IOS_HELPER): it speaks the helper's
lines, sends GStreamer's test picture as its screen (H.264, the encoder there is here, and it
says so), and writes every command it gets to FAKEIOS_DIR/commands.jsonl. FAKEIOS_DARK_AFTER
(seconds) stops the pictures then, as a dark iPhone does. Never a real phone."""

import json
import os
import subprocess
import sys
import threading

HERE = os.environ.get("FAKEIOS_DIR", ".")


def say(**event):
    print(json.dumps(event), file=sys.stderr, flush=True)


def main(args):
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
    video.terminate()
    video.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
