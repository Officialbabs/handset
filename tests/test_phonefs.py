"""The phones' files as folders here (phonefs.py), mounted for real (FUSE, through fusermount3) on a
stand-in phone whose storage is a folder of the test's own: what a file manager does with them,
listing, reading, writing, saving through a hidden file, renaming, and the mount going."""

import os
import shutil
import tempfile
import time
import unittest

from handset.phonefs import Entry, FileSystem, Phone


class FolderPhone(Phone):
    """A phone whose storage is a folder here, slow calls counted."""

    def __init__(self, top: str):
        self.top = top
        self.calls: list[str] = []
        self.stopped = False

    def _here(self, path: str) -> str:
        return os.path.join(self.top, path.lstrip("/"))

    def listing(self, path):
        self.calls.append("list " + path)
        here = self._here(path)
        if not os.path.isdir(here):
            return "no such folder"
        found = []
        for name in os.listdir(here):
            info = os.stat(os.path.join(here, name))
            found.append(Entry(name, os.path.isdir(os.path.join(here, name)), info.st_size, info.st_mtime))
        return found

    def fetch(self, path, local):
        self.calls.append("fetch " + path)
        shutil.copyfile(self._here(path), local)
        return True

    def push(self, local, path):
        self.calls.append("push " + path)
        shutil.copyfile(local, self._here(path))
        return True

    def remove(self, path, folder):
        try:
            (os.rmdir if folder else os.unlink)(self._here(path))
            return True
        except OSError:
            return False

    def make_folder(self, path):
        os.mkdir(self._here(path))
        return True

    def rename(self, old, new):
        os.rename(self._here(old), self._here(new))
        return True

    def stop(self):
        self.stopped = True


@unittest.skipUnless(os.path.exists("/dev/fuse") and shutil.which("fusermount3"), "no FUSE here")
class MountedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = os.path.join(self.tmp.name, "phone")
        os.makedirs(os.path.join(self.storage, "DCIM"))
        with open(os.path.join(self.storage, "DCIM", "photo.jpg"), "wb") as file:
            file.write(b"\xff\xd8" + os.urandom(300_000))
        with open(os.path.join(self.storage, "notes.txt"), "w") as file:
            file.write("on the phone")
        self.phone = FolderPhone(self.storage)
        self.mounted = os.path.join(self.tmp.name, "Phones", "Test Phone")
        self.fs = FileSystem(self.phone, self.mounted, os.path.join(self.tmp.name, "cache"))
        problem = self.fs.start()
        self.assertIsNone(problem)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.fs.stop)

    def test_a_file_manager_at_work(self):
        self.assertEqual(sorted(os.listdir(self.mounted)), ["DCIM", "notes.txt"])
        self.assertTrue(os.path.isdir(os.path.join(self.mounted, "DCIM")))
        with open(os.path.join(self.mounted, "notes.txt")) as file:
            self.assertEqual(file.read(), "on the phone")
        with open(os.path.join(self.mounted, "DCIM", "photo.jpg"), "rb") as file, \
                open(os.path.join(self.storage, "DCIM", "photo.jpg"), "rb") as original:
            self.assertEqual(file.read(), original.read())  # whole, through the kernel's pieces

        # Written here, it's on the phone once it's closed: big, in many pieces.
        big = os.urandom(1_000_000)
        with open(os.path.join(self.mounted, "big.bin"), "wb") as file:
            file.write(big)
        with open(os.path.join(self.storage, "big.bin"), "rb") as file:
            self.assertEqual(file.read(), big)
        self.assertEqual(os.path.getsize(os.path.join(self.mounted, "big.bin")), len(big))

        # Saved as editors and file managers save: a hidden file, then renamed over the old one.
        hidden = os.path.join(self.mounted, ".notes.txt.saving")
        with open(hidden, "w") as file:
            file.write("edited here")
        os.replace(hidden, os.path.join(self.mounted, "notes.txt"))
        with open(os.path.join(self.storage, "notes.txt")) as file:
            self.assertEqual(file.read(), "edited here")

        # Added to, cut short, folders made and gone.
        with open(os.path.join(self.mounted, "notes.txt"), "a") as file:
            file.write(" and more")
        with open(os.path.join(self.storage, "notes.txt")) as file:
            self.assertEqual(file.read(), "edited here and more")
        os.truncate(os.path.join(self.mounted, "notes.txt"), 6)
        with open(os.path.join(self.storage, "notes.txt")) as file:
            self.assertEqual(file.read(), "edited")
        os.mkdir(os.path.join(self.mounted, "Made"))
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "Made")))
        os.rename(os.path.join(self.mounted, "big.bin"), os.path.join(self.mounted, "Made", "moved.bin"))
        self.assertTrue(os.path.exists(os.path.join(self.storage, "Made", "moved.bin")))
        os.unlink(os.path.join(self.mounted, "Made", "moved.bin"))
        os.rmdir(os.path.join(self.mounted, "Made"))
        self.assertFalse(os.path.exists(os.path.join(self.storage, "Made")))
        with self.assertRaises(FileNotFoundError):
            os.stat(os.path.join(self.mounted, "nothing here"))

    def test_read_twice_fetched_once(self):
        path = os.path.join(self.mounted, "DCIM", "photo.jpg")
        for _ in range(2):
            with open(path, "rb") as file:
                file.read()
        self.assertEqual(sum(1 for c in self.phone.calls if c.startswith("fetch")), 1)

    def test_it_goes_when_stopped(self):
        self.assertTrue(os.path.ismount(self.mounted))
        self.fs.stop()
        deadline = time.monotonic() + 5
        while os.path.ismount(self.mounted) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(os.path.exists(self.mounted))  # its folder gone too
        self.assertTrue(self.phone.stopped)


if __name__ == "__main__":
    unittest.main()
