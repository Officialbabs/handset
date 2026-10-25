"""Handset's own scope: which units are Handset's, and the name it gives its own."""

import unittest

from handset.scope import own_unit, scope_name


class ScopeTest(unittest.TestCase):
    def test_whose_unit(self):
        self.assertTrue(own_unit("app-ada-io.github.Officialbabs.Handset-223254.scope"))
        self.assertTrue(own_unit("app-gnome-io.github.Officialbabs.Handset-42.scope"))
        self.assertTrue(own_unit("app-io.github.Officialbabs.Handset@abc.service"))
        # A phone app's entry started it: not Handset's own.
        self.assertFalse(own_unit("app-ada-io.github.Officialbabs.Handset.App.pbd3cb83ce3c7e53d-10138.scope"))
        self.assertFalse(own_unit("app-ada-com.anthropic.Claude-9247.scope"))
        self.assertFalse(own_unit("handset-sync-090250.service"))

    def test_its_own_name(self):
        self.assertEqual(scope_name(7), "app-io.github.Officialbabs.Handset-7.scope")
        self.assertTrue(own_unit(scope_name(7)))


if __name__ == "__main__":
    unittest.main()
