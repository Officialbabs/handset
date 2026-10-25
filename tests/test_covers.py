"""Covers for an iPhone's songs: which of the catalog's results, and where they're kept."""

import os
import tempfile
import unittest

from handset import covers


class CoversTest(unittest.TestCase):
    def test_the_same_artists_cover_at_full_size(self):
        results = [{"artistName": "Someone Else", "artworkUrl100": "https://x/a/100x100bb.jpg"},
                   {"artistName": "A Singer", "artworkUrl100": "https://x/b/100x100bb.jpg"}]
        self.assertEqual(covers.best(results, "a singer"), "https://x/b/600x600bb.jpg")
        self.assertIsNone(covers.best(results, "Nobody"))  # none rather than someone else's
        self.assertIsNone(covers.best([], "A Singer"))
        named = [{"artistName": "A Singer", "collectionName": "Other Album", "artworkUrl100": "https://x/c/100x100bb.jpg"},
                 {"artistName": "A Singer", "collectionName": "The Album (Deluxe)", "artworkUrl100": "https://x/d/100x100bb.jpg"}]
        self.assertEqual(covers.best(named, "A Singer", "the album"), "https://x/d/600x600bb.jpg")

    def test_one_per_album(self):
        saved = os.environ.get("XDG_CACHE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CACHE_HOME"] = tmp
            try:
                self.assertEqual(covers.kept("Song One", "A Singer", "An Album"),
                                 covers.kept("Song Two", "a singer", "an album"))
                self.assertNotEqual(covers.kept("Song One", "A Singer", ""), covers.kept("Song Two", "A Singer", ""))
                self.assertTrue(covers.kept("x", "y", "z").startswith(os.path.join(tmp, "handset", "covers")))
                self.assertIsNone(covers.cover("", "A Singer"))  # nothing to look up: not asked
            finally:
                if saved is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = saved


if __name__ == "__main__":
    unittest.main()
