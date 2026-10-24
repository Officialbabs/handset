"""HANDSET_DEBUG=1: what Handset does with the phone, and what the phone's tools say, on stderr."""

import os
import sys
import time

DEBUG = bool(os.environ.get("HANDSET_DEBUG"))


def debug(*parts):
    if DEBUG:
        print(time.strftime("%H:%M:%S"), "handset:", *parts, file=sys.stderr, flush=True)
