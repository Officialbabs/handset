"""A stand-in for Ada's phone service in Handset's tests: it answers `{"op":"watch"}` on a socket
of its own with a state, and pushes more states when the test says. Never the real socket."""

from __future__ import annotations

import json
import os
import socket
import threading


def phone(**fields) -> dict:
    """A phone entry as the service lists it, offline and idle unless `fields` say otherwise."""
    entry = {"id": "1A2B3C4D5E6F7G", "name": "Pixel 7", "model": "Pixel 7", "platform": "android",
             "status": "offline", "usb": False, "wifi": False, "ada": True, "using": None,
             "busy": None, "problem": None, "readiness": None, "android": "17"}
    entry.update(fields)
    return entry


class FakeService:
    """Listens at `path` (a temporary folder's, never the real socket) until close()."""

    def __init__(self, path: str, phones: list[dict]):
        self.path = path
        self.phones = phones
        self.requests: list[dict] = []
        self.watchers: list[socket.socket] = []
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def state(self) -> dict:
        return {"fingerprint": "", "ignored": [], "phones": self.phones, "requests": [],
                "server": {"address": "127.0.0.1:5037", "error": None, "ok": True},
                "usbmuxd": {"error": None, "ok": True}}

    def push(self, phones: list[dict]):
        """A change: every watcher hears the new state."""
        self.phones = phones
        line = (json.dumps({"event": "state", "state": self.state()}) + "\n").encode()
        with self.lock:
            for conn in list(self.watchers):
                try:
                    conn.sendall(line)
                except OSError:
                    self.watchers.remove(conn)

    def drop_watchers(self):
        """The service going away for a moment."""
        with self.lock:
            for conn in self.watchers:
                conn.close()
            self.watchers.clear()

    def close(self):
        self.drop_watchers()
        self.server.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        line = conn.makefile("r").readline()
        try:
            request = json.loads(line)
        except ValueError:
            conn.close()
            return
        self.requests.append(request)
        if request.get("op") == "watch":
            conn.sendall((json.dumps({"ok": True, "state": self.state()}) + "\n").encode())
            with self.lock:
                self.watchers.append(conn)
        else:
            conn.sendall(b'{"ok":false,"error":"invalid","message":"Only Ada\'s tasks work on phones"}\n')
            conn.close()
