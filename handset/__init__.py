"""Handset: a phone's screen in a window on the desk, through Ada's phone service."""

import os

DEFAULT_APP_ID = "io.github.Officialbabs.Handset"
# HANDSET_APP_ID runs a separate instance for development.
APP_ID = os.environ.get("HANDSET_APP_ID") or DEFAULT_APP_ID
NAME = "Handset"
VERSION = "0.2.0"
