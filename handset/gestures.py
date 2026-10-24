"""From the window to the phone: where a point in the window falls on the phone's screen, and
what the mouse did there (a tap, a long press, a drag, a scroll), in plain Python so it's tested
without a window."""

from __future__ import annotations

from dataclasses import dataclass

# A press that moves less than this (window pixels) is a tap or a long press, not a drag.
SLOP = 8
# Held at least this long without moving: a long press.
LONG_PRESS_MS = 500
# A drag's length on the phone, so a fling stays a fling and a slow drag stays slow.
SHORTEST_SWIPE_MS = 80
LONGEST_SWIPE_MS = 2000
# A scroll wheel's notch moves the phone's page this far, in its screen's heights.
SCROLL_PER_NOTCH = 0.12
# A press this close to the screen's edge (window pixels) goes down on the edge itself. Phones
# start their own swipes (home from the bottom, back from the sides, the notifications from the
# top) only from the screen's outermost pixels, which a mouse can hardly hit, and just past them
# is the window's resize grip.
EDGE_ZONE = 12


@dataclass(frozen=True)
class Fit:
    """A phone screen of `screen` pixels, drawn as large as fits, centred, in a widget of
    `widget` pixels (as Gtk.Picture draws with ContentFit.CONTAIN)."""

    widget: tuple[float, float]
    screen: tuple[int, int]

    @property
    def scale(self) -> float:
        (ww, wh), (sw, sh) = self.widget, self.screen
        if sw <= 0 or sh <= 0:
            return 0.0
        return min(ww / sw, wh / sh)

    @property
    def origin(self) -> tuple[float, float]:
        (ww, wh), (sw, sh) = self.widget, self.screen
        return (ww - sw * self.scale) / 2, (wh - sh * self.scale) / 2

    def to_screen(self, x: float, y: float) -> tuple[int, int] | None:
        """The phone's pixel under the window's point, or None off the screen."""
        scale = self.scale
        if scale <= 0:
            return None
        ox, oy = self.origin
        px, py = (x - ox) / scale, (y - oy) / scale
        sw, sh = self.screen
        if px < 0 or py < 0 or px >= sw or py >= sh:
            return None
        return int(px), int(py)

    def to_widget(self, x: float, y: float) -> tuple[float, float]:
        """Where the phone's pixel shows in the window."""
        ox, oy = self.origin
        return ox + x * self.scale, oy + y * self.scale

    def clamp(self, x: float, y: float) -> tuple[int, int]:
        """The phone's pixel nearest the window's point, for a drag that ends off the screen."""
        scale = self.scale or 1.0
        ox, oy = self.origin
        sw, sh = self.screen
        return (int(min(max((x - ox) / scale, 0), sw - 1)),
                int(min(max((y - oy) / scale, 0), sh - 1)))


@dataclass(frozen=True)
class Gesture:
    """What a press did, in the phone's pixels: "tap" (at start), "press" (a long press at
    start, for ms), or "swipe" (from start to end, taking ms)."""

    kind: str
    start: tuple[int, int]
    end: tuple[int, int] | None = None
    ms: int = 0


def to_edge(fit: Fit, x: float, y: float) -> tuple[int, int] | None:
    """Where a press at the window's point (x, y) goes down on the phone: as Fit.to_screen, but
    within EDGE_ZONE of an edge, on the edge."""
    point = fit.to_screen(x, y)
    if point is None:
        return None
    (ox, oy), (sw, sh) = fit.origin, fit.screen
    right, bottom = ox + sw * fit.scale, oy + sh * fit.scale
    px, py = point
    if x - ox < EDGE_ZONE:
        px = 0
    elif right - x <= EDGE_ZONE:
        px = sw - 1
    if y - oy < EDGE_ZONE:
        py = 0
    elif bottom - y <= EDGE_ZONE:
        py = sh - 1
    return px, py


def classify(fit: Fit, down: tuple[float, float], up: tuple[float, float], ms: float,
             moved: float) -> Gesture | None:
    """The gesture of a press at `down` released at `up` (window points) `ms` later, having
    strayed at most `moved` window pixels from `down` on the way. None: it began off the
    phone's screen."""
    start = fit.to_screen(*down)
    if start is None:
        return None
    if moved < SLOP:
        if ms >= LONG_PRESS_MS:
            return Gesture("press", start, ms=int(ms))
        return Gesture("tap", start)
    end = fit.clamp(*up)
    return Gesture("swipe", start, end, int(min(max(ms, SHORTEST_SWIPE_MS), LONGEST_SWIPE_MS)))


def safe_box(screen: tuple[int, int]) -> tuple[int, int, int, int]:
    """Where a scroll's finger may go on a screen this size (left, top, right, bottom): away
    from the edges, where a swipe would go back (the sides) or open the notifications or go home
    (top and bottom) instead of scrolling."""
    sw, sh = screen
    return int(sw * 0.15), int(sh * 0.08), int(sw * 0.85), int(sh * 0.92)


def _clamp(value: float, low: int, high: int) -> int:
    return int(min(max(value, low), high))


def scroll(fit: Fit, at: tuple[float, float], down: float, right: float = 0.0) -> Gesture | None:
    """A scroll of `down` wheel notches down the page and `right` notches to the right, at the
    window's point `at`, as the swipe that moves the phone's page that way: the finger goes the
    other way. Never from or past the edges (safe_box). None off the screen or for no
    movement."""
    start = fit.to_screen(*at)
    if start is None or (down == 0 and right == 0):
        return None
    sw, sh = fit.screen
    left, top, right_edge, bottom = safe_box(fit.screen)
    x0, y0 = _clamp(start[0], left, right_edge), _clamp(start[1], top, bottom)
    x = _clamp(x0 - right * SCROLL_PER_NOTCH * sw, left, right_edge)
    y = _clamp(y0 - down * SCROLL_PER_NOTCH * sh, top, bottom)
    if (x, y) == (x0, y0):
        return None
    return Gesture("swipe", (x0, y0), (x, y), 300)


def inside(screen: tuple[int, int], x: float, y: float) -> tuple[int, int]:
    """The nearest point to (x, y) where a scroll's finger may go."""
    left, top, right, bottom = safe_box(screen)
    return _clamp(x, left, right), _clamp(y, top, bottom)
