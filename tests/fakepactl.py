#!/usr/bin/env python3
"""A stand-in for pactl in Handset's tests, found first on PATH as `pactl`: the sound server's
streams are `sink-inputs.json` in FAKEPACTL_DIR, as `pactl -f json list sink-inputs` prints them.
`set-sink-input-volume` sets one's volume there, one for all its channels or one for each (refused
for one not there, or given as many as it hasn't, as pactl does), and `subscribe` says when one
comes, changes or goes, until it's killed (nothing, while a `quiet` file is there), writing
`subscribed` once it's listening; in German where its locale says so, as pactl does with a
language pack (its words its own). Listing takes as many seconds as `slow` says, if it's there;
with a `vanish` file, the streams are gone right after it (the next listing only). While a `down`
file is there, the sound server is gone: everything's refused, and `subscribe` ends. Never the
computer's own sound server."""

import json
import os
import sys
import time

HERE = os.environ.get("FAKEPACTL_DIR", ".")
STREAMS = os.path.join(HERE, "sink-inputs.json")


def streams() -> list[dict]:
    try:
        with open(STREAMS) as file:
            return json.load(file)
    except (OSError, ValueError):
        return []


def save(listed: list[dict]):
    with open(STREAMS + ".new", "w") as file:
        json.dump(listed, file)
    os.replace(STREAMS + ".new", STREAMS)


def slow() -> float:
    try:
        with open(os.path.join(HERE, "slow")) as file:
            return float(file.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def said(event: str, index: int) -> str:
    """An event's line, in the language its locale says (LC_ALL, LC_MESSAGES, then LANG)."""
    language = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "C"
    if language.startswith("de"):
        event = {"new": "neu", "change": "Wechsel", "remove": "entfernt"}[event]
        return f"Ereignis '{event}' auf Ziel-Eingang #{index}"
    return f"Event '{event}' on sink-input #{index}"


def subscribe():
    known = {stream["index"]: stream for stream in streams()}
    with open(os.path.join(HERE, "subscribed"), "w"):
        pass
    while True:
        time.sleep(0.02)
        if os.path.exists(os.path.join(HERE, "down")):
            return
        now = {stream["index"]: stream for stream in streams()}
        if not os.path.exists(os.path.join(HERE, "quiet")):
            for index, stream in now.items():
                if index not in known:
                    print(said("new", index), flush=True)
                elif stream != known[index]:
                    print(said("change", index), flush=True)
            for index in known.keys() - now.keys():
                print(said("remove", index), flush=True)
        known = now


def main(args: list[str]) -> int:
    if os.path.exists(os.path.join(HERE, "down")):
        print("Connection failure: Connection refused", file=sys.stderr)
        return 1
    if args == ["-f", "json", "list", "sink-inputs"]:
        time.sleep(slow())
        print(json.dumps(streams()))
        if os.path.exists(os.path.join(HERE, "vanish")):
            os.remove(os.path.join(HERE, "vanish"))
            save([])
        return 0
    if args[:1] == ["set-sink-input-volume"] and len(args) >= 3:
        listed = streams()
        for stream in listed:
            if str(stream["index"]) == args[1]:
                channels = list(stream.get("volume") or {"mono": 0})
                volumes = args[2:] * len(channels) if len(args) == 3 else args[2:]
                if len(volumes) != len(channels):
                    print("Failed to set volume: not as many channels", file=sys.stderr)
                    return 1
                stream["volume"] = {channel: {"value_percent": volume} for channel, volume in zip(channels, volumes)}
                save(listed)
                return 0
        print("Failure: No such entity", file=sys.stderr)
        return 1
    if args == ["subscribe"]:
        try:
            subscribe()
        except BrokenPipeError:
            return 0
        return 1  # the sound server gone
    print("fakepactl: not played:", args, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
