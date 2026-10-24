"""Live video in, GTK textures out: H.264 or H.265 pushed in as it comes, decoded by GStreamer,
and every frame handed over as the decoder's own YUV, which GTK turns into colour on the GPU.

With the graphics card's video driver installed, the GPU decodes, and its frames reach GTK
where they are, in the GPU's memory (DMABuf), never copied: a phone's whole screen at 60 frames
a second then costs the CPU next to nothing. Otherwise (or if that path fails here) frames come
through ordinary memory.

Everything handed back runs on GLib's main loop."""

from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstAllocators", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gdk, GLib, Gst, GstAllocators, GstVideo  # noqa: E402

from .log import debug  # noqa: E402

# Decoders, best first: the GPU's (VA-API: GStreamer has them wherever the graphics card's video
# driver is installed, intel-media-va-driver for Intel's), then GStreamer's libav ones (from
# gstreamer1.0-libav), then the rest.
DECODERS = {"h264": ("vah264dec", "avdec_h264", "openh264dec"),
            "h265": ("vah265dec", "avdec_h265", "libde265dec")}
# The frames' layouts GTK takes as they are from ordinary memory: three planes (I420, from
# software decoders), or luma and interleaved chroma (NV12, from GPUs).
FORMATS = {"I420": Gdk.MemoryFormat.G8_B8_R8_420, "NV12": Gdk.MemoryFormat.G8_B8R8_420}
# Planes in a GPU frame, by its DRM format's name.
DRM_PLANES = {"NV12": 2, "P010": 2, "YU12": 3}


def decoder(codec: str) -> str | None:
    Gst.init(None)
    for name in DECODERS[codec]:
        if Gst.ElementFactory.find(name):
            return name
    return None


def hardware(codec: str) -> bool:
    """Whether the GPU decodes this codec here: full-size video then costs little."""
    return (decoder(codec) or "").startswith("va")


def color_state(colorimetry: GstVideo.VideoColorimetry, size: tuple[int, int]) -> Gdk.ColorState:
    """The colours the video's YUV means, as GTK needs them told: phones' encoders use BT.709 or
    BT.601, mostly in the narrow (16–235) range. The tones are the screen's own, sRGB, whatever
    the encoder writes: told BT.709's curve, GTK would turn them to sRGB and lift every shadow,
    washing the picture out."""
    params = Gdk.CicpParams()
    params.set_color_primaries(1)  # BT.709's, which are sRGB's
    params.set_transfer_function(13)  # sRGB
    matrix = colorimetry.matrix
    # Not said (an iPhone's stream): BT.709 for HD, BT.601 for SD, as GStreamer itself decides
    # (by the frame's height), and Apple and everyone else encode.
    bt709 = matrix == GstVideo.VideoColorMatrix.BT709 or (
        matrix == GstVideo.VideoColorMatrix.UNKNOWN and size[1] > 576)
    params.set_matrix_coefficients(1 if bt709 else 6)
    full = int(colorimetry.range) == 1  # GST_VIDEO_COLOR_RANGE_0_255
    params.set_range(Gdk.CicpRange.FULL if full else Gdk.CicpRange.NARROW)
    return params.build_color_state()


def drm_format(text: str) -> tuple[int, int, int]:
    """A DRM format as caps say it ("NV12:0x0100000000000002"): its fourcc, modifier (0, linear,
    when not said) and number of planes."""
    name, _, modifier = text.partition(":")
    fourcc = int.from_bytes(name.encode().ljust(4, b" ")[:4], "little")
    return fourcc, int(modifier, 16) if modifier else 0, DRM_PLANES.get(name, 1)


class Decoder:
    """One stream's decoder. `push()` takes bytes from any thread; `on_frame(texture)` gets the
    newest frame (a slow window skips the ones in between); `on_broken()` says the decoder
    choked (a stream cut mid-frame, say) and was made afresh, so the stream should start again
    from a picture a decoder can begin with. `crop(frame size)` gives the frames' top left part
    to keep (width, height), or None for all of it: for a stream padded beyond the screen.
    `formats` are the layouts taken from ordinary memory; `gpu_frames` False keeps a GPU's
    frames out of its memory too.

    `show_at(width, height)` says how big the frames show, in the monitor's pixels: they are
    then made twice that size (never more than they are), on the GPU when it decodes, and the
    window halves them exactly. Halving is an even average of every 2×2 pixels, so small text
    stays sharp; drawing a whole phone screen at a quarter of its size or less in one step either
    leaves it jagged or (mipmapped) soft."""

    def __init__(self, codec: str, on_frame: Callable[[Gdk.Texture], None],
                 on_broken: Callable[[], None] | None = None,
                 crop: Callable[[tuple[int, int]], tuple[int, int] | None] | None = None,
                 formats: tuple[str, ...] = ("NV12", "I420"), gpu_frames: bool = True):
        self.codec = codec
        self.formats = formats
        self.gpu_frames = gpu_frames
        self.crop = crop
        self.cropped: tuple[int, int] | None = None
        self.shown_at: tuple[int, int] | None = None  # the frames' size in the window
        self.native: tuple[int, int] | None = None  # the frames' size as decoded
        self.native_crop: tuple[int, int] | None = None
        self.on_frame = on_frame
        self.on_broken = on_broken
        self.pipeline: Gst.Pipeline | None = None
        self.source: Gst.Element | None = None
        self.dmabuf = False  # the frames stay in the GPU's memory
        self.lock = threading.Lock()
        self.latest = None
        self.showing = False
        self.caps = None
        self.color = None
        self.frames = 0
        self.stopped = False

    def start(self) -> bool:
        """False when no decoder for this codec is installed."""
        name = decoder(self.codec)
        if not name:
            return False
        display = Gdk.Display.get_default()
        self.dmabuf = self.gpu_frames and name.startswith("va") and display is not None
        if self.dmabuf:
            scaler = "vapostproc name=scaler scale-method=hq"
            self.base_caps = "video/x-raw(memory:DMABuf),format=DMA_DRM"
        else:
            # The decoder's own 4:2:0 frames: videoconvert only steps in for a decoder that gives
            # a layout GTK doesn't take.
            scaler = "videoconvert ! videoscale name=scaler method=bilinear"
            self.base_caps = f"video/x-raw,format=(string){{{','.join(self.formats)}}}"
        debug("decoding", self.codec, "with", name, "into the GPU's memory" if self.dmabuf else "")
        self.pipeline = Gst.parse_launch(
            f"appsrc name=source is-live=true do-timestamp=true format=time "
            f"caps=video/x-{self.codec},stream-format=byte-stream ! {self.codec}parse ! {name} ! {scaler} ! "
            f'capsfilter name=size caps="{self._size_caps()}" ! '
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
        self.pipeline.get_by_name("scaler").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._decoded_caps)
        self.source = self.pipeline.get_by_name("source")
        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._sample)
        sink.connect("propose-allocation", self._allocation)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._broken)
        self.pipeline.set_state(Gst.State.PLAYING)
        return True

    def show_at(self, width: int, height: int):
        """The frames show this big in the window (the monitor's pixels)."""
        if (width, height) != self.shown_at and width > 0 and height > 0:
            self.shown_at = (width, height)
            self._resize()

    def scaled_size(self) -> tuple[int, int] | None:
        """The size the frames are made: twice what the window shows of the screen, the same
        shape, never more than they are (None: as decoded)."""
        if not self.shown_at or not self.native:
            return None
        (fw, fh), (cw, ch) = self.native, self.native_crop or self.native
        dw, dh = self.shown_at
        scale = min(1.0, 2 * dw / cw, 2 * dh / ch)
        if scale > 0.9:
            return None  # all but their own size: as they are
        return max(2, round(fw * scale / 2) * 2), max(2, round(fh * scale / 2) * 2)

    def _size_caps(self) -> str:
        size = self.scaled_size()
        return self.base_caps + (f",width={size[0]},height={size[1]},pixel-aspect-ratio=1/1" if size else "")

    def _resize(self):
        pipeline = self.pipeline
        if pipeline:
            wanted = Gst.Caps.from_string(self._size_caps())
            capsfilter = pipeline.get_by_name("size")
            if not wanted.is_equal(capsfilter.get_property("caps")):
                debug("frames made", self.scaled_size() or "as decoded", "for", self.shown_at)
                capsfilter.set_property("caps", wanted)
        return GLib.SOURCE_REMOVE

    def _decoded_caps(self, _pad, info):
        """The frames' size as decoded (and the screen's part of them), for scaled_size()."""
        event = info.get_event()
        if event.type == Gst.EventType.CAPS:
            structure = event.parse_caps().get_structure(0)
            native = (structure.get_int("width")[1], structure.get_int("height")[1])
            if native != self.native:
                self.native = native
                self.native_crop = self.crop(native) if self.crop else None
                GLib.idle_add(self._resize)
        return Gst.PadProbeReturn.OK

    def push(self, data: bytes):
        source = self.source
        if source:
            source.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    def stop(self):
        self.stopped = True
        self._drop()

    def _drop(self):
        pipeline, self.pipeline, self.source = self.pipeline, None, None
        if pipeline:
            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)

    @staticmethod
    def _allocation(_sink, query):
        # Frames in the GPU's memory come only to a sink that reads their layout from GStreamer's
        # video meta, which an appsink doesn't say it does: this one does (the planes' offsets and
        # strides in _sample).
        query.add_allocation_meta(GstVideo.video_meta_api_get_type(), None)
        return True

    def _broken(self, _bus, message):
        debug("pipeline error:", message.parse_error()[0].message)
        self._drop()
        if self.stopped:
            return
        if self.dmabuf:
            debug("the GPU's frames didn't work here: through ordinary memory instead")
            self.gpu_frames = False
        self.caps = None
        if self.start() and self.on_broken:
            self.on_broken()

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps()
        if self.caps is None or not caps.is_equal(self.caps):  # a new wrapper every frame
            self.caps = caps
            self._new_caps(caps)
        buffer = sample.get_buffer()
        meta = GstVideo.buffer_get_video_meta(buffer)
        if self.dmabuf:
            planes = []
            for plane in range(self.n_planes):
                _found, index, _length, skip = buffer.find_memory(meta.offset[plane], 1)
                memory = buffer.peek_memory(index)
                planes.append((GstAllocators.dmabuf_memory_get_fd(memory), memory.offset + skip,
                               meta.stride[plane]))
            frame = ("gpu", self.width, self.height, planes, self.color, sample)  # sample: kept alive
        else:
            layout = meta if meta is not None else self.info
            planes = [(layout.offset[i], layout.stride[i]) for i in range(self.info.finfo.n_planes)]
            frame = ("memory", self.width, self.height, planes, self.color,
                     buffer.extract_dup(0, buffer.get_size()))
        with self.lock:
            self.latest = frame
            if self.showing:
                return Gst.FlowReturn.OK
            self.showing = True
        GLib.idle_add(self._show)
        return Gst.FlowReturn.OK

    def _new_caps(self, caps: Gst.Caps):
        structure = caps.get_structure(0)
        self.width, self.height = structure.get_int("width")[1], structure.get_int("height")[1]
        colorimetry = GstVideo.VideoColorimetry()
        colorimetry.from_string(structure.get_string("colorimetry") or "")
        if self.dmabuf:
            self.fourcc, self.modifier, self.n_planes = drm_format(structure.get_string("drm-format") or "")
            what = structure.get_string("drm-format")
        else:
            self.info = GstVideo.VideoInfo.new_from_caps(caps)
            self.memory_format = FORMATS[GstVideo.VideoFormat.to_string(self.info.finfo.format)]
            what = GstVideo.VideoFormat.to_string(self.info.finfo.format)
        native = self.native or (self.width, self.height)
        self.color = color_state(colorimetry, native)
        self.cropped = None
        if self.native_crop:
            # The screen's part, as much smaller as the frames were made.
            (fw, fh), (cw, ch) = native, self.native_crop
            self.cropped = (min(self.width, round(cw * self.width / fw)),
                            min(self.height, round(ch * self.height / fh)))
        debug("video", what, self.width, self.height, "colours", colorimetry.to_string(), "kept", self.cropped)

    def _show(self):
        with self.lock:
            frame, self.latest = self.latest, None
            self.showing = False
        if not frame or self.stopped:
            return GLib.SOURCE_REMOVE
        kind, width, height, planes, color, data = frame
        if self.cropped:
            # The same memory, read less far: the planes' strides stay as they are.
            width, height = min(width, self.cropped[0]), min(height, self.cropped[1])
        if kind == "gpu":
            texture = self._gpu_texture(width, height, planes, color, data)
            if texture is None:
                return GLib.SOURCE_REMOVE
        else:
            # One property at a time, in this order: given together, the colour state can be set
            # before the format and lost, and the frame then shows its raw planes (green, purple).
            builder = Gdk.MemoryTextureBuilder()
            builder.set_width(width)
            builder.set_height(height)
            builder.set_format(self.memory_format)
            builder.set_color_state(color)
            builder.set_bytes(GLib.Bytes.new(data))
            for plane, (offset, stride) in enumerate(planes):
                builder.set_offset(plane, offset)
                builder.set_stride_for_plane(plane, stride)
            texture = builder.build()
        self.on_frame(texture)
        self.frames += 1
        if self.frames in (1, 30) or self.frames % 600 == 0:
            debug("frames shown:", self.frames, f"{width}x{height}")
        return GLib.SOURCE_REMOVE

    def _gpu_texture(self, width, height, planes, color, sample) -> Gdk.Texture | None:
        """A texture of the frame where the GPU decoded it. The sample stays alive until GTK is
        done with the texture, so the decoder doesn't reuse its memory while it shows."""
        builder = Gdk.DmabufTextureBuilder()
        builder.set_display(Gdk.Display.get_default())
        builder.set_width(width)
        builder.set_height(height)
        builder.set_fourcc(self.fourcc)
        builder.set_modifier(self.modifier)
        builder.set_n_planes(len(planes))
        for plane, (fd, offset, stride) in enumerate(planes):
            builder.set_fd(plane, fd)
            builder.set_offset(plane, offset)
            builder.set_stride(plane, stride)
        builder.set_color_state(color)
        try:
            # GTK's data pointer must not be empty; the sample rides in the callback instead.
            return builder.build(lambda *_args, kept=sample: kept, 1)
        except GLib.Error as error:
            debug("a GPU frame GTK didn't take:", error.message)
            self.gpu_frames = False
            GLib.idle_add(self._restart_through_memory)
            return None

    def _restart_through_memory(self):
        if not self.stopped:
            self._drop()
            self.caps = None
            if self.start() and self.on_broken:
                self.on_broken()
        return GLib.SOURCE_REMOVE
