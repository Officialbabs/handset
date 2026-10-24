"""Phone apps pinned to the dock, and their icons read out of APKs: in folders of the test's own."""

import os
import tempfile
import unittest

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from handset import apkicon, apps  # noqa: E402
from handset.apps import PhoneApp  # noqa: E402
from tests import apkfixture  # noqa: E402

CHAT = PhoneApp("FAKE7", "Pixel 7", "android", "org.example.chat", "Fake Chat")


def pixbuf(data: bytes) -> GdkPixbuf.Pixbuf:
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    return loader.get_pixbuf()


class PinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")}
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp.name, "data")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.dirname(apps.dock_path()))
        with open(apps.dock_path(), "w") as file:
            file.write("# the dock's own\n[Dock]\nPinned=ada-files.desktop;zen.desktop;\nOther=kept\n")

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def entry(self) -> GLib.KeyFile:
        entry = GLib.KeyFile()
        entry.load_from_file(apps.entry_path("FAKE7", "org.example.chat"), GLib.KeyFileFlags.NONE)
        return entry

    def test_pinning_makes_an_entry_and_docks_it(self):
        self.assertTrue(apps.pin(CHAT, None))
        entry = self.entry()
        self.assertEqual(entry.get_string("Desktop Entry", "Name"), "Fake Chat (Pixel 7)")
        self.assertTrue(entry.get_string("Desktop Entry", "Exec").endswith("--phone FAKE7 --app org.example.chat"))
        icon = entry.get_string("Desktop Entry", "Icon")
        self.assertTrue(os.path.isabs(icon) and os.path.isfile(icon))  # the dock takes a file, not a theme name
        self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(icon).get_width(), 192)  # a letter, for want of its own
        self.assertEqual(apps.docked(), ["ada-files.desktop", "zen.desktop", f"{CHAT.id}.desktop"])
        with open(apps.dock_path()) as file:
            self.assertIn("Other=kept", file.read())
        self.assertEqual(apps.pinned(), [CHAT])
        self.assertTrue(apps.is_pinned("FAKE7", "org.example.chat"))

    def test_unpinning_keeps_it_among_the_apps(self):
        apps.pin(CHAT, None)
        apps.unpin("FAKE7", "org.example.chat")
        self.assertEqual(apps.docked(), ["ada-files.desktop", "zen.desktop"])
        self.assertEqual(apps.pinned(), [])
        self.assertEqual(apps.entries(), [CHAT])  # still one of the phone's apps
        apps.remove_entry("FAKE7", "org.example.chat")
        self.assertEqual(apps.entries(), [])
        self.assertEqual(apps.icon_files("FAKE7", "org.example.chat"), [])

    def test_a_new_icon_is_a_new_file(self):
        apps.write_entry(CHAT)
        letter = self.entry().get_string("Desktop Entry", "Icon")
        self.assertFalse(apps.write_entry(CHAT))  # as it was: nothing written
        with open(letter, "rb") as file:
            self.assertFalse(apps.write_entry(CHAT, file.read()))
        own = apps.letter_icon("Something else")
        self.assertTrue(apps.write_entry(CHAT, own))
        icon = self.entry().get_string("Desktop Entry", "Icon")
        self.assertNotEqual(icon, letter)  # the desk sees a new picture by its new name
        self.assertEqual(apps.icon_files("FAKE7", "org.example.chat"), [icon])
        apps.write_entry(CHAT)  # no icon given: the one it has stays
        self.assertEqual(self.entry().get_string("Desktop Entry", "Icon"), icon)

    def test_without_the_docks_settings_nothing_is_made_up(self):
        os.unlink(apps.dock_path())
        self.assertFalse(apps.pin(CHAT, None))
        self.assertFalse(os.path.exists(apps.dock_path()))  # the dock's own first pins stay
        self.assertTrue(apps.has_entry("FAKE7", "org.example.chat"))  # the entry is there to pin by hand

    def test_ids_are_app_ids(self):
        self.assertRegex(CHAT.id, r"^io\.github\.Officialbabs\.Handset\.App\.p[0-9a-f]{16}$")
        self.assertNotEqual(CHAT.id, apps.app_id("OTHER", "org.example.chat"))


class IconTest(unittest.TestCase):
    def test_an_adaptive_icon_and_its_label(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "fixture.apk")
            apkfixture.main(path)
            label, icon = apkicon.read_local([path], "org.example.fixture", size=128)
            self.assertEqual(label, "Fixture")
            self.assertEqual(apkicon.read_local([path], "org.example.fixture", language="de")[0], "Testanwendung")
        picture = pixbuf(icon)
        self.assertEqual((picture.get_width(), picture.get_height()), (128, 128))
        pixels, stride = picture.get_pixels(), picture.get_rowstride()
        alpha = lambda x, y: pixels[y * stride + x * 4 + 3]  # noqa: E731
        self.assertEqual(alpha(1, 1), 0)  # outside the mask
        self.assertEqual(alpha(64, 64), 255)

    def test_an_app_that_isnt_there(self):
        self.assertIsNone(apkicon.read_app("FAKE7", "no such package!"))


if __name__ == "__main__":
    unittest.main()
