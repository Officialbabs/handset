"""The phones' files as folders on this computer (~/Phones/Pixel 7, ~/Phones/My iPhone…), for
any file manager and any app's Open dialog. A file system of Handset's own in user space (FUSE),
spoken straight to the kernel through /dev/fuse, which fusermount3 mounts for this person (no
library, nothing installed): the kernel asks, a thread answers, over the phone's own link (an
Android phone's adb, an iPhone's Apple file services through Handset's helper), so it works by
cable and over Wi-Fi alike.

A file is fetched whole when it's opened and read from here (kept a while, so a second look is
quick); one written is written here and sent when it's closed. Folders are listed as they're
looked in, and kept a few seconds. The mount goes when the phone does, and with Handset (fusermount3's
auto_unmount: even if Handset stops without saying so).
"""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .log import debug

# The kernel's requests (include/uapi/linux/fuse.h).
LOOKUP, FORGET, GETATTR, SETATTR = 1, 2, 3, 4
MKDIR, UNLINK, RMDIR, RENAME = 9, 10, 11, 12
OPEN, READ, WRITE, STATFS, RELEASE = 14, 15, 16, 17, 18
FSYNC, FLUSH, INIT, OPENDIR, READDIR, RELEASEDIR = 20, 25, 26, 27, 28, 29
FSYNCDIR, ACCESS, CREATE, INTERRUPT, DESTROY = 30, 34, 35, 36, 38
BATCH_FORGET, RENAME2 = 42, 45
NO_REPLY = {FORGET, BATCH_FORGET, INTERRUPT}

IN_HEADER = struct.Struct("<IIQQIIIHH")
OUT_HEADER = struct.Struct("<IiQ")
ATTR = struct.Struct("<QQQQQQIIIIIIIIII")
ENTRY_OUT = struct.Struct("<QQQQII")
ATTR_OUT = struct.Struct("<QII")
OPEN_OUT = struct.Struct("<QII")
INIT_OUT = struct.Struct("<IIIIHHIIHHI28x")
READ_IN = struct.Struct("<QQII")
WRITE_IN = struct.Struct("<QQIIQII")
SETATTR_IN = struct.Struct("<IIQQQQQQIIIIIIII")
DIRENT = struct.Struct("<QQII")
STATFS_OUT = struct.Struct("<QQQQQIIII24x")

FATTR_SIZE = 1 << 3
FUSE_BIG_WRITES = 1 << 5
MAX_WRITE = 128 * 1024
VALID_S = 1  # how long the kernel may keep what it's told of a name or a file
LISTED_FOR_S = 4  # a folder's listing is used again this long
DT_DIR, DT_REG = 4, 8
KEPT_FOR_S = 24 * 3600  # a phone's file fetched here is kept this long, for a second look


@dataclass(frozen=True)
class Entry:
    name: str
    folder: bool
    size: int = 0
    modified: float = 0.0


class Phone:
    """One phone's files, as the file system uses them. Paths are the phone's, from its top
    ("/DCIM/100APPLE/IMG_0001.HEIC", "/Download/notes.txt"). Each call is slow (over the
    phone's link) and answers False or an error in words when it can't."""

    def listing(self, path: str) -> list[Entry] | str:
        raise NotImplementedError

    def fetch(self, path: str, local: str) -> bool:
        raise NotImplementedError

    def push(self, local: str, path: str) -> bool:
        raise NotImplementedError

    def remove(self, path: str, folder: bool) -> bool:
        raise NotImplementedError

    def make_folder(self, path: str) -> bool:
        raise NotImplementedError

    def rename(self, old: str, new: str) -> bool:
        raise NotImplementedError

    def stop(self):
        pass


class Android(Phone):
    """An Android phone's storage (/sdcard), over adb."""

    TOP = "/sdcard"

    def __init__(self, serial: str):
        self.serial = serial

    def _on_phone(self, path: str) -> str:
        return self.TOP + path if path != "/" else self.TOP

    def _shell(self, command: str) -> bool:
        from .android import shell

        try:
            shell(self.serial, command, timeout=60)
            return True
        except (OSError, subprocess.SubprocessError) as error:
            debug("the phone's files:", command, error)
            return False

    def listing(self, path: str) -> list[Entry] | str:
        from .android import list_folder

        listed = list_folder(self.serial, self._on_phone(path), hidden=True)
        if isinstance(listed, str):
            return listed
        return [Entry(f.name, f.folder, f.size, f.modified) for f in listed]

    def fetch(self, path: str, local: str) -> bool:
        from .android import adb_command

        try:
            done = subprocess.run(adb_command(self.serial, "pull", "-a", self._on_phone(path), local),
                                  capture_output=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0 and os.path.exists(local)

    def push(self, local: str, path: str) -> bool:
        import shlex

        from .android import adb_command

        target = self._on_phone(path)
        try:
            done = subprocess.run(adb_command(self.serial, "push", local, target), capture_output=True, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode:
            return False
        # The phone's media index learns it, so its apps (Photos, a music app) show it now.
        self._shell(f"content call --uri content://media --method scan_file --arg {shlex.quote(target)}")
        return True

    def remove(self, path: str, folder: bool) -> bool:
        import shlex

        return self._shell(("rmdir " if folder else "rm -f ") + shlex.quote(self._on_phone(path)))

    def make_folder(self, path: str) -> bool:
        import shlex

        return self._shell("mkdir " + shlex.quote(self._on_phone(path)))

    def rename(self, old: str, new: str) -> bool:
        import shlex

        return self._shell(f"mv {shlex.quote(self._on_phone(old))} {shlex.quote(self._on_phone(new))}")


class IPhone(Phone):
    """An iPhone's files through Handset's helper (iphone.FilesSession): its media folders
    (DCIM, recordings…) and, under "On My iPhone", the apps that share their files."""

    def __init__(self, link):
        from .iphone import FilesSession

        self.session = FilesSession(link)
        self.bundles: dict[str, str] = {}  # an app's name under On My iPhone → its bundle id

    def _real(self, path: str) -> str:
        """The helper's path for one shown here: an app under On My iPhone by its bundle id."""
        top = self.session.apps_root
        if not path.startswith(top + "/"):
            return path
        name, _, rest = path[len(top) + 1:].partition("/")
        if name not in self.bundles:
            self.listing(top)
        bundle = self.bundles.get(name, name)
        return f"{top}/{bundle}" + (f"/{rest}" if rest else "")

    def listing(self, path: str) -> list[Entry] | str:
        listed = self.session.list_folder(self._real(path), hidden=True)
        if isinstance(listed, str):
            return listed
        if path == self.session.apps_root:
            self.bundles = {f.name: f.path.rsplit("/", 1)[-1] for f in listed}
        return [Entry(f.name, f.folder, f.size, f.modified) for f in listed]

    def _done(self, command: dict) -> bool:
        answer = self.session._ask(command)
        return isinstance(answer, dict) and answer.get("event") != "error"

    def fetch(self, path: str, local: str) -> bool:
        return self._done({"op": "fetch", "path": self._real(path), "to": local}) and os.path.exists(local)

    def push(self, local: str, path: str) -> bool:
        return self._done({"op": "send", "from": local, "path": self._real(path)})

    def remove(self, path: str, folder: bool) -> bool:
        return self._done({"op": "remove", "path": self._real(path)})

    def make_folder(self, path: str) -> bool:
        return self._done({"op": "mkdir", "path": self._real(path)})

    def rename(self, old: str, new: str) -> bool:
        return self._done({"op": "rename", "path": self._real(old), "to": self._real(new)})

    def stop(self):
        self.session.stop()


@dataclass
class Handle:
    path: str
    local: str  # the file's copy here
    writing: bool
    dirty: bool = False


def joined(folder: str, name: str) -> str:
    return "/" + name if folder == "/" else f"{folder}/{name}"


def parent_of(path: str) -> str:
    folder = path.rsplit("/", 1)[0]
    return folder or "/"


class FileSystem:
    """The file system for one phone, at `mountpoint`. start() mounts it and answers the kernel
    in threads of its own; stop() unmounts it."""

    def __init__(self, phone: Phone, mountpoint: str, cache: str):
        self.phone = phone
        self.mountpoint = mountpoint
        self.cache = cache  # where opened files are kept here
        self.fd: int | None = None
        self.connection: str | None = None  # the kernel's number for this mount (fusectl)
        self.channel: socket.socket | None = None  # to fusermount3: closing it unmounts
        self.helper: subprocess.Popen | None = None
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="phonefs")
        self.lock = threading.Lock()
        self.paths: dict[int, str] = {1: "/"}
        self.nodes: dict[str, int] = {"/": 1}
        self.listings: dict[str, tuple[float, dict[str, Entry]]] = {}
        self.handles: dict[int, Handle | list] = {}
        self.next_handle = 1
        self.stopped = False
        self.uid, self.gid = os.getuid(), os.getgid()

    # ---- Mounting -------------------------------------------------------------------------------

    def start(self) -> str | None:
        """Mounted, and answering: None, or why not in words."""
        unmount(self.mountpoint)  # one left from before (Handset stopped): gone first
        os.makedirs(self.mountpoint, exist_ok=True)
        os.makedirs(self.cache, exist_ok=True)
        for name in os.listdir(self.cache):  # copies kept from before, older than a day: gone
            path = os.path.join(self.cache, name)
            try:
                if time.time() - os.path.getmtime(path) > KEPT_FOR_S:
                    os.unlink(path)
            except OSError:
                pass
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        env = dict(os.environ, _FUSE_COMMFD=str(theirs.fileno()))
        options = "rw,nosuid,nodev,auto_unmount,fsname=handset,subtype=handset"
        try:
            self.helper = subprocess.Popen(["fusermount3", "-o", options, "--", self.mountpoint], env=env,
                                           pass_fds=(theirs.fileno(),), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as error:
            ours.close()
            theirs.close()
            return f"fusermount3 isn't there ({error}): install fuse3"
        theirs.close()
        ours.settimeout(10)
        try:
            _data, ancillary, _flags, _address = ours.recvmsg(1, socket.CMSG_SPACE(4))
        except (OSError, socket.timeout):
            ancillary = []
        if not ancillary:
            said = self.helper.stderr.read().decode(errors="replace").strip() if self.helper.poll() is not None else ""
            ours.close()
            return f"the phone's folder couldn't be mounted ({said or 'no answer'})"
        self.fd = struct.unpack("i", ancillary[0][2][:4])[0]
        self.connection = connection_of(self.mountpoint)
        ours.settimeout(None)
        self.channel = ours  # auto_unmount: when it closes, fusermount3 unmounts
        threading.Thread(target=self._serve, daemon=True, name="phonefs").start()
        return None

    def stop(self):
        # Unmounted first, while it still answers: looking at its own folder now would ask it.
        if self.fd is not None:
            try:
                subprocess.run(["fusermount3", "-u", "-z", self.mountpoint], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass  # severing below takes the mount away the hard way
        self.stopped = True
        self._sever()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.phone.stop()
        if self.helper:
            try:
                self.helper.wait(timeout=3)  # fusermount3, seeing it gone
            except subprocess.TimeoutExpired:
                self.helper.kill()
        try:
            os.rmdir(self.mountpoint)
        except OSError:
            pass

    # ---- The kernel's requests ----------------------------------------------------------------

    def _sever(self):
        """The kernel connection, cut, which wakes everything waiting on this mount with an error
        instead of leaving it asleep for good, and closing the channel has fusermount3
        (auto_unmount) take the mountpoint away. Safe to call twice.

        Closing /dev/fuse alone doesn't cut it while _serve is inside a read of it: the kernel
        ends a connection at the last release, and a read in progress holds the file. Requests
        already handed to the pool then wait for good (seen 29 Oct: six abandoned iPhone mounts
        holding ten, the desk's shell among the waiters). fusectl's abort cuts it outright; it
        comes first, while the descriptor still holds the connection, so its number can't yet
        belong to another mount."""
        fd, self.fd = self.fd, None
        if fd is not None:
            abort_connection(self.connection)
            try:
                os.close(fd)
            except OSError:
                pass
        channel, self.channel = self.channel, None
        if channel:
            channel.close()

    def _serve(self):
        try:
            while not self.stopped and self.fd is not None:
                try:
                    request = os.read(self.fd, MAX_WRITE + 4096)
                except OSError as error:
                    if error.errno in (errno.EINTR, errno.EAGAIN, errno.ENOENT):
                        continue  # interrupted, or a request the kernel took back
                    break  # ENODEV: unmounted
                except TypeError:
                    break  # self.fd became None under us: stopping
                if len(request) < IN_HEADER.size:
                    continue
                length, opcode, unique, node, _uid, _gid, _pid, _ext, _pad = IN_HEADER.unpack_from(request)
                body = request[IN_HEADER.size:length]
                if opcode == INIT:
                    self._init(unique, body)
                elif opcode == DESTROY:
                    self._reply(unique, 0)
                    break
                elif opcode not in NO_REPLY:
                    try:
                        self.pool.submit(self._answer, opcode, unique, node, body)
                    except RuntimeError:
                        break  # the pool is shut: stop() raced this one request
        finally:
            # However this loop ends — unmounted, stopping, or a fault — the mount dies with
            # it. A served folder whose server is gone leaves every later look at it waiting
            # forever, in a sleep nothing can interrupt; a lid close then can't put the
            # computer to sleep (seen 26 Oct, phonefs's pool shut while a request was in
            # hand). Severing wakes the waiters and has the helper unmount.
            self._sever()
        debug("the phone's folder is unmounted:", self.mountpoint)

    def _reply(self, unique: int, error: int, data: bytes = b""):
        fd = self.fd
        if fd is None:
            return
        try:
            os.write(fd, OUT_HEADER.pack(OUT_HEADER.size + len(data), -error, unique) + data)
        except OSError:
            pass  # the request was taken back (interrupted), or it's unmounted

    def _init(self, unique: int, body: bytes):
        _major, _minor, readahead, _flags = struct.unpack_from("<IIII", body)
        self._reply(unique, 0, INIT_OUT.pack(7, 31, readahead, FUSE_BIG_WRITES, 16, 12, MAX_WRITE, 1, 32, 0, 0))

    def _answer(self, opcode: int, unique: int, node: int, body: bytes):
        try:
            handler = ANSWERS.get(opcode)
            if handler is None:
                self._reply(unique, errno.ENOSYS)
                return
            result = handler(self, node, body)
            if isinstance(result, int):
                self._reply(unique, result)
            else:
                self._reply(unique, 0, result)
        except Exception as error:  # noqa: BLE001 - whatever it was, the kernel gets an answer
            debug("the phone's folder:", opcode, error)
            self._reply(unique, errno.EIO)

    # ---- Names and what they are ----------------------------------------------------------------

    def _node(self, path: str) -> int:
        with self.lock:
            node = self.nodes.get(path)
            if node is None:
                node = len(self.paths) + 1
                while node in self.paths:
                    node += 1
                self.paths[node], self.nodes[path] = path, node
            return node

    def _path(self, node: int) -> str | None:
        return self.paths.get(node)

    def _listing(self, path: str, fresh: bool = False) -> dict[str, Entry] | int:
        kept = self.listings.get(path)
        if kept and not fresh and time.monotonic() - kept[0] < LISTED_FOR_S:
            return kept[1]
        listed = self.phone.listing(path)
        if isinstance(listed, str):
            debug("the phone's folder", path, ":", listed)
            return errno.ENOENT if "not" in listed.lower() or "no such" in listed.lower() else errno.EIO
        entries = {e.name: e for e in listed}
        self.listings[path] = (time.monotonic(), entries)
        return entries

    def _entry(self, path: str) -> Entry | int:
        if path == "/":
            return Entry("", True)
        listing = self._listing(parent_of(path))
        if isinstance(listing, int):
            return listing
        entry = listing.get(path.rsplit("/", 1)[1])
        return entry if entry else errno.ENOENT

    def _forget_listing(self, path: str):
        self.listings.pop(parent_of(path), None)
        self.listings.pop(path, None)

    def _attr(self, node: int, entry: Entry, size: int | None = None) -> bytes:
        size = entry.size if size is None else size
        mode = (stat.S_IFDIR | 0o755) if entry.folder else (stat.S_IFREG | 0o644)
        when = int(entry.modified or time.time())
        return ATTR.pack(node, size, (size + 511) // 512, when, when, when, 0, 0, 0, mode,
                         2 if entry.folder else 1, self.uid, self.gid, 0, 4096, 0)

    def _entry_out(self, path: str, entry: Entry) -> bytes:
        node = self._node(path)
        return ENTRY_OUT.pack(node, 0, VALID_S, VALID_S, 0, 0) + self._attr(node, entry)

    # ---- The answers ----------------------------------------------------------------------------

    def lookup(self, node: int, body: bytes):
        folder, name = self._path(node), body.split(b"\0", 1)[0].decode(errors="surrogateescape")
        if folder is None:
            return errno.ENOENT
        path = joined(folder, name)
        entry = self._entry(path)
        return entry if isinstance(entry, int) else self._entry_out(path, entry)

    def getattr(self, node: int, _body: bytes):
        path = self._path(node)
        if path is None:
            return errno.ENOENT
        entry = self._entry(path)
        if isinstance(entry, int):
            return entry
        size = self._open_size(path)
        return ATTR_OUT.pack(VALID_S, 0, 0) + self._attr(node, entry, size)

    def _open_size(self, path: str) -> int | None:
        """A file open for writing here: its size as it is here, not as the phone has it yet."""
        for handle in list(self.handles.values()):
            if isinstance(handle, Handle) and handle.path == path and handle.writing:
                try:
                    return os.path.getsize(handle.local)
                except OSError:
                    return None
        return None

    def setattr(self, node: int, body: bytes):
        valid, _pad, fh, size = SETATTR_IN.unpack_from(body)[:4]
        path = self._path(node)
        if path is None:
            return errno.ENOENT
        if valid & FATTR_SIZE:
            handle = self.handles.get(fh) if fh else None
            if isinstance(handle, Handle) and handle.writing:
                os.truncate(handle.local, size)
                handle.dirty = True
            else:  # truncated by name: fetched, cut, sent back
                local = self._copy(path, fetch=size > 0)
                if local is None:
                    return errno.EIO
                os.truncate(local, size)
                if not self.phone.push(local, path):
                    return errno.EIO
                self._forget_listing(path)
        return self.getattr(node, b"")

    def statfs(self, _node: int, _body: bytes):
        return STATFS_OUT.pack(1 << 26, 1 << 25, 1 << 25, 1 << 20, 1 << 19, 4096, 255, 4096, 0)

    def access(self, _node: int, _body: bytes):
        return 0

    def opendir(self, node: int, _body: bytes):
        path = self._path(node)
        if path is None:
            return errno.ENOENT
        listing = self._listing(path, fresh=True)
        if isinstance(listing, int):
            return listing
        entries = [(".", node, True), ("..", 1, True)]
        entries += [(name, self._node(joined(path, name)), e.folder) for name, e in sorted(listing.items())]
        return OPEN_OUT.pack(self._keep(entries), 0, 0)

    def readdir(self, _node: int, body: bytes):
        fh, offset, size, _flags = READ_IN.unpack_from(body)
        entries = self.handles.get(fh)
        if not isinstance(entries, list):
            return errno.EBADF
        out = b""
        for index in range(offset, len(entries)):
            name, ino, folder = entries[index]
            raw = name.encode(errors="surrogateescape")
            record = DIRENT.pack(ino, index + 1, len(raw), DT_DIR if folder else DT_REG) + raw
            record += b"\0" * (-len(record) % 8)
            if len(out) + len(record) > size:
                break
            out += record
        return out

    def releasedir(self, _node: int, body: bytes):
        self.handles.pop(struct.unpack_from("<Q", body)[0], None)
        return 0

    def open(self, node: int, body: bytes):
        flags = struct.unpack_from("<I", body)[0]
        path = self._path(node)
        if path is None:
            return errno.ENOENT
        writing = flags & os.O_ACCMODE != os.O_RDONLY
        local = self._copy(path, fetch=not (flags & os.O_TRUNC), private=writing)
        if local is None:
            return errno.EIO
        if flags & os.O_TRUNC:
            os.truncate(local, 0)
        return OPEN_OUT.pack(self._keep(Handle(path, local, writing, dirty=bool(flags & os.O_TRUNC))), 0, 0)

    def create(self, node: int, body: bytes):
        _flags, _mode, _umask, _open_flags = struct.unpack_from("<IIII", body)
        name = body[16:].split(b"\0", 1)[0].decode(errors="surrogateescape")
        folder = self._path(node)
        if folder is None:
            return errno.ENOENT
        path = joined(folder, name)
        local = self._copy(path, fetch=False, private=True)
        if local is None or not self.phone.push(local, path):  # there, empty, from now on
            return errno.EACCES
        self._forget_listing(path)
        self._remember(folder, Entry(name, False, 0, time.time()))
        handle = self._keep(Handle(path, local, True))
        return self._entry_out(path, Entry(name, False, 0, time.time())) + OPEN_OUT.pack(handle, 0, 0)

    def read(self, _node: int, body: bytes):
        fh, offset, size, _flags = READ_IN.unpack_from(body)
        handle = self.handles.get(fh)
        if not isinstance(handle, Handle):
            return errno.EBADF
        with open(handle.local, "rb") as file:
            file.seek(offset)
            return file.read(size)

    def write(self, _node: int, body: bytes):
        fh, offset, size, _flags, _owner, _more, _pad = WRITE_IN.unpack_from(body)
        handle = self.handles.get(fh)
        if not isinstance(handle, Handle) or not handle.writing:
            return errno.EBADF
        data = body[WRITE_IN.size:WRITE_IN.size + size]
        with open(handle.local, "r+b") as file:
            file.seek(offset)
            file.write(data)
        handle.dirty = True
        return struct.pack("<II", len(data), 0)

    def flush(self, _node: int, body: bytes):
        handle = self.handles.get(struct.unpack_from("<Q", body)[0])
        return self._send(handle)

    def fsync(self, _node: int, body: bytes):
        return self._send(self.handles.get(struct.unpack_from("<Q", body)[0]))

    def release(self, _node: int, body: bytes):
        handle = self.handles.pop(struct.unpack_from("<Q", body)[0], None)
        error = self._send(handle)
        if isinstance(handle, Handle) and handle.writing:
            try:
                os.unlink(handle.local)
            except OSError:
                pass
        return error

    def _send(self, handle) -> int:
        """A file written here, onto the phone (once, however often it's flushed)."""
        if not isinstance(handle, Handle) or not handle.dirty:
            return 0
        handle.dirty = False
        if not self.phone.push(handle.local, handle.path):
            return errno.EIO
        self._forget_listing(handle.path)
        return 0

    def mkdir(self, node: int, body: bytes):
        name = body[8:].split(b"\0", 1)[0].decode(errors="surrogateescape")
        folder = self._path(node)
        if folder is None:
            return errno.ENOENT
        path = joined(folder, name)
        if not self.phone.make_folder(path):
            return errno.EACCES
        self._forget_listing(path)
        return self._entry_out(path, Entry(name, True, 0, time.time()))

    def unlink(self, node: int, body: bytes):
        return self._remove(node, body, folder=False)

    def rmdir(self, node: int, body: bytes):
        return self._remove(node, body, folder=True)

    def _remove(self, node: int, body: bytes, folder: bool):
        parent = self._path(node)
        if parent is None:
            return errno.ENOENT
        path = joined(parent, body.split(b"\0", 1)[0].decode(errors="surrogateescape"))
        if not self.phone.remove(path, folder):
            return errno.ENOTEMPTY if folder else errno.EACCES
        self._forget_listing(path)
        return 0

    def rename(self, node: int, body: bytes):
        return self._rename(node, struct.unpack_from("<Q", body)[0], body[8:])

    def rename2(self, node: int, body: bytes):
        return self._rename(node, struct.unpack_from("<Q", body)[0], body[16:])

    def _rename(self, node: int, new_folder: int, names: bytes):
        old_name, new_name = (n.decode(errors="surrogateescape") for n in names.split(b"\0")[:2])
        old_parent, new_parent = self._path(node), self._path(new_folder)
        if old_parent is None or new_parent is None:
            return errno.ENOENT
        old, new = joined(old_parent, old_name), joined(new_parent, new_name)
        if not self.phone.rename(old, new):
            return errno.EACCES
        self._forget_listing(old)
        self._forget_listing(new)
        with self.lock:
            node_id = self.nodes.pop(old, None)
            if node_id is not None:
                self.nodes[new], self.paths[node_id] = node_id, new
        return 0

    # ---- Here ---------------------------------------------------------------------------------

    def _keep(self, handle) -> int:
        with self.lock:
            number = self.next_handle
            self.next_handle += 1
            self.handles[number] = handle
            return number

    def _remember(self, folder: str, entry: Entry):
        kept = self.listings.get(folder)
        if kept:
            kept[1][entry.name] = entry

    def _copy(self, path: str, fetch: bool, private: bool = False) -> str | None:
        """The file here: fetched from the phone (or an empty one). Read-only copies are kept and
        shared while the phone's file is as it was; one being written is its own."""
        entry = self._entry(path) if fetch else None
        if fetch and isinstance(entry, int):
            return None
        if private:
            handle, local = tempfile.mkstemp(dir=self.cache, prefix="writing-")
            os.close(handle)
        else:
            key = f"{path}\n{entry.size if entry else 0}\n{entry.modified if entry else 0}"
            local = os.path.join(self.cache, hashlib.sha1(key.encode()).hexdigest()[:20])
            if os.path.exists(local):
                return local
        if not fetch:
            open(local, "wb").close()
            return local
        partial = local + ".part" if not private else local
        if not self.phone.fetch(path, partial):
            return None
        if partial != local:
            os.replace(partial, local)
        return local


ANSWERS = {
    LOOKUP: FileSystem.lookup, GETATTR: FileSystem.getattr, SETATTR: FileSystem.setattr,
    MKDIR: FileSystem.mkdir, UNLINK: FileSystem.unlink, RMDIR: FileSystem.rmdir, RENAME: FileSystem.rename,
    RENAME2: FileSystem.rename2, OPEN: FileSystem.open, READ: FileSystem.read, WRITE: FileSystem.write,
    STATFS: FileSystem.statfs, RELEASE: FileSystem.release, FSYNC: FileSystem.fsync, FLUSH: FileSystem.flush,
    OPENDIR: FileSystem.opendir, READDIR: FileSystem.readdir, RELEASEDIR: FileSystem.releasedir,
    FSYNCDIR: lambda self, node, body: 0, ACCESS: FileSystem.access, CREATE: FileSystem.create,
}


def unmount(mountpoint: str):
    """A mount there taken away (one left by a Handset that stopped, whose folder can't even be
    looked at any more, say); nothing if none. A mount the kernel has cut off fails lstat
    (ENOTCONN), which lexists would read as nothing there — and it's the one most in need of
    going, or makedirs trips over it."""
    try:
        os.lstat(mountpoint)
    except FileNotFoundError:
        return
    except OSError:
        pass
    subprocess.run(["fusermount3", "-u", "-z", mountpoint], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=10)


def connection_of(mountpoint: str) -> str | None:
    """The kernel's number for the FUSE mount at `mountpoint` (its device's minor, as fusectl
    names it under /sys/fs/fuse/connections), from mountinfo; None if it isn't mounted."""
    escaped = mountpoint.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")
    found = None
    try:
        with open("/proc/self/mountinfo") as file:
            for line in file:
                fields = line.split(" ")
                if len(fields) > 4 and fields[4] == escaped:
                    found = fields[2].split(":")[1]  # the last mount there is the one on top
    except OSError:
        return None
    return found


def abort_connection(connection: str | None):
    """A FUSE connection cut through fusectl: everything waiting on it wakes with an error."""
    if not connection:
        return
    try:
        with open(f"/sys/fs/fuse/connections/{connection}/abort", "w") as file:
            file.write("1")
    except OSError:
        pass  # gone already, or fusectl isn't mounted: closing the descriptor is what's left


def phones_folder() -> str:
    """Where the phones' folders are: ~/Phones (HANDSET_PHONES_FOLDER for trying)."""
    return os.environ.get("HANDSET_PHONES_FOLDER") or os.path.join(os.path.expanduser("~"), "Phones")


def folder_name(name: str) -> str:
    return name.replace("/", "-").strip() or "Phone"


def cache_name(phone_id: str) -> str:
    return hashlib.sha1(phone_id.encode()).hexdigest()[:12]


def mounted_folder(name: str) -> str | None:
    """A phone's folder here, while it's there (mounted)."""
    path = os.path.join(phones_folder(), folder_name(name))
    try:
        with open("/proc/self/mounts") as file:
            mounts = {line.split()[1].encode().decode("unicode_escape") for line in file}
    except OSError:
        return None
    return path if path in mounts else None
