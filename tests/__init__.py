"""Handset's tests: the phones' folders (phonefs.py) go in a folder of the tests' own, never in
~/Phones."""

import atexit
import os
import shutil
import tempfile

_phones = tempfile.mkdtemp(prefix="handset-phones-")
os.environ["HANDSET_PHONES_FOLDER"] = _phones
atexit.register(shutil.rmtree, _phones, True)
