#!/usr/bin/env python3
"""A tiny synthetic APK (binary manifest + resources.arsc + adaptive vector icon + WebP fallback)
to exercise apkicon.py while no phone is reachable. Usage: make_fixture.py OUT.apk"""
import io
import struct
import sys
import zipfile

from PIL import Image, ImageDraw

ANDROID = "http://schemas.android.com/apk/res/android"
ATTR = {"label": 0x01010001, "icon": 0x01010002, "name": 0x01010003, "drawable": 0x01010199,
        "roundIcon": 0x0101052c, "viewportWidth": 0x01010402, "viewportHeight": 0x01010403,
        "width": 0x01010159, "height": 0x01010155, "pathData": 0x01010405, "fillColor": 0x01010404,
        "rotation": 0x01010326, "pivotX": 0x010101b5, "pivotY": 0x010101b6, "inset": 0x010104b5,
        "type": 0x010101a1, "startX": 0x01010510, "startY": 0x01010511, "endX": 0x01010512,
        "endY": 0x01010513, "startColor": 0x0101019d, "endColor": 0x0101019e, "fillType": 0x0101051e}


def pool(strings, utf8=False):
    data, offsets = b"", []
    for s in strings:
        offsets.append(len(data))
        if utf8:
            b = s.encode()
            data += bytes([len(s), len(b)]) + b + b"\0"
        else:
            data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
    data += b"\0" * (-len(data) % 4)
    start = 28 + 4 * len(strings)
    body = struct.pack(f"<{len(offsets)}I", *offsets) + data
    return struct.pack("<HHIIIIII", 1, 28, 28 + len(body), len(strings), 0,
                       0x100 if utf8 else 0, start, 0) + body


def axml(tree):
    """tree: (tag, {attr: (type, data|str)}, [children]); attrs are android: ones."""
    names = []
    def walk(node):
        for k in node[1]:
            if k not in names:
                names.append(k)
        for c in node[2]:
            walk(c)
    walk(tree)
    strings = list(names) + [ANDROID, "android"]
    def idx(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)
    body = b""
    def element(node):
        nonlocal body
        tag, attrs, kids = node
        attr_bytes = b""
        for k, (t, d) in attrs.items():
            raw = 0xFFFFFFFF
            if t == 3:
                raw = d = idx(d)
            attr_bytes += struct.pack("<IIIHBBI", idx(ANDROID), names.index(k), raw, 8, 0, t, d)
        ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, idx(tag), 20, 20, len(attrs), 0, 0, 0)
        body += struct.pack("<HHIII", 0x0102, 16, 16 + 20 + len(attr_bytes), 1, 0xFFFFFFFF) + ext + attr_bytes
        for c in kids:
            element(c)
        body += struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF, 0xFFFFFFFF, idx(tag))
    ns = struct.pack("<HHIIIII", 0x0100, 16, 24, 1, 0xFFFFFFFF, idx("android"), idx(ANDROID))
    element(tree)
    nse = struct.pack("<HHIIIII", 0x0101, 16, 24, 1, 0xFFFFFFFF, idx("android"), idx(ANDROID))
    sp = pool(strings)
    resmap = struct.pack(f"<HHI{len(names)}I", 0x0180, 8, 8 + 4 * len(names), *[ATTR.get(n, 0x01010000) for n in names])
    content = sp + resmap + ns + body + nse
    return struct.pack("<HHI", 3, 8, 8 + len(content)) + content


def config(density=0, sdk=0, lang=b"\0\0"):
    c = bytearray(64)
    struct.pack_into("<I", c, 0, 64)
    c[8:10] = lang
    struct.pack_into("<H", c, 14, density)
    struct.pack_into("<H", c, 24, sdk)
    return bytes(c)


def arsc(types, values_pool):
    """types: [(name, [key names], [(config, {entry index: (type, data)})])]"""
    type_names = [t[0] for t in types]
    keys = sorted({k for t in types for k in t[1]})
    chunks = b""
    for tid, (name, entry_keys, configs) in enumerate(types, 1):
        n = len(entry_keys)
        chunks += struct.pack(f"<HHIBBHI{n}I", 0x202, 16, 16 + 4 * n, tid, 0, 0, n, *[0] * n)
        for cfg, entries in configs:
            offsets, data = [], b""
            for i in range(n):
                if i in entries:
                    offsets.append(len(data))
                    t, d = entries[i]
                    data += struct.pack("<HHIHBBI", 8, 0, keys.index(entry_keys[i]), 8, 0, t, d)
                else:
                    offsets.append(0xFFFFFFFF)
            header = 20 + len(cfg)
            start = header + 4 * n
            chunks += struct.pack("<HHIBBHII", 0x201, header, start + len(data), tid, 0, 0, n, start) + cfg
            chunks += struct.pack(f"<{n}I", *offsets) + data
    tp, kp = pool(type_names), pool(keys, utf8=True)
    name = "com.example.fixture".encode("utf-16-le").ljust(256, b"\0")
    pkg_body = tp + kp + chunks
    pkg = struct.pack("<HHII", 0x200, 288, 288 + len(pkg_body), 0x7F) + name + struct.pack(
        "<IIIII", 288, len(type_names), 288 + len(tp), len(keys), 0) + pkg_body
    vp = pool(values_pool, utf8=True)
    return struct.pack("<HHII", 2, 12, 12 + len(vp) + len(pkg), 1) + vp + pkg


def main(out):
    REF, STR, COLOR, FLOAT, DIM, FRACTION = 1, 3, 0x1C, 4, 5, 6
    f = lambda v: struct.unpack("<I", struct.pack("<f", v))[0]
    # resource ids: 0x7f01xxxx mipmap, 0x7f02xxxx color, 0x7f03xxxx string, 0x7f04 drawable
    values = ["res/mipmap-anydpi-v26/ic_launcher.xml", "res/mipmap-xxxhdpi/ic_launcher.webp",
              "res/drawable/fg.xml", "Fixture", "Testanwendung", "res/B2.xml"]
    table = arsc([
        ("mipmap", ["ic_launcher"], [
            (config(0xFFFE, 26), {0: (STR, 0)}),
            (config(640), {0: (STR, 1)}),
        ]),
        ("color", ["ic_launcher_background"], [(config(), {0: (COLOR, 0xFF3DDC84)})]),
        ("string", ["app_name"], [(config(), {0: (STR, 3)}), (config(lang=b"de"), {0: (STR, 4)})]),
        ("drawable", ["fg", "grad"], [(config(), {0: (STR, 2), 1: (STR, 5)})]),
    ], values)
    manifest = axml(("manifest", {}, [("application", {"label": (REF, 0x7F030000), "icon": (REF, 0x7F010000)}, [
        ("activity", {"name": (STR, ".Main")}, [("intent-filter", {}, [
            ("action", {"name": (STR, "android.intent.action.MAIN")}, []),
            ("category", {"name": (STR, "android.intent.category.LAUNCHER")}, [])])])])]))
    adaptive = axml(("adaptive-icon", {}, [("background", {"drawable": (REF, 0x7F020000)}, []),
                                            ("foreground", {"drawable": (REF, 0x7F040000)}, [])]))
    vector = axml(("vector", {"width": (DIM, (108 << 8) | 1), "height": (DIM, (108 << 8) | 1),
                              "viewportWidth": (FLOAT, f(108)), "viewportHeight": (FLOAT, f(108))}, [
        ("group", {"rotation": (FLOAT, f(45)), "pivotX": (FLOAT, f(54)), "pivotY": (FLOAT, f(54))}, [
            ("path", {"pathData": (STR, "M40,40h28v28h-28z"), "fillColor": (REF, 0x7F040001)}, [])]),
        ("path", {"pathData": (STR, "M54,30a24,24 0 1,1 -0.1,0zM54,44a10,10 0 1,0 0.1,0z"),
                  "fillColor": (COLOR, 0x80FFFFFF), "fillType": (0x10, 1)}, [])]))
    gradient = axml(("gradient", {"type": (0x10, 0), "startX": (FLOAT, f(40)), "startY": (FLOAT, f(40)),
                                  "endX": (FLOAT, f(68)), "endY": (FLOAT, f(68)),
                                  "startColor": (COLOR, 0xFF1A73E8), "endColor": (COLOR, 0xFFEA4335)}, []))
    raster = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
    ImageDraw.Draw(raster).ellipse((8, 8, 184, 184), fill=(61, 220, 132, 255))
    webp = io.BytesIO()
    raster.save(webp, "WEBP")
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("AndroidManifest.xml", manifest, zipfile.ZIP_DEFLATED)
        z.writestr("resources.arsc", table, zipfile.ZIP_STORED)
        z.writestr(values[0], adaptive, zipfile.ZIP_DEFLATED)
        z.writestr(values[1], webp.getvalue(), zipfile.ZIP_STORED)
        z.writestr(values[2], vector, zipfile.ZIP_DEFLATED)
        z.writestr(values[5], gradient, zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    main(sys.argv[1])
