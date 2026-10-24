"""The phone's screen, drawn. Frames come made twice the size it shows (decoder.py), and are
halved here with a plain linear filter, which at exactly half is an even 2×2 average: sharp.
Frames much bigger than that (the window just resized, or a decoder that can't scale) are drawn
with mipmaps instead, so they aren't jagged meanwhile."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402


# Frames up to this many times the size they show are drawn with a linear filter.
LINEAR_UP_TO = 2.2


class Screen(Gtk.Widget):
    """Draws the newest frame over all of itself (an AspectFrame keeps it the phone's shape).
    It answers to Gtk.Picture's set_paintable/get_paintable, for textures. `on_resize(width,
    height)` hears its size in the monitor's pixels whenever it changes."""

    __gtype_name__ = "HandsetScreen"

    def __init__(self, on_resize=None, **kwargs):
        super().__init__(**kwargs)
        self.texture: Gdk.Texture | None = None
        self.on_resize = on_resize

    def scale(self) -> float:
        """The monitor's pixels per window pixel (fractional scaling too)."""
        native = self.get_native()
        surface = native.get_surface() if native else None
        return surface.get_scale() if surface else float(self.get_scale_factor())

    def do_size_allocate(self, width: int, height: int, baseline: int):
        if self.on_resize and width > 0 and height > 0:
            scale = self.scale()
            self.on_resize(round(width * scale), round(height * scale))

    def is_dark(self) -> bool:
        """Whether the frame showing is all but black, as a sleeping phone's screen is: from a
        tiny copy the GPU makes of it (12×24 pixels)."""
        native = self.get_native()
        renderer = native.get_renderer() if native else None
        if self.texture is None or renderer is None:
            return False
        snapshot = Gtk.Snapshot()
        snapshot.append_scaled_texture(self.texture, Gsk.ScalingFilter.TRILINEAR, Graphene.Rect().init(0, 0, 12, 24))
        small = renderer.render_texture(snapshot.to_node(), Graphene.Rect().init(0, 0, 12, 24))
        downloader = Gdk.TextureDownloader.new(small)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        raw = data.get_data()
        brightest = max(max(raw[y * stride + x * 4:y * stride + x * 4 + 3]) for y in range(24) for x in range(12))
        return brightest < 24

    def set_paintable(self, texture: Gdk.Texture | None):
        self.texture = texture
        self.queue_draw()

    def get_paintable(self) -> Gdk.Texture | None:
        return self.texture

    def do_snapshot(self, snapshot: Gtk.Snapshot):
        width, height = self.get_width(), self.get_height()
        if self.texture is None or width <= 0 or height <= 0:
            return
        ratio = self.texture.get_width() / (width * self.scale())
        scaling = Gsk.ScalingFilter.LINEAR if ratio <= LINEAR_UP_TO else Gsk.ScalingFilter.TRILINEAR
        snapshot.append_scaled_texture(self.texture, scaling, Graphene.Rect().init(0, 0, width, height))
