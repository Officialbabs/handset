"""Handset tests for saying it's on, and turning it off, as a desktop switch sees them."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from handset import __main__ as app

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OnFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.saved
        self.tmp.cleanup()

    def test_in_the_runtime_folder_itself(self):
        self.assertEqual(app.on_file(), os.path.join(self.tmp.name, "handset.on"))

    def test_on_and_off(self):
        app.say_on(True)
        self.assertTrue(os.path.exists(app.on_file()))
        app.say_on(False)
        self.assertFalse(os.path.exists(app.on_file()))
        app.say_on(False)  # already off: nothing to do, no error

    def test_off_with_nothing_running_clears_a_stale_word(self):
        # Handset stopped without saying so (it crashed): `handset --off` just clears the file.
        app.say_on(True)
        env = dict(os.environ, HANDSET_APP_ID="io.github.Officialbabs.HandsetTestOff",
                   XDG_CONFIG_HOME=self.tmp.name, ADA_PHONES_SOCKET=os.path.join(self.tmp.name, "none"))
        started = time.monotonic()
        subprocess.run([sys.executable, "-W", "ignore", os.path.join(HERE, "bin", "handset"), "--off"],
                       env=env, timeout=30, capture_output=True)
        self.assertFalse(os.path.exists(app.on_file()))
        self.assertLess(time.monotonic() - started, 20)  # and no window opened to wait on


if __name__ == "__main__":
    unittest.main()
