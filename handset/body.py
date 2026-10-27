"""A phone's screen as Handset's window draws it (phonebody.py): its size and its rounded corners,
and the cutout in it (a notch hanging from the top, a punch hole, an island), as the phone says or
its model is known; and where it goes at a size, in its glass rim (layout).

An Android phone says: `dumpsys display` gives its screen's rounded corners and its cutout's
outline (an SVG path, a circle for a punch hole) in its screen's pixels. An iPhone doesn't: its
model's, as Apple makes them (in points, 3 or 2 of its pixels each). Kept for each phone
(~/.cache/handset/bodies.json): an Android phone is asked once.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass

from .log import debug


@dataclass
class Cutout:
    kind: str  # "notch" (hangs from the screen's top edge), "hole" (a circle) or "island" (a pill)
    x: float  # its box, in the upright screen's pixels
    y: float
    width: float
    height: float


@dataclass
class Body:
    screen: tuple[int, int]  # the screen's pixels, upright
    corner: float  # the screen's corners' radius, in those pixels
    cutout: Cutout | None = None

    @staticmethod
    def from_dict(data: dict) -> Body | None:
        try:
            cutout = Cutout(**data["cutout"]) if data.get("cutout") else None
            return Body(tuple(data["screen"]), float(data["corner"]), cutout)
        except (KeyError, TypeError, ValueError):
            return None


def fallback(platform: str, screen: tuple[int, int]) -> Body:
    """A phone that says nothing of itself: rounded as most are now, with no cutout drawn."""
    width = min(screen)
    return Body(screen, (0.12 if platform == "ios" else 0.06) * width)


# ---- Android: what the phone says --------------------------------------------------------------

CIRCLE = re.compile(r"M\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*a\s*([\d.]+)\s*,\s*([\d.]+)\s+0\s+[01]\s+[01]\s+(-?[\d.]+)\s*,\s*0")


def parse_android(text: str, screen: tuple[int, int]) -> Body:
    """The phone's front from `dumpsys display` (its built-in screen's, the first listed): its
    screen's corner radius, and its cutout from the path it's drawn by (a circle: a punch hole;
    anything else, the box it's kept clear in, as a notch)."""
    body = fallback("android", screen)
    corner = re.search(r"RoundedCorner\{position=TopLeft, radius=(\d+)", text)
    if corner:
        body.corner = float(corner[1])
    spec = re.search(r"cutoutSpec=\{([^}]*)\}", text)
    density = re.search(r"density=\{([\d.]+)\}", text)
    if spec:
        path = spec[1].split("@bind")[0]
        scale = float(density[1]) if density and "@dp" in path else 1.0
        circle = CIRCLE.search(path)
        if circle:
            x, y, r = float(circle[1]) * scale, float(circle[2]) * scale, float(circle[3]) * scale
            if "@left" not in path:  # its x from the screen's middle
                x += screen[0] / 2
            body.cutout = Cutout("hole", x, y - r, 2 * r, 2 * r)
            return body
    bounds = re.search(r"boundingRect=\{Bounds=\[(.*?)\]\}", text)
    for found in re.finditer(r"Rect\((\d+), (\d+) - (\d+), (\d+)\)", bounds[1] if bounds else ""):
        left, top, right, bottom = (int(n) for n in found.groups())
        if right > left and bottom > top and top == 0:
            body.cutout = Cutout("notch", left, 0, right - left, bottom)
            break
    return body


# ---- iPhones: their models' ----------------------------------------------------------------------

# iPhones by model: their screen's corner radius, and their cutout (kind, width, height, how far
# below the top), in points. A Home button's iPhone has square corners.
IPHONES = (
    (r"iPhone (X|XS|XS Max|11 Pro|11 Pro Max)$", (39.0, "notch", 209.0, 30.0, 0.0)),
    (r"iPhone (XR|11)$", (41.5, "notch", 209.0, 30.0, 0.0)),
    (r"iPhone 12 mini$", (44.0, "notch", 210.0, 32.0, 0.0)),
    (r"iPhone 12( Pro)?$", (47.33, "notch", 210.0, 32.0, 0.0)),
    (r"iPhone 12 Pro Max$", (53.33, "notch", 210.0, 32.0, 0.0)),
    (r"iPhone 13 mini$", (44.0, "notch", 162.0, 33.0, 0.0)),
    (r"iPhone (13( Pro)?|14|16e)$", (47.33, "notch", 162.0, 33.0, 0.0)),
    (r"iPhone (13 Pro Max|14 Plus)$", (53.33, "notch", 162.0, 33.0, 0.0)),
    (r"iPhone (1[4-9] Pro( Max)?|1[5-9]( Plus)?|Air|17)$", (55.0, "island", 126.0, 37.33, 11.33)),
)
HOME_BUTTON = r"iPhone (SE|[5-8]( Plus)?|5s|5c)\b"


def iphone_body(model: str, screen: tuple[int, int]) -> Body:
    """An iPhone's front, from its model's (an unknown model: rounded, with no cutout drawn)."""
    width = min(screen)
    if re.search(HOME_BUTTON, model or ""):
        return Body(screen, 0.0)
    points = width / (3 if width >= 1000 else 2)
    scale = width / points
    for pattern, (corner, kind, cut_width, cut_height, top) in IPHONES:
        if re.search(pattern, model or ""):
            cut = Cutout(kind, (width - cut_width * scale) / 2, top * scale, cut_width * scale, cut_height * scale)
            return Body(screen, corner * scale, cut)
    return fallback("ios", screen)


# ---- Kept ----------------------------------------------------------------------------------------

def bodies_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "handset", "bodies.json")


def kept(phone_id: str, screen: tuple[int, int] | None = None) -> Body | None:
    """The body kept for the phone (if `screen` is given, only if it's for that screen)."""
    try:
        with open(bodies_path()) as file:
            body = Body.from_dict(json.load(file).get(phone_id) or {})
    except (OSError, ValueError, AttributeError):
        return None
    return body if body and (screen is None or tuple(body.screen) == tuple(screen)) else None


def keep(phone_id: str, body: Body):
    try:
        with open(bodies_path()) as file:
            bodies = json.load(file)
    except (OSError, ValueError):
        bodies = {}
    bodies[phone_id] = asdict(body)
    try:
        os.makedirs(os.path.dirname(bodies_path()), exist_ok=True)
        with open(bodies_path() + ".new", "w") as file:
            json.dump(bodies, file)
        os.replace(bodies_path() + ".new", bodies_path())
    except OSError as error:
        debug("the phone's shape wasn't kept:", error)


def android_body(phone_id: str, serial: str, screen: tuple[int, int]) -> Body:
    """An Android phone's front: kept, or asked of it (slow: off the main loop)."""
    body = kept(phone_id, screen)
    if body:
        return body
    from .android import shell

    try:
        text = shell(serial, "dumpsys display | grep -m2 -E 'roundedCorners|cutoutSpec'", timeout=10)
    except Exception as error:  # noqa: BLE001 - the phone not answering: its shape as most are
        debug("the phone didn't say its shape:", error)
        return fallback("android", screen)
    body = parse_android(text, screen)
    keep(phone_id, body)
    return body


# ---- Where it goes --------------------------------------------------------------------------------

RIM = 2.0  # the glass round the screen, a hairline (the window's pixels)


@dataclass
class Layout:
    """The screen and its rim, in a widget's pixels, as boxes (x, y, width, height)."""
    outer: tuple[float, float, float, float]  # the rim's outside
    outer_radius: float
    screen: tuple[float, float, float, float]
    screen_radius: float
    cutout: tuple[str, float, float, float, float] | None = None  # (kind, x, y, width, height)
    scale: float = 1.0  # the window's pixels per screen pixel


def turned(body: Body, rotation: int) -> tuple[int, int]:
    """The screen's size as it shows, turned `rotation` degrees."""
    width, height = body.screen
    return (height, width) if rotation in (90, 270) else (width, height)


def _turn_box(box: tuple[float, float, float, float], screen: tuple[int, int], rotation: int):
    """A box on the upright screen, as it shows turned (the phone's top on the left at 90°, as
    Android turns it, on the right at 270°)."""
    x, y, w, h = box
    width, height = screen
    if rotation == 90:
        return y, width - x - w, h, w
    if rotation == 180:
        return width - x - w, height - y - h, w, h
    if rotation == 270:
        return height - y - h, x, h, w
    return box


def rim(_body: Body, _scale: float) -> float:
    """The glass rim's thickness, whatever the screen's size."""
    return RIM


def outer_size(body: Body, rotation: int, scale: float) -> tuple[float, float]:
    """The screen's size with its rim, drawn at `scale`."""
    width, height = turned(body, rotation)
    glass = rim(body, scale)
    return width * scale + 2 * glass, height * scale + 2 * glass


def fit_scale(body: Body, rotation: int, room: tuple[float, float]) -> float:
    """The largest scale at which the screen and its rim fit in `room`."""
    low, high = 0.0, 4.0
    for _ in range(40):
        middle = (low + high) / 2
        width, height = outer_size(body, rotation, middle)
        if width <= room[0] and height <= room[1]:
            low = middle
        else:
            high = middle
    return low


def layout(body: Body, rotation: int, width: float, height: float, cutout: bool = True,
           middle: bool = True) -> Layout:
    """The screen and its rim as large as fit in a widget `width`×`height`: in its middle, or at its
    top left (`middle` False)."""
    scale = fit_scale(body, rotation, (width, height))
    outer_w, outer_h = outer_size(body, rotation, scale)
    ox, oy = ((width - outer_w) / 2, (height - outer_h) / 2) if middle else (0.0, 0.0)
    shown_w, shown_h = turned(body, rotation)
    glass = rim(body, scale)
    screen = (ox + glass, oy + glass, shown_w * scale, shown_h * scale)
    screen_radius = body.corner * scale
    shape = Layout((ox, oy, outer_w, outer_h), screen_radius + glass, screen, screen_radius, scale=scale)
    if cutout and body.cutout:
        x, y, w, h = _turn_box((body.cutout.x, body.cutout.y, body.cutout.width, body.cutout.height),
                               body.screen, rotation)
        shape.cutout = (body.cutout.kind, screen[0] + x * scale, screen[1] + y * scale, w * scale, h * scale)
    return shape
