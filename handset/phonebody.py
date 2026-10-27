"""The phone's screen in Handset's window, as the phone has it: its own rounded corners, and its
notch, punch hole or island over it where the phone has one (body.py says the shape), in a hairline
of glass. As large as fits, so the shape is always the phone's: at the window's top left (the
window comes round it once it's resized, and the top left is what stays put), in its middle when
it fills the screen. Each of its corners resizes the window, the desktop doing it.

Its one child, the screen, is drawn clipped to the screen's corners; the cutout goes over it (the
phone's screen shows nothing there either), and while Ada works on the phone a ring round it.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

from . import body as bodies  # noqa: E402


def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = r, g, b, a
    return color


def _rect(box) -> Graphene.Rect:
    return Graphene.Rect().init(*box)


def _rounded(box, radius: float) -> Gsk.RoundedRect:
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(_rect(box), max(0.0, min(radius, box[2] / 2, box[3] / 2)))
    return rounded


# The rim: light glass, brighter at its very edge, a dark line where it meets the screen; the
# cutout black, as the phone's is.
RIM_GLASS = _rgba(1, 1, 1, 0.3)
RIM_EDGE = _rgba(1, 1, 1, 0.55)
RIM_INNER = _rgba(0, 0, 0, 0.55)
SCREEN = _rgba(0, 0, 0)
LENS_RING = _rgba(1, 1, 1, 0.08)
RING_WIDTH = 3.0
# A corner held resizes the window from there: this much of it, each way.
GRIP = 18
CORNERS = (("nw-resize", Gdk.SurfaceEdge.NORTH_WEST), ("ne-resize", Gdk.SurfaceEdge.NORTH_EAST),
           ("sw-resize", Gdk.SurfaceEdge.SOUTH_WEST), ("se-resize", Gdk.SurfaceEdge.SOUTH_EAST))


class PhoneBody(Gtk.Widget):
    __gtype_name__ = "HandsetPhoneBody"

    def __init__(self, child: Gtk.Widget):
        super().__init__()
        self.body = bodies.fallback("android", (1080, 2400))
        self.rotation = 0
        self.show_cutout = True
        self.ring: Gdk.RGBA | None = None  # Ada at work (or a drop coming): round the screen
        self.bare = False  # full screen: the screen alone, no rim
        self.shape: bodies.Layout | None = None
        self.child = child
        child.set_parent(self)
        self.grips = []
        for cursor, edge in CORNERS:  # after the screen, so they're over it
            grip = Gtk.Box(cursor=Gdk.Cursor.new_from_name(cursor, None))
            press = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
            press.connect("pressed", self._grabbed, edge)
            grip.add_controller(press)
            grip.set_parent(self)
            self.grips.append(grip)

    def do_dispose(self):
        for widget in [self.child] + self.grips:
            if widget:
                widget.unparent()
        self.child, self.grips = None, []
        super().do_dispose()

    def _filled(self) -> bool:
        """Whether the window fills the screen (maximized, full screen)."""
        root = self.get_root()
        return isinstance(root, Gtk.Window) and (root.is_maximized() or root.is_fullscreen())

    def _grabbed(self, gesture: Gtk.GestureClick, _presses: int, x: float, y: float, edge: Gdk.SurfaceEdge):
        """A corner held: the desktop resizes the window from it."""
        native = self.get_native()
        surface = native.get_surface() if native else None
        if not isinstance(surface, Gdk.Toplevel):
            return
        found, point = gesture.get_widget().compute_point(native, Graphene.Point().init(x, y))
        if not found:
            return
        sx, sy = native.get_surface_transform()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        surface.begin_resize(edge, gesture.get_device(), gesture.get_current_button(), point.x + sx, point.y + sy,
                             gesture.get_current_event_time())

    def set_body(self, body: bodies.Body, rotation: int = 0, cutout: bool = True):
        if (body, rotation, cutout) != (self.body, self.rotation, self.show_cutout):
            self.body, self.rotation, self.show_cutout = body, rotation, cutout
            self.queue_allocate()
            self.queue_draw()

    def set_bare(self, bare: bool):
        if bare != self.bare:
            self.bare = bare
            self.queue_draw()

    def set_ring(self, color: Gdk.RGBA | None):
        if color != self.ring:
            self.ring = color
            self.queue_draw()

    def screen_box(self) -> tuple[float, float, float, float] | None:
        """Where the screen is drawn, in this widget's pixels."""
        return self.shape.screen if self.shape else None

    def do_measure(self, orientation, for_size):
        if not self.child:
            return 0, 0, -1, -1
        smallest, _natural, _b1, _b2 = self.child.measure(orientation, -1)
        return min(smallest, 160), 360, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int):
        if not self.child:
            return
        filled = self._filled()
        self.shape = bodies.layout(self.body, self.rotation, width, height, self.show_cutout, middle=filled)
        ox, oy, ow, oh = self.shape.outer
        size = 0 if filled else GRIP  # filling the screen, nothing to resize
        for grip, (left, top) in zip(self.grips, ((0, 0), (1, 0), (0, 1), (1, 1))):
            at = Graphene.Point().init(ox + left * (ow - size), oy + top * (oh - size))
            grip.allocate(size, size, -1, Gsk.Transform().translate(at))
        x, y, w, h = (round(v) for v in self.shape.screen)
        self.shape.screen = (x, y, w, h)
        # Never under what the child needs (the clip hides what's over).
        least_w = self.child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        w = max(w, least_w)
        least_h = self.child.measure(Gtk.Orientation.VERTICAL, w)[0]
        h = max(h, least_h)
        self.child.allocate(w, h, -1, Gsk.Transform().translate(Graphene.Point().init(x, y)))

    def do_snapshot(self, snapshot: Gtk.Snapshot):
        shape = self.shape
        if not shape:
            return
        if not self.bare:
            outer = _rounded(shape.outer, shape.outer_radius)
            snapshot.push_rounded_clip(outer)
            snapshot.append_color(RIM_GLASS, _rect(shape.outer))
            snapshot.pop()
            snapshot.append_border(outer, [1, 1, 1, 1], [RIM_EDGE] * 4)
        screen = _rounded(shape.screen, shape.screen_radius)
        snapshot.push_rounded_clip(screen)
        snapshot.append_color(SCREEN, _rect(shape.screen))
        self.snapshot_child(self.child, snapshot)
        if shape.cutout:
            self._cutout(snapshot, *shape.cutout)
        snapshot.pop()
        if not self.bare:
            snapshot.append_border(screen, [1, 1, 1, 1], [RIM_INNER] * 4)
        if self.ring:
            snapshot.append_border(screen, [RING_WIDTH] * 4, [self.ring] * 4)

    def _cutout(self, snapshot: Gtk.Snapshot, kind: str, x: float, y: float, w: float, h: float):
        if kind == "hole":
            r = min(w, h) / 2
            box = (x + w / 2 - r, y + h / 2 - r, 2 * r, 2 * r)
            lens = _rounded(box, r)
            snapshot.push_rounded_clip(lens)
            snapshot.append_color(SCREEN, _rect(box))
            snapshot.pop()
            snapshot.append_border(lens, [1, 1, 1, 1], [LENS_RING] * 4)
            return
        if kind == "island":
            pill = _rounded((x, y, w, h), min(w, h) / 2)
            snapshot.push_rounded_clip(pill)
            snapshot.append_color(SCREEN, _rect((x, y, w, h)))
            snapshot.pop()
            return
        # A notch hangs from the screen's top edge (its side, turned), drawn upright about its
        # middle and turned with the screen: rounded below, curving out into the edge above.
        upright_w, upright_h = (h, w) if self.rotation in (90, 270) else (w, h)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x + w / 2, y + h / 2))
        snapshot.rotate(-self.rotation)
        snapshot.append_fill(notch_path(upright_w, upright_h), Gsk.FillRule.WINDING, SCREEN)
        snapshot.restore()


def notch_path(width: float, height: float) -> Gsk.Path:
    """A notch `width`×`height` about (0, 0), hanging down from its top edge: its lower corners
    rounded, its upper ones curving out into the edge it hangs from."""
    left, right, top, bottom = -width / 2, width / 2, -height / 2, height / 2
    low = min(height, width) * 0.42
    ear = height * 0.22
    path = Gsk.PathBuilder.new()
    path.move_to(left - ear, top - 1)
    path.line_to(left - ear, top)
    path.arc_to(left, top, left, top + ear)
    path.line_to(left, bottom - low)
    path.arc_to(left, bottom, left + low, bottom)
    path.line_to(right - low, bottom)
    path.arc_to(right, bottom, right, bottom - low)
    path.line_to(right, top + ear)
    path.arc_to(right, top, right + ear, top)
    path.line_to(right + ear, top - 1)
    path.close()
    return path.to_path()
