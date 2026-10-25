"""Apps an app opens, out of its window into their own: the phone's tasks and window manager
events as Handset reads them, and the watch on them through the stand-in adb (fakeadb.py)."""

import os
import tempfile
import time
import unittest

from gi.repository import GLib

from handset.android import adb_command
from handset.popout import RootTask, TaskWatch, parse_event, parse_tasks, to_part

HERE = os.path.dirname(os.path.abspath(__file__))

# `am stack list` on a Pixel 7 (Android 17), cut down: two apps' displays and the phone's own.
TASKS = """RootTask id=481 bounds=[0,0][1080,2400] displayId=123 userId=0
 configuration={1.0 ?mcc0mnc nrml long port night winConfig={ mWindowingMode=fullscreen mActivityType=standard mAlwaysOnTop=undefined mRotation=ROTATION_0} as.7 s.440}
  taskId=481: com.google.android.deskclock/com.android.deskclock.DeskClock bounds=[0,0][1080,2400] userId=0 visible=true topActivity=ComponentInfo{com.google.android.deskclock/com.android.deskclock.DeskClock}

RootTask id=480 bounds=[0,0][1080,2400] displayId=123 userId=0
 configuration={1.0 ?mcc0mnc nrml long port night winConfig={ mWindowingMode=fullscreen mActivityType=standard mAlwaysOnTop=undefined mRotation=ROTATION_0} as.7 s.440}
  taskId=480: com.google.android.calculator/com.android.calculator2.Calculator bounds=[0,0][1080,2400] userId=0 visible=false topActivity=ComponentInfo{com.google.android.calculator/com.android.calculator2.Calculator}

RootTask id=477 bounds=[0,0][1080,2400] displayId=120 userId=0
 configuration={1.0 ?mcc0mnc nrml long port night winConfig={ mWindowingMode=fullscreen mActivityType=standard mAlwaysOnTop=undefined mRotation=ROTATION_0} as.7 s.440}
  taskId=477: com.google.android.gm/com.google.android.gm.ConversationListActivityGmail bounds=[0,0][1080,2400] userId=0 visible=true topActivity=ComponentInfo{org.example.viewer/org.example.viewer.Show}

RootTask id=1 bounds=[0,0][1080,2400] displayId=0 userId=0
 configuration={1.0 ?mcc0mnc nrml long port night winConfig={ mWindowingMode=fullscreen mActivityType=home mAlwaysOnTop=undefined mRotation=ROTATION_0} as.7 s.440}
  taskId=2: com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity bounds=[0,0][1080,2400] userId=0 visible=true topActivity=ComponentInfo{com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity}
"""


def task(id, display, package):
    return (f"RootTask id={id} bounds=[0,0][1080,2400] displayId={display} userId=0\n"
            " configuration={winConfig={ mWindowingMode=fullscreen mActivityType=standard}}\n"
            f"  taskId={id}: {package}/.Main bounds=[0,0][1080,2400] userId=0 visible=true\n\n")


def spin(until, seconds=8.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    return until()


class ReadingTest(unittest.TestCase):
    def test_tasks(self):
        tasks = parse_tasks(TASKS)
        self.assertEqual([(t.id, t.display, t.package, t.standard) for t in tasks], [
            (481, 123, "com.google.android.deskclock", True), (480, 123, "com.google.android.calculator", True),
            (477, 120, "com.google.android.gm", True), (1, 0, "com.google.android.apps.nexuslauncher", False)])

    def test_events(self):
        self.assertEqual(parse_event("I/wm_create_task: [0,479,479,121]"), ("wm_create_task", 121))
        self.assertEqual(parse_event("I/wm_task_moved: [479,479,122,1,0]\n"), ("wm_task_moved", 122))
        self.assertEqual(parse_event("I/wm_task_to_front: [0,477,120]"), ("wm_task_to_front", 120))
        self.assertEqual(parse_event("I/wm_task_removed: [478,478,119,removeChild, last child = "
                                     "ActivityRecord{55378541 u0 com.google.android.gm/.ComposeActivityGmail}]"),
                         ("wm_task_removed", 119))
        self.assertIsNone(parse_event("I/wm_create_activity: [0,216122340,479,com.example/.Main,NULL]"))
        self.assertIsNone(parse_event("--------- beginning of events"))

    def test_what_comes_out(self):
        tasks = parse_tasks(TASKS)
        # The clock came on top of the calculator's window: out it goes.
        self.assertEqual(to_part(tasks, 123, "com.google.android.calculator").id, 481)
        self.assertIsNone(to_part(tasks, 123, "com.google.android.deskclock"))  # its own window
        # Another app shown in Gmail's own task (a viewer it started) can't be parted from it.
        self.assertIsNone(to_part(tasks, 120, "com.google.android.gm"))
        self.assertIsNone(to_part(tasks, 0, "org.example.chat"))  # the home screen stays
        chooser = [RootTask(5, 9, "com.android.intentresolver", True)]
        self.assertIsNone(to_part(chooser, 9, "org.example.chat"))  # the phone asking which app: the app's
        self.assertIsNone(to_part([], 9, "org.example.chat"))


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.get(key) for key in ("HANDSET_ADB", "FAKEADB_DIR")}
        os.environ["HANDSET_ADB"] = os.path.join(HERE, "fakeadb.py")
        os.environ["FAKEADB_DIR"] = self.tmp.name
        self.parted, self.emptied = [], []
        self.watch = TaskWatch("FAKE7", lambda display, task: self.parted.append((display, task)),
                               self.emptied.append, adb_command)

    def tearDown(self):
        self.watch.stop()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write(self, name, text, mode="w"):
        with open(os.path.join(self.tmp.name, name), mode) as file:
            file.write(text)

    def test_another_app_comes_out_and_its_window_closes_with_it(self):
        self.write("tasks", task(10, 7, "org.example.chat"))
        self.watch.watch(7, "org.example.chat")
        spin(lambda: False, 0.5)  # the watch's start: nothing to do
        self.assertEqual(self.parted, [])
        # A link in the chat opened the video app, on the chat's display.
        self.write("tasks", task(11, 7, "org.example.video") + task(10, 7, "org.example.chat"))
        self.write("events", "I/wm_task_moved: [11,11,7,1,1]\nI/wm_create_task: [0,11,11,7]\n", "a")
        self.assertTrue(spin(lambda: self.parted))
        display, opened = self.parted[0]
        self.assertEqual((display, opened.id, opened.package), (7, 11, "org.example.video"))
        # Its window's display (8) takes it; the video app is shown, and later closes.
        self.watch.watch(8, "org.example.video")
        self.write("tasks", task(11, 8, "org.example.video") + task(10, 7, "org.example.chat"))
        self.write("events", "I/wm_task_to_front: [0,11,8]\n", "a")
        spin(lambda: False, 0.6)
        self.assertEqual(len(self.parted), 1)  # once
        # The chat opens the video again: the video app's task comes back on the chat's display,
        # and goes out again (to the window it has).
        self.write("tasks", task(11, 7, "org.example.video") + task(10, 7, "org.example.chat"))
        self.write("events", "I/wm_task_to_front: [0,11,7]\n", "a")
        self.assertTrue(spin(lambda: len(self.parted) == 2))
        self.assertEqual((self.parted[1][0], self.parted[1][1].id), (7, 11))
        self.write("tasks", task(11, 8, "org.example.video") + task(10, 7, "org.example.chat"))
        self.write("events", "I/wm_task_to_front: [0,11,8]\n", "a")
        spin(lambda: False, 0.6)
        self.write("tasks", task(10, 7, "org.example.chat"))
        self.write("events", "I/wm_task_removed: [11,11,8,removeChild]\n", "a")
        self.assertTrue(spin(lambda: self.emptied))
        self.assertEqual(self.emptied, [8])
        self.assertEqual(len(self.parted), 2)

    def test_other_displays_are_left_alone(self):
        self.write("tasks", task(10, 7, "org.example.chat") + task(12, 0, "org.example.video"))
        self.watch.watch(7, "org.example.chat")
        self.write("events", "I/wm_create_task: [0,12,12,0]\nI/wm_task_removed: [13,13,0,removeChild]\n", "a")
        spin(lambda: False, 1.2)
        self.assertEqual((self.parted, self.emptied), ([], []))


if __name__ == "__main__":
    unittest.main()
