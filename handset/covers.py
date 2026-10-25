"""Covers for what an iPhone plays: its media service tells the title, artist and album but has no
picture, so the cover is looked up in Apple's music catalog (its public search, no account) and
kept here at 600 pixels, one lookup per album (or song). Slow: called off the main loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

from .log import debug

SEARCH = "https://itunes.apple.com/search"
# The catalog's storefronts differ (an album sold in one country isn't in another's): tried in turn.
STOREFRONTS = ("US", "GB")
SIZE = "600x600bb"
# Nothing found for a song is remembered this long before it's looked up again.
NONE_FOR_S = 7 * 24 * 3600


def folder() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "covers")


def kept(title: str, artist: str, album: str) -> str:
    """Where a cover is kept: one per album, or per song when there's no album."""
    key = f"{artist.casefold()}\n{(album or title).casefold()}"
    return os.path.join(folder(), hashlib.sha1(key.encode()).hexdigest()[:20] + ".jpg")


def best(results: list[dict], artist: str, name: str = "", key: str = "collectionName") -> str | None:
    """The cover of the result by the same artist (and with the same name, when one's given), at
    SIZE: none rather than someone else's."""
    wanted, named = artist.casefold(), name.casefold()
    for result in results:
        by = str(result.get("artistName", "")).casefold()
        if not wanted or not (wanted in by or by in wanted):
            continue
        if named and named not in str(result.get(key, "")).casefold():
            continue
        url = str(result.get("artworkUrl100") or "")
        if url:
            return url.replace("100x100bb", SIZE)
    return None


def search(term: str, entity: str, country: str = "US") -> list[dict]:
    query = urllib.parse.urlencode({"term": term, "entity": entity, "limit": 25, "country": country})
    request = urllib.request.Request(f"{SEARCH}?{query}", headers={"User-Agent": "Handset"})
    with urllib.request.urlopen(request, timeout=10) as reply:
        return json.load(reply).get("results", [])


def cover(title: str, artist: str, album: str = "") -> str | None:
    """A file with the cover, or None."""
    if not title:
        return None
    path = kept(title, artist, album)
    if os.path.exists(path):
        return path
    if os.path.exists(path + ".none") and time.time() - os.path.getmtime(path + ".none") < NONE_FOR_S:
        return None
    try:
        # The album by its artist; else the song itself (a single, or an album named otherwise).
        url = None
        for country in STOREFRONTS:
            if album:
                url = best(search(f"{artist} {album}", "album", country), artist, album)
            url = url or best(search(f"{artist} {title}", "song", country), artist, title, "trackName")
            if url:
                break
        picture = b""
        if url:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Handset"}),
                                        timeout=15) as reply:
                picture = reply.read()
    except (OSError, ValueError) as error:
        debug("no cover for", title, error)
        return None  # tried again next time: the network may be back
    os.makedirs(folder(), exist_ok=True)
    if not url or not picture:
        open(path + ".none", "w").close()
        return None
    with open(path + ".new", "wb") as file:
        file.write(picture)
    os.replace(path + ".new", path)
    return path
