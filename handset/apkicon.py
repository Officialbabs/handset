"""A phone app's launcher label and icon (as a PNG), read straight out of its APK over adb, for
pinning it to the dock (apps.py).

Nothing is installed or launched on the phone. The APK isn't pulled whole: its zip central
directory, AndroidManifest.xml, resources.arsc and the icon's files are read as byte ranges with
`dd` (APKs are world-readable), and checked against the zip's CRCs. Density splits
(split_config.xxhdpi.apk) are read too when the base APK doesn't hold the icon. Adaptive icons
are drawn as the launcher does: both layers at 108 dp, the middle 72 dp kept, under a mask.

Everything read from the APK is untrusted data: it is only parsed, never executed.
"""
import io
import math
import re
import shlex
import struct
import subprocess
import zlib

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Rsvg", "2.0")
from gi.repository import GdkPixbuf, GLib, Rsvg  # noqa: E402

ANDROID_NS = "http://schemas.android.com/apk/res/android"
# Framework attribute ids, for APKs whose XML string pools have the attribute names stripped.
ATTR_IDS = {0x01010001: "label", 0x01010002: "icon", 0x01010003: "name", 0x0101000e: "enabled",
            0x01010199: "drawable", 0x0101052c: "roundIcon", 0x01010202: "targetActivity"}
# A few framework colours, for when framework-res itself isn't worth reading.
FRAMEWORK_COLORS = {0x0106000b: 0xFFFFFFFF, 0x0106000c: 0xFF000000, 0x0106000d: 0x00000000}

T_NULL, T_REF, T_ATTR, T_STRING, T_FLOAT, T_DIM, T_FRACTION, T_DYNREF = 0, 1, 2, 3, 4, 5, 6, 7
T_INT_DEC, T_INT_HEX, T_BOOL = 0x10, 0x11, 0x12
COLOR_TYPES = (0x1C, 0x1D, 0x1E, 0x1F)
ANYDPI, NODPI = 0xFFFE, 0xFFFF


class Stats:
    adb_calls = 0
    bytes_read = 0
    warnings: list = []


def warn(message):
    if message not in Stats.warnings:
        Stats.warnings.append(message)


# ---------------------------------------------------------------- byte sources and zips

class LocalFile:
    def __init__(self, path):
        self.name = path
        self.file = open(path, "rb")
        self.file.seek(0, 2)
        self.size = self.file.tell()

    def read(self, offset, length):
        self.file.seek(offset)
        data = self.file.read(length)
        Stats.bytes_read += len(data)
        return data


class AdbFile:
    """A file on the phone read in byte ranges (block-aligned dd, so it seeks)."""
    BLOCK = 4096

    def __init__(self, serial, path, size):
        self.serial, self.name, self.size = serial, path, size

    def read(self, offset, length):
        length = max(0, min(length, self.size - offset))
        if not length:
            return b""
        first, last = offset // self.BLOCK, (offset + length - 1) // self.BLOCK
        command = (f"dd if={shlex.quote(self.name)} bs={self.BLOCK} skip={first} "
                   f"count={last - first + 1} 2>/dev/null")
        data = adb_out(self.serial, command)
        Stats.bytes_read += len(data)
        start = offset - first * self.BLOCK
        chunk = data[start:start + length]
        if len(chunk) != length:
            raise OSError(f"short read from {self.name} at {offset}: {len(chunk)}/{length}")
        return chunk


class Zip:
    def __init__(self, source):
        self.source = source
        tail_len = min(source.size, 65557)
        tail = source.read(source.size - tail_len, tail_len)
        end = tail.rfind(b"PK\x05\x06")
        if end < 0:
            raise ValueError(f"{source.name}: not a zip")
        count, cd_size, cd_offset = struct.unpack_from("<HII", tail, end + 10)
        tail_start = source.size - tail_len
        if cd_offset >= tail_start:  # the central directory is inside the tail already
            cd = tail[cd_offset - tail_start:cd_offset - tail_start + cd_size]
        else:
            cd = source.read(cd_offset, cd_size)
        self.entries = {}
        p = 0
        for _ in range(count):
            if cd[p:p + 4] != b"PK\x01\x02":
                raise ValueError(f"{source.name}: bad central directory")
            (method, crc, csize, usize, nlen, elen, clen,
             local) = struct.unpack_from("<10xH4xIIIHHH8xI", cd, p)
            name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
            self.entries[name] = (method, crc, csize, usize, local, elen)
            p += 46 + nlen + elen + clen

    def read(self, name):
        method, crc, csize, usize, local, elen = self.entries[name]
        slack = 64
        head = self.source.read(local, 30 + len(name.encode()) + elen + slack + csize)
        if head[:4] != b"PK\x03\x04":
            raise ValueError(f"{name}: bad local header")
        nlen, lelen = struct.unpack_from("<HH", head, 26)
        start = 30 + nlen + lelen
        if start + csize > len(head):  # a long alignment extra field: read the data again
            head = self.source.read(local + start, csize)
            start = 0
        raw = head[start:start + csize]
        if method == 0:
            data = raw
        elif method == 8:
            data = zlib.decompress(raw, -15)
        else:
            raise ValueError(f"{name}: compression method {method}")
        if zlib.crc32(data) != crc or len(data) != usize:
            raise ValueError(f"{name}: CRC mismatch")
        return data


# ---------------------------------------------------------------- binary XML and string pools

class StringPool:
    def __init__(self, buf, off):
        _, header_size, _ = struct.unpack_from("<HHI", buf, off)
        count, _, flags, strings_start, _ = struct.unpack_from("<IIIII", buf, off + 8)
        self.buf, self.utf8 = buf, bool(flags & 0x100)
        self.offsets = struct.unpack_from(f"<{count}I", buf, off + header_size)
        self.base = off + strings_start
        self.cache = {}

    def __len__(self):
        return len(self.offsets)

    def get(self, i):
        if i is None or i >= len(self.offsets):
            return None
        if i in self.cache:
            return self.cache[i]
        buf, p = self.buf, self.base + self.offsets[i]
        if self.utf8:
            for _ in range(2):  # char count, then byte count
                n = buf[p]
                p += 1
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | buf[p]
                    p += 1
            s = buf[p:p + n].decode("utf-8", "replace")
        else:
            n = struct.unpack_from("<H", buf, p)[0]
            p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", buf, p)[0]
                p += 2
            s = buf[p:p + 2 * n].decode("utf-16-le", "replace")
        self.cache[i] = s
        return s


class Value:
    __slots__ = ("type", "data", "string")

    def __init__(self, type_, data, string=None):
        self.type, self.data, self.string = type_, data, string

    def __repr__(self):
        return f"Value({self.type:#x}, {self.data:#x}, {self.string!r})"

    @property
    def float(self):
        return struct.unpack("<f", struct.pack("<I", self.data))[0]


class Element:
    def __init__(self, tag):
        self.tag, self.attrs, self.children = tag, {}, []

    def get(self, name):
        return self.attrs.get(name)


def parse_axml(buf):
    """Compiled XML → Element tree; attributes keyed by local name (android: namespace only,
    plus un-namespaced ones), valued as Value."""
    typ, header_size, size = struct.unpack_from("<HHI", buf, 0)
    if typ != 0x0003:
        raise ValueError("not binary XML")
    pool, resmap, root, stack = None, (), None, []
    p = header_size
    while p + 8 <= min(size, len(buf)):
        ctype, chsize, csize = struct.unpack_from("<HHI", buf, p)
        if csize < 8:
            break
        if ctype == 0x0001:
            pool = StringPool(buf, p)
        elif ctype == 0x0180:
            resmap = struct.unpack_from(f"<{(csize - chsize) // 4}I", buf, p + chsize)
        elif ctype == 0x0102:
            x = p + chsize
            _ns, name, astart, asize, acount = struct.unpack_from("<IIHHH", buf, x)
            element = Element(pool.get(name))
            for i in range(acount):
                a = x + astart + i * asize
                ans, aname, raw, _vs, _r0, vtype, vdata = struct.unpack_from("<IIIHBBI", buf, a)
                key = pool.get(aname) or ""
                if aname < len(resmap) and resmap[aname] in ATTR_IDS and not key:
                    key = ATTR_IDS[resmap[aname]]
                namespace = pool.get(ans) if ans != 0xFFFFFFFF else None
                if namespace not in (None, ANDROID_NS):
                    key = f"{{{namespace}}}{key}"
                string = pool.get(raw) if raw != 0xFFFFFFFF else None
                if vtype == T_STRING:
                    string = pool.get(vdata)
                element.attrs[key] = Value(vtype, vdata, string)
            if stack:
                stack[-1].children.append(element)
            else:
                root = element
            stack.append(element)
        elif ctype == 0x0103:
            if stack:
                stack.pop()
        p += csize
    return root


# ---------------------------------------------------------------- resources.arsc

class Config:
    def __init__(self, buf, c):
        size = struct.unpack_from("<I", buf, c)[0]
        raw = bytes(buf[c:c + size]) + bytes(max(0, 64 - size))
        self.mcc, self.mnc = struct.unpack_from("<HH", raw, 4)
        self.language = unpack_locale(raw[8:10], ord("a"))
        self.country = unpack_locale(raw[10:12], ord("0"))
        self.orientation, self.density = raw[12], struct.unpack_from("<H", raw, 14)[0]
        self.sdk = struct.unpack_from("<H", raw, 24)[0]
        self.ui_mode, self.sw_dp = raw[29], struct.unpack_from("<H", raw, 30)[0]
        self.w_dp, self.h_dp = struct.unpack_from("<HH", raw, 32)
        self.script = raw[36:40].rstrip(b"\0").decode("ascii", "replace")

    def describe(self):
        parts = []
        if self.language:
            parts.append(self.language + (f"-r{self.country}" if self.country else ""))
        if self.ui_mode & 0x30 == 0x20:
            parts.append("night")
        if self.density:
            parts.append({ANYDPI: "anydpi", NODPI: "nodpi", 120: "ldpi", 160: "mdpi", 240: "hdpi",
                          320: "xhdpi", 480: "xxhdpi", 640: "xxxhdpi"}.get(self.density,
                                                                           f"{self.density}dpi"))
        if self.sdk:
            parts.append(f"v{self.sdk}")
        return "-".join(parts) or "default"


def unpack_locale(two, base):
    if not two[0]:
        return ""
    if two[0] & 0x80:  # packed three-letter code
        first = two[1] & 0x1F
        second = ((two[1] & 0xE0) >> 5) | ((two[0] & 0x03) << 3)
        third = (two[0] & 0x7C) >> 2
        return "".join(chr(base + v) for v in (first, second, third))
    return two.decode("ascii", "replace")


class TypeChunk:
    def __init__(self, buf, p, header_size, flags, count, entries_start, config, keys, strings, apk):
        self.buf, self.p, self.header_size, self.flags = buf, p, header_size, flags
        self.count, self.entries_start, self.config = count, entries_start, config
        self.keys, self.strings, self.apk = keys, strings, apk

    def entry_offset(self, eid):
        buf, base = self.buf, self.p + self.header_size
        if self.flags & 0x01:  # sparse: sorted (index, offset/4) pairs
            lo, hi = 0, self.count - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                idx, off = struct.unpack_from("<HH", buf, base + 4 * mid)
                if idx == eid:
                    return off * 4
                lo, hi = (mid + 1, hi) if idx < eid else (lo, mid - 1)
            return None
        if eid >= self.count:
            return None
        if self.flags & 0x02:  # 16-bit offsets
            off = struct.unpack_from("<H", buf, base + 2 * eid)[0]
            return None if off == 0xFFFF else off * 4
        off = struct.unpack_from("<I", buf, base + 4 * eid)[0]
        return None if off == 0xFFFFFFFF else off

    def entry(self, eid):
        """(key name, Value) for a simple entry, (key name, {attr id: Value}) for a bag."""
        off = self.entry_offset(eid)
        if off is None:
            return None
        buf, e = self.buf, self.p + self.entries_start + off
        size, flags = struct.unpack_from("<HH", buf, e)
        if flags & 0x08:  # compact entry (Android 14+)
            data = struct.unpack_from("<I", buf, e + 4)[0]
            return self.keys.get(size), self.value(flags >> 8, data)
        key = self.keys.get(struct.unpack_from("<I", buf, e + 4)[0])
        if flags & 0x01:
            _parent, n = struct.unpack_from("<II", buf, e + 8)
            bag, q = {}, e + size
            for _ in range(n):
                name, _vs, _r0, vtype, vdata = struct.unpack_from("<IHBBI", buf, q)
                bag[name] = self.value(vtype, vdata)
                q += 12
            return key, bag
        _vs, _r0, vtype, vdata = struct.unpack_from("<HBBI", buf, e + size)
        return key, self.value(vtype, vdata)

    def value(self, vtype, data):
        return Value(vtype, data, self.strings.get(data) if vtype == T_STRING else None)


class Package:
    def __init__(self, pid, name):
        self.id, self.name, self.types, self.type_names = pid, name, {}, {}


class ResTable:
    def __init__(self, device):
        self.packages, self.device = {}, device

    def add(self, buf, apk):
        typ, header_size, size = struct.unpack_from("<HHI", buf, 0)
        if typ != 0x0002:
            raise ValueError("not a resource table")
        strings, p = None, header_size
        while p + 8 <= size:
            ctype, _chs, csize = struct.unpack_from("<HHI", buf, p)
            if ctype == 0x0001:
                strings = StringPool(buf, p)
            elif ctype == 0x0200:
                self._package(buf, p, strings, apk)
            p += csize

    def _package(self, buf, off, strings, apk):
        _, header_size, size = struct.unpack_from("<HHI", buf, off)
        pid = struct.unpack_from("<I", buf, off + 8)[0]
        name = bytes(buf[off + 12:off + 268]).decode("utf-16-le", "replace").split("\0")[0]
        type_strings, _, key_strings, _ = struct.unpack_from("<IIII", buf, off + 268)
        id_offset = struct.unpack_from("<I", buf, off + 284)[0] if header_size >= 288 else 0
        package = self.packages.setdefault(pid, Package(pid, name))
        type_pool = keys = None
        p = off + header_size
        while p + 8 <= off + size:
            ctype, chs, csize = struct.unpack_from("<HHI", buf, p)
            if ctype == 0x0001:
                if p - off == type_strings:
                    type_pool = StringPool(buf, p)
                elif p - off == key_strings:
                    keys = StringPool(buf, p)
            elif ctype == 0x0201:
                tid, flags = buf[p + 8], buf[p + 9]
                count, entries_start = struct.unpack_from("<II", buf, p + 12)
                chunk = TypeChunk(buf, p, chs, flags, count, entries_start, Config(buf, p + 20),
                                  keys, strings, apk)
                package.types.setdefault(tid, []).append(chunk)
            p += csize
        if type_pool:
            for i in range(len(type_pool)):
                package.type_names[i + 1 + id_offset] = type_pool.get(i)

    # -- choosing a configuration the way the phone would (portrait, day, its locale)

    def acceptable(self, cfg):
        dev = self.device
        return (cfg.mcc == 0 and cfg.mnc == 0 and cfg.orientation in (0, 1)
                and cfg.ui_mode & 0x0F in (0, 1) and cfg.ui_mode & 0x30 in (0, 0x10)
                and cfg.sdk <= dev["sdk"] and cfg.sw_dp <= dev["sw_dp"]
                and cfg.w_dp <= dev["sw_dp"] and not (cfg.language and cfg.language not in
                                                      (dev["language"],)))

    def locale_score(self, cfg):
        if not cfg.language:
            return 2
        if cfg.country == self.device["country"]:
            return 4
        return 3 if not cfg.country else 1

    @staticmethod
    def density_score(cfg):
        return {ANYDPI: 100000, NODPI: 700, 0: 160}.get(cfg.density, cfg.density)

    def candidates(self, resid):
        package = self.packages.get(resid >> 24)
        if not package:
            return []
        found = []
        for chunk in package.types.get((resid >> 16) & 0xFF, []):
            if self.acceptable(chunk.config):
                entry = chunk.entry(resid & 0xFFFF)
                if entry is not None:
                    found.append((chunk, entry))
        return found

    def all_configs(self, resid):
        package = self.packages.get(resid >> 24)
        out = []
        for chunk in (package.types.get((resid >> 16) & 0xFF, []) if package else []):
            entry = chunk.entry(resid & 0xFFFF)
            if entry is not None:
                value = entry[1]
                what = value.string if isinstance(value, Value) and value.string else repr(value)
                out.append(f"{chunk.config.describe()}: {what} [{chunk.apk.source.name.rsplit('/', 1)[-1]}]")
        return out

    def best(self, resid, depth=0):
        """The (chunk, Value) the phone would use for resid, following references."""
        found = self.candidates(resid)
        if not found:
            return None
        chunk, (_key, value) = max(found, key=lambda f: (self.locale_score(f[0].config),
                                                         self.density_score(f[0].config),
                                                         f[0].config.sdk))
        if isinstance(value, Value) and value.type in (T_REF, T_DYNREF) and value.data and depth < 20:
            return self.best(value.data, depth + 1) or (chunk, value)
        return chunk, value

    def name(self, resid):
        package = self.packages.get(resid >> 24)
        if not package:
            return f"{resid:#010x}"
        type_name = package.type_names.get((resid >> 16) & 0xFF, "?")
        for chunk in package.types.get((resid >> 16) & 0xFF, []):
            entry = chunk.entry(resid & 0xFFFF)
            if entry:
                return f"@{type_name}/{entry[0]}"
        return f"@{type_name}/{resid:#x}"


# ---------------------------------------------------------------- the app

class App:
    def __init__(self, package, apks, device, framework_apk=None):
        self.package = package
        self.apks = apks  # [Zip], base first
        self.framework_apk = framework_apk  # a callable giving a Zip, read only if needed
        self.table = ResTable(device)
        self.manifest = parse_axml(apks[0].read("AndroidManifest.xml"))
        self.table.add(apks[0].read("resources.arsc"), apks[0])
        self.splits_loaded = False
        self.framework_loaded = False

    def load_splits(self):
        """Density/locale splits carry their own resources.arsc with extra configurations."""
        if self.splits_loaded:
            return
        self.splits_loaded = True
        for apk in self.apks[1:]:
            if "resources.arsc" in apk.entries:
                self.table.add(apk.read("resources.arsc"), apk)

    def load_framework(self):
        if self.framework_loaded or not self.framework_apk:
            return
        self.framework_loaded = True
        try:
            zip_ = self.framework_apk()
            self.table.add(zip_.read("resources.arsc"), zip_)
        except Exception as error:  # noqa: BLE001
            warn(f"framework-res unreadable: {error}")

    def best(self, resid):
        if resid >> 24 == 0x01:
            self.load_framework()
        found = self.table.best(resid)
        if found is None and resid >> 24 != 0x01:
            self.load_splits()
            found = self.table.best(resid)
        return found

    def launcher_components(self):
        """[(activity element, its name)] with a MAIN/LAUNCHER intent filter."""
        app = next((c for c in self.manifest.children if c.tag == "application"), None)
        result = []
        for child in (app.children if app else []):
            if child.tag not in ("activity", "activity-alias"):
                continue
            for f in child.children:
                if f.tag != "intent-filter":
                    continue
                actions = {a.get("name").string for a in f.children if a.tag == "action" and a.get("name")}
                cats = {a.get("name").string for a in f.children if a.tag == "category" and a.get("name")}
                if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in cats:
                    name = child.get("name").string if child.get("name") else "?"
                    if name.startswith("."):
                        name = self.package + name
                    result.append((child, name))
        return app, result

    def string(self, value):
        if value is None:
            return None
        if value.type == T_STRING:
            return value.string
        if value.type in (T_REF, T_DYNREF):
            found = self.best(value.data)
            if found and isinstance(found[1], Value) and found[1].type == T_STRING:
                return found[1].string
        return None

    def file(self, chunk, path):
        for apk in [chunk.apk] + self.apks:
            if path in apk.entries:
                return apk.read(path)
        raise KeyError(path)


# ---------------------------------------------------------------- drawing

def rgba(argb):
    return ((argb >> 16) & 255) / 255, ((argb >> 8) & 255) / 255, (argb & 255) / 255, ((argb >> 24) & 255) / 255


def css(argb, alpha=1.0):
    r, g, b, a = rgba(argb)
    return f"rgb({round(r * 255)},{round(g * 255)},{round(b * 255)})", a * alpha


def complex_value(data):
    mantissa = data & 0xFFFFFF00
    if mantissa & 0x80000000:
        mantissa -= 1 << 32
    return mantissa * (1 / (1 << 8), 1 / (1 << 15), 1 / (1 << 23), 1 / (1 << 31))[(data >> 4) & 3]


class Painter:
    def __init__(self, app):
        self.app = app
        self.kinds = []  # what was drawn, for the report
        self.ids = 0

    # -- values

    def number(self, value, default=0.0):
        if value is None:
            return default
        if value.type == T_FLOAT:
            return value.float
        if value.type in (T_DIM, T_FRACTION):
            return complex_value(value.data)
        if value.type in (T_INT_DEC, T_INT_HEX):
            return float(struct.unpack("<i", struct.pack("<I", value.data))[0])
        if value.type in (T_REF, T_DYNREF):
            found = self.app.best(value.data)
            if found and isinstance(found[1], Value):
                return self.number(found[1], default)
        if value.type == T_STRING and value.string:
            try:
                return float(re.sub(r"[a-z%]+$", "", value.string))
            except ValueError:
                pass
        return default

    def length(self, value, extent, dp):
        """A dimension in px (dp-scaled) or a fraction of extent."""
        if value is None:
            return 0.0
        if value.type == T_FRACTION or (value.type in (T_REF, T_DYNREF) and self._is_fraction(value)):
            return self.number(value) * extent
        if value.type == T_DIM and value.data & 0xF == 0:  # px
            return self.number(value) * dp / 2.625  # assume the phone's 420 dpi
        return self.number(value) * dp

    def _is_fraction(self, value):
        found = self.app.best(value.data)
        return bool(found and isinstance(found[1], Value) and found[1].type == T_FRACTION)

    def color(self, value, depth=0):
        """ARGB int, ('gradient', Element, chunk) or None."""
        if value is None or depth > 10:
            return None
        if value.type in COLOR_TYPES:
            return value.data
        if value.type == T_STRING and value.string and value.string.startswith("#"):
            return parse_hex_color(value.string)
        if value.type in (T_ATTR, 0x08):
            warn(f"theme attribute colour ?{value.data:#x} (no theme here): grey stand-in")
            return 0xFF9E9E9E
        if value.type in (T_REF, T_DYNREF):
            if value.data in FRAMEWORK_COLORS and not self.app.framework_apk:
                return FRAMEWORK_COLORS[value.data]
            found = self.app.best(value.data)
            if not found:
                if value.data in FRAMEWORK_COLORS:
                    return FRAMEWORK_COLORS[value.data]
                warn(f"unresolved colour {value.data:#010x}")
                return None
            chunk, v = found
            if isinstance(v, Value) and v.type == T_STRING and v.string and v.string.endswith(".xml"):
                root = parse_axml(self.app.file(chunk, v.string))
                if root.tag == "selector":  # colour state list: the first stateless item
                    items = [i for i in root.children if i.tag == "item"]
                    plain = [i for i in items if not any(k.startswith("state_") for k in i.attrs)]
                    item = (plain or items or [None])[0]
                    if item is None:
                        return None
                    c = self.color(item.get("color"), depth + 1)
                    if isinstance(c, int) and item.get("alpha") is not None:
                        a = round(((c >> 24) & 255) * self.number(item.get("alpha"), 1.0))
                        c = (c & 0xFFFFFF) | (max(0, min(255, a)) << 24)
                    return c
                if root.tag == "gradient":
                    return ("gradient", root, chunk)
                warn(f"colour file <{root.tag}> unsupported")
                return None
            if isinstance(v, Value):
                return self.color(v, depth + 1)
        return None

    # -- drawables

    def draw_ref(self, cr, value, x, y, w, h, dp, chunk=None, depth=0):
        """Draw an attribute value (reference, colour or file) into the rect."""
        if value is None or depth > 10:
            return
        if value.type in COLOR_TYPES:
            self.fill(cr, value.data, x, y, w, h)
            return
        if value.type not in (T_REF, T_DYNREF):
            warn(f"drawable value type {value.type:#x} unsupported")
            return
        found = self.app.best(value.data)
        if not found:
            warn(f"unresolved drawable {value.data:#010x}")
            return
        chunk, v = found
        if isinstance(v, Value) and v.type in COLOR_TYPES:
            self.kinds.append("colour")
            self.fill(cr, v.data, x, y, w, h)
        elif isinstance(v, Value) and v.type == T_STRING and v.string:
            self.draw_file(cr, chunk, v.string, x, y, w, h, dp)
        elif isinstance(v, Value) and v.type in (T_REF, T_DYNREF):
            # A name for another resource: a colour (often the framework's own) or a drawable.
            c = self.color(v)
            if isinstance(c, int):
                self.kinds.append("colour")
                self.fill(cr, c, x, y, w, h)
            else:
                self.draw_ref(cr, v, x, y, w, h, dp, depth=depth + 1)
        else:
            warn(f"drawable {self.app.table.name(value.data)} is {v!r}")

    def draw_file(self, cr, chunk, path, x, y, w, h, dp):
        data = self.app.file(chunk, path)
        if data[:4] == b"\x03\x00\x08\x00":
            self.draw_element(cr, parse_axml(data), chunk, x, y, w, h, dp)
        else:
            surface = image_surface(data)
            self.kinds.append(f"bitmap {surface.get_width()}x{surface.get_height()} "
                              f"({chunk.config.describe()})")
            cr.save()
            cr.translate(x, y)
            cr.scale(w / surface.get_width(), h / surface.get_height())
            cr.set_source_surface(surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_BEST)
            cr.paint()
            cr.restore()

    def fill(self, cr, argb, x, y, w, h):
        cr.save()
        cr.set_source_rgba(*rgba(argb))
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.restore()

    def child_or_attr(self, cr, element, chunk, x, y, w, h, dp):
        if element.get("drawable") is not None:
            self.draw_ref(cr, element.get("drawable"), x, y, w, h, dp)
        elif element.children:
            self.draw_element(cr, element.children[0], chunk, x, y, w, h, dp)

    def draw_element(self, cr, el, chunk, x, y, w, h, dp):
        tag = el.tag
        if tag == "vector":
            self.kinds.append("vector")
            self.draw_vector(cr, el, x, y, w, h)
        elif tag == "adaptive-icon":
            self.kinds.append("adaptive-icon")
            for layer in ("background", "foreground"):
                part = next((c for c in el.children if c.tag == layer), None)
                if part is not None:
                    self.child_or_attr(cr, part, chunk, x, y, w, h, dp)
        elif tag == "inset":
            def side(name):
                v = el.get(name)
                return v if v is not None else el.get("inset")
            left = self.length(side("insetLeft"), w, dp)
            right = self.length(side("insetRight"), w, dp)
            top = self.length(side("insetTop"), h, dp)
            bottom = self.length(side("insetBottom"), h, dp)
            self.kinds.append("inset")
            self.child_or_attr(cr, el, chunk, x + left, y + top, w - left - right, h - top - bottom, dp)
        elif tag == "layer-list":
            for item in el.children:
                if item.tag != "item":
                    continue
                l, t = self.length(item.get("left"), w, dp), self.length(item.get("top"), h, dp)
                r, b = self.length(item.get("right"), w, dp), self.length(item.get("bottom"), h, dp)
                iw, ih = w - l - r, h - t - b
                ix, iy = x + l, y + t
                if item.get("width") is not None:
                    nw = self.length(item.get("width"), w, dp)
                    ix, iw = ix + (iw - nw) / 2, nw
                if item.get("height") is not None:
                    nh = self.length(item.get("height"), h, dp)
                    iy, ih = iy + (ih - nh) / 2, nh
                self.child_or_attr(cr, item, chunk, ix, iy, iw, ih, dp)
        elif tag == "bitmap":
            src = el.get("src")
            if src is not None:
                self.draw_ref(cr, src, x, y, w, h, dp)
        elif tag in ("selector", "level-list", "animation-list"):
            items = [c for c in el.children if c.tag == "item"]
            if items:
                self.child_or_attr(cr, items[-1] if tag == "selector" else items[0], chunk, x, y, w, h, dp)
        elif tag in ("animated-vector", "animated-selector"):
            self.child_or_attr(cr, el, chunk, x, y, w, h, dp)
        elif tag in ("color",):
            c = self.color(el.get("color"))
            if isinstance(c, int):
                self.fill(cr, c, x, y, w, h)
        elif tag == "shape":
            self.draw_shape(cr, el, x, y, w, h, dp)
        elif tag == "rotate":
            # A turning drawable (a clock's hands) as it stands at the start of its turn.
            degrees = self.number(el.get("fromDegrees"))
            px = x + (self.length(el.get("pivotX"), w, dp) if el.get("pivotX") is not None else w / 2)
            py = y + (self.length(el.get("pivotY"), h, dp) if el.get("pivotY") is not None else h / 2)
            cr.save()
            cr.translate(px, py)
            cr.rotate(math.radians(degrees))
            cr.translate(-px, -py)
            self.kinds.append("rotate")
            self.child_or_attr(cr, el, chunk, x, y, w, h, dp)
            cr.restore()
        else:
            warn(f"<{tag}> drawable unsupported")

    def draw_shape(self, cr, el, x, y, w, h, dp):
        self.kinds.append("shape")
        kind = el.get("shape").data if el.get("shape") is not None else 0
        solid = next((c for c in el.children if c.tag == "solid"), None)
        corners = next((c for c in el.children if c.tag == "corners"), None)
        cr.save()
        if kind == 1:
            cr.save()
            cr.translate(x + w / 2, y + h / 2)
            cr.scale(w / 2, h / 2)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
        else:
            radius = self.length(corners.get("radius"), w, dp) if corners is not None else 0
            rounded_rect(cr, x, y, w, h, min(radius, w / 2, h / 2))
        c = self.color(solid.get("color")) if solid is not None else None
        if isinstance(c, int):
            cr.set_source_rgba(*rgba(c))
            cr.fill()
        else:
            gradient = next((c for c in el.children if c.tag == "gradient"), None)
            if gradient is not None:
                start = self.color(gradient.get("startColor")) or 0
                end = self.color(gradient.get("endColor")) or 0
                angle = math.radians(self.number(gradient.get("angle"), 0))
                dx, dy = math.cos(angle) * w / 2, -math.sin(angle) * h / 2
                pat = cairo.LinearGradient(x + w / 2 - dx, y + h / 2 - dy, x + w / 2 + dx, y + h / 2 + dy)
                pat.add_color_stop_rgba(0, *rgba(start))
                pat.add_color_stop_rgba(1, *rgba(end))
                cr.set_source(pat)
                cr.fill()
        cr.restore()

    # -- vector drawables, via SVG and librsvg

    def draw_vector(self, cr, el, x, y, w, h):
        svg = self.vector_svg(el)
        handle = Rsvg.Handle.new_from_data(svg.encode())
        rect = Rsvg.Rectangle()
        rect.x, rect.y, rect.width, rect.height = x, y, w, h
        handle.render_document(cr, rect)

    def vector_svg(self, el):
        vw = self.number(el.get("viewportWidth"), 24)
        vh = self.number(el.get("viewportHeight"), 24)
        defs, body = [], []
        if el.get("tint") is not None:
            warn("vector android:tint ignored")
        alpha = self.number(el.get("alpha"), 1.0)
        self.svg_children(el, body, defs)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw} {vh}" width="{vw}" '
                f'height="{vh}" preserveAspectRatio="none"><defs>{"".join(defs)}</defs>'
                f'<g opacity="{alpha}">{"".join(body)}</g></svg>')

    def svg_children(self, el, out, defs):
        open_clips = 0
        for child in el.children:
            if child.tag == "group":
                px, py = self.number(child.get("pivotX")), self.number(child.get("pivotY"))
                tx, ty = self.number(child.get("translateX")), self.number(child.get("translateY"))
                sx, sy = self.number(child.get("scaleX"), 1), self.number(child.get("scaleY"), 1)
                rot = self.number(child.get("rotation"))
                inner = []
                self.svg_children(child, inner, defs)
                out.append(f'<g transform="translate({tx + px},{ty + py}) rotate({rot}) '
                           f'scale({sx},{sy}) translate({-px},{-py})">{"".join(inner)}</g>')
            elif child.tag == "clip-path":
                self.ids += 1
                d = self.path_data(child.get("pathData"))
                defs.append(f'<clipPath id="c{self.ids}"><path d="{d}"/></clipPath>')
                out.append(f'<g clip-path="url(#c{self.ids})">')
                open_clips += 1
            elif child.tag == "path":
                out.append(self.svg_path(child, defs))
        out.append("</g>" * open_clips)

    def path_data(self, value):
        if value is None:
            return ""
        s = value.string if value.type == T_STRING else None
        if s is None and value.type in (T_REF, T_DYNREF):
            found = self.app.best(value.data)
            s = found[1].string if found and isinstance(found[1], Value) else ""
        return xml_escape(s or "")

    def paint(self, value, alpha, defs):
        c = self.color(value)
        if c is None:
            return "none", 1
        if isinstance(c, tuple):
            return f"url(#{self.svg_gradient(c[1], defs)})", alpha
        colour, a = css(c, alpha)
        return colour, a

    def svg_gradient(self, g, defs):
        self.ids += 1
        gid = f"g{self.ids}"
        kind = g.get("type").data if g.get("type") is not None else 0
        stops = []
        items = [i for i in g.children if i.tag == "item"]
        for item in items:
            c = self.color(item.get("color"))
            if isinstance(c, int):
                colour, a = css(c)
                stops.append(f'<stop offset="{self.number(item.get("offset"))}" stop-color="{colour}" stop-opacity="{a}"/>')
        if not items:
            for offset, name in ((0, "startColor"), (0.5, "centerColor"), (1, "endColor")):
                c = self.color(g.get(name))
                if isinstance(c, int):
                    colour, a = css(c)
                    stops.append(f'<stop offset="{offset}" stop-color="{colour}" stop-opacity="{a}"/>')
        spread = {1: "repeat", 2: "reflect"}.get(g.get("tileMode").data if g.get("tileMode") else 0, "pad")
        n = self.number
        if kind == 1:
            defs.append(f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" cx="{n(g.get("centerX"))}" '
                        f'cy="{n(g.get("centerY"))}" r="{n(g.get("gradientRadius"))}" spreadMethod="{spread}">'
                        f'{"".join(stops)}</radialGradient>')
        else:
            if kind == 2:
                warn("sweep gradient drawn as linear")
            defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{n(g.get("startX"))}" '
                        f'y1="{n(g.get("startY"))}" x2="{n(g.get("endX"))}" y2="{n(g.get("endY"))}" '
                        f'spreadMethod="{spread}">{"".join(stops)}</linearGradient>')
        return gid

    def svg_path(self, el, defs):
        d = self.path_data(el.get("pathData"))
        fill, fill_a = self.paint(el.get("fillColor"), self.number(el.get("fillAlpha"), 1), defs)
        stroke, stroke_a = self.paint(el.get("strokeColor"), self.number(el.get("strokeAlpha"), 1), defs)
        rule = "evenodd" if el.get("fillType") is not None and el.get("fillType").data == 1 else "nonzero"
        cap = ("butt", "round", "square")[min(el.get("strokeLineCap").data, 2)] if el.get("strokeLineCap") else "butt"
        join = ("miter", "round", "bevel")[min(el.get("strokeLineJoin").data, 2)] if el.get("strokeLineJoin") else "miter"
        if el.get("trimPathEnd") is not None or el.get("trimPathStart") is not None:
            warn("path trimming ignored")
        return (f'<path d="{d}" fill="{fill}" fill-opacity="{fill_a}" fill-rule="{rule}" '
                f'stroke="{stroke}" stroke-opacity="{stroke_a}" '
                f'stroke-width="{self.number(el.get("strokeWidth"), 0)}" stroke-linecap="{cap}" '
                f'stroke-linejoin="{join}" stroke-miterlimit="{self.number(el.get("strokeMiterLimit"), 4)}"/>')


def xml_escape(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def parse_hex_color(s):
    s = s.lstrip("#")
    if len(s) in (3, 4):
        s = "".join(ch * 2 for ch in s)
    if len(s) == 6:
        s = "ff" + s
    return int(s, 16)


def rounded_rect(cr, x, y, w, h, r):
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def image_surface(data):
    """PNG/WebP/JPEG bytes → cairo surface, decoded by GdkPixbuf (re-encoded as PNG for cairo)."""
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    ok, png = pixbuf.save_to_bufferv("png", [], [])
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def mask_path(cr, shape, size):
    if shape == "circle":
        cr.arc(size / 2, size / 2, size / 2, 0, 2 * math.pi)
    elif shape == "rounded":
        rounded_rect(cr, 0, 0, size, size, size * 0.24)
    elif shape == "squircle":  # superellipse, n = 5
        steps = 256
        for i in range(steps):
            t = 2 * math.pi * i / steps
            c, s = math.cos(t), math.sin(t)
            px = size / 2 + size / 2 * math.copysign(abs(c) ** (2 / 5), c)
            py = size / 2 + size / 2 * math.copysign(abs(s) ** (2 / 5), s)
            (cr.move_to if i == 0 else cr.line_to)(px, py)
        cr.close_path()
    else:
        cr.rectangle(0, 0, size, size)


def render_icon(app, value, size, shape):
    """The icon as a size×size ARGB surface, plus the painter (for what it drew)."""
    painter = Painter(app)
    found = app.best(value.data) if value.type in (T_REF, T_DYNREF) else None
    adaptive = False
    if found and isinstance(found[1], Value) and found[1].string and found[1].string.endswith(".xml"):
        root = parse_axml(app.file(found[0], found[1].string))
        adaptive = root.tag == "adaptive-icon"
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(out)
    if adaptive:
        # Layers are 108 dp, the visible icon is the middle 72 dp under the mask.
        layers = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        lc = cairo.Context(layers)
        full = size * 1.5
        painter.draw_ref(lc, value, -size / 4, -size / 4, full, full, full / 108)
        mask_path(cr, shape, size)
        cr.clip()
        cr.set_source_surface(layers, 0, 0)
        cr.paint()
    else:
        painter.draw_ref(cr, value, 0, 0, size, size, size / 48)
    return out, painter, adaptive


# ---------------------------------------------------------------- adb

def adb_out(serial, command):
    from .android import adb_command

    Stats.adb_calls += 1
    result = subprocess.run(adb_command(serial, "exec-out", command), capture_output=True, timeout=60)
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace").strip() or "adb failed")
    return result.stdout


def phone_facts(serial, package):
    """One adb call: SDK, locale, smallest width, the package's APKs (with sizes) and its
    launcher activity as the phone resolves it."""
    script = (f"getprop ro.build.version.sdk; getprop persist.sys.locale; getprop ro.product.locale; "
              f"wm size | tail -n1; wm density | tail -n1; echo @@; "
              f"for f in $(pm path {package} | sed 's/^package://'); do echo \"$(stat -c %s \"$f\") $f\"; done; "
              f"echo @@; cmd package resolve-activity --brief -a android.intent.action.MAIN "
              f"-c android.intent.category.LAUNCHER {package} | tail -n1")
    text = adb_out(serial, script).decode(errors="replace")
    head, apks, activity = (text.split("@@\n") + ["", ""])[:3]
    lines = head.splitlines()
    sdk = int(lines[0]) if lines and lines[0].isdigit() else 37
    locale = (lines[1] or lines[2] or "en-US").replace("_", "-")
    lang, _, country = locale.partition("-")
    size = re.search(r"(\d+)x(\d+)", head)
    dens = re.search(r"(\d+)\s*$", lines[4] if len(lines) > 4 else "")
    sw_dp = 411
    if size and dens:
        sw_dp = round(min(int(size[1]), int(size[2])) * 160 / int(dens[1]))
    files = []
    for line in apks.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            files.append((parts[1], int(parts[0])))
    activity = activity.strip()
    return ({"sdk": sdk, "language": lang, "country": country.split("-")[0], "sw_dp": sw_dp},
            files, activity if "/" in activity else None)


def read_app(serial: str, package: str, size: int = 256, shape: str = "squircle") -> tuple[str, bytes | None] | None:
    """The app's label and icon (PNG bytes, or None when it has none), read from the phone; None
    when its APK couldn't be read. Never raises."""
    if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
        return None
    try:
        device, files, activity = phone_facts(serial, package)
        if not files:
            return None
        files.sort(key=lambda f: not f[0].endswith("/base.apk"))
        # The app's own APK (base.apk, or a system app's one APK of its own name), then the
        # splits that hold resources: density and language (not ABI or feature splits).
        wanted = re.compile(r"split_config\.([a-z]+dpi|" + re.escape(device["language"]) + r")\.apk$")
        zips = [Zip(AdbFile(serial, *files[0]))] + [Zip(AdbFile(serial, path, length))
                                                    for path, length in files[1:] if wanted.search(path)]

        def framework():
            facts = adb_out(serial, "stat -c %s /system/framework/framework-res.apk").strip()
            return Zip(AdbFile(serial, "/system/framework/framework-res.apk", int(facts)))

        return read(App(package, zips, device, framework), activity, size, shape)
    except Exception as error:  # noqa: BLE001 - an APK we can't make sense of is just an app without its icon
        warn(f"{package}: {error}")
        return None


def read_local(apks: list[str], package: str, activity: str | None = None, size: int = 256,
               shape: str = "squircle", language: str = "en") -> tuple[str, bytes | None]:
    """The same from APK files here (base first, then splits): for tests."""
    device = {"sdk": 37, "language": language, "country": "", "sw_dp": 411}
    zips = [Zip(LocalFile(p)) for p in apks]
    try:
        return read(App(package, zips, device), activity, size, shape)
    finally:
        for zip_ in zips:
            zip_.source.file.close()


def read(app: App, activity: str | None, size: int, shape: str) -> tuple[str, bytes | None]:
    Stats.warnings = []
    application, launchers = app.launcher_components()
    chosen = None
    if activity:
        want = activity.split("/", 1)[-1]
        if want.startswith("."):
            want = app.package + want
        chosen = next((el for el, name in launchers if name == want), None)
    if chosen is None and launchers:
        chosen = launchers[0][0]

    def attr(name):
        for el in (chosen, application):
            if el is not None and el.get(name) is not None and el.get(name).data != 0:
                return el.get(name)
        return None

    label = app.string(attr("label")) or app.package
    icon = attr("icon")
    if icon is None:
        return label, None
    surface, _painter, _adaptive = render_icon(app, icon, size, shape)
    out = io.BytesIO()
    surface.write_to_png(out)
    return label, out.getvalue()
