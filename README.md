# Handset

Your phone's screen in a window on the desk, live, shared with Ada. You use the phone from the
window with the mouse and keyboard while Ada keeps working on it with her own phone tool, and the
window shows what she's doing.

Handset finds your phones through Ada's phone service (Settings › Phones), so there's nothing to
pair: a phone Ada can use, Handset can show.

## Using it

The window is just the phone's screen, edge to edge, with a slim bezel above it: no title bar,
no buttons. Drag the bezel to move the window; double-click it to make the window fill the
screen.

- **The mouse is a finger.** Press, drag and let go as on the glass; hold for a long press. The
  wheel scrolls.
- **The other buttons:** right-click goes back; a middle-click goes home, and dragging with the
  middle button moves the window. Its edges resize it, and it keeps the phone's shape.
- **The keyboard types on the phone.** Enter, Backspace, Tab, the arrows, Home and End do what
  they do there, and Escape goes back. (Android types plain letters, digits and punctuation only.)
- **Shortcuts:** Ctrl+B back, Ctrl+H home, Ctrl+R recent apps, Ctrl+N notifications, F11 full
  screen, Ctrl+P the next phone, Ctrl+Shift+A the phone's apps, Ctrl+W close.
- **When Ada is working on the phone**, a small badge at the top says what she's doing and a ring
  goes round the screen. She waits while you use the phone; the window's touches are meant to
  count for that as your finger's do (not yet checked on a real phone).
- With more than one phone, Ctrl+P switches, or pick one on the page shown while a phone isn't
  connected. `handset --phone "Pixel 7"` opens that one.

## The phone's apps on the desk

While a phone is connected, its apps are apps on the desk: each one the phone's app drawer shows
is in search and among your apps, with its own icon and name ("WhatsApp (Pixel 7)"). Open one and
it comes up in a window of its own, under its own icon in the dock, not Handset's; pin it there
like any app. When the phone goes, its apps go with it, but for the ones pinned to the dock
(opening one waits for the phone). A small background process keeps the list (`handset --sync`,
started at sign-in and by Handset); it reads the icons once, keeps them in `~/.cache/handset/apps`,
and reads an app's again only when the app updates.

Right-click the bezel and choose **Phone Apps…** (or press Ctrl+Shift+A) for the phone's apps in
a list, to open one or pin it. An app's window has the phone's button on the left of its bezel
(the whole phone) and its close button on the right, and a thin frame round it.

- **On Android** the app gets a screen of its own on the phone, as big as the phone's, and only
  that app shows on it: the phone's own screen carries on as it was, and an app that was open
  there moves over. Closing the window closes the app. That screen sleeps when the phone does and
  shows nothing while the phone is locked: the window wakes the phone, and says when to unlock
  it. This needs the `scrcpy` package. Apps that hide their screen from sharing (banking, some
  video apps) show black, as they do when mirrored.
- **On an iPhone** the phone has one screen, so the app opens in front on it and the window shows
  the phone's screen with that app. A locked iPhone doesn't open apps: unlock it first.
- Icons come from the phone: on Android read out of each app's package (drawn as the launcher
  draws them, its first letter on a colour when one can't be read), on an iPhone from the phone.
- Each app is a menu entry in `~/.local/share/applications` (`io.github.Officialbabs.Handset.App.…`);
  a pin is a line in the dock's pins (`~/.config/os-ai/dock.ini`). `handset --phone ID --app APP`
  is what it runs; `handset --forget-apps` takes them all away (`./install.sh --uninstall` does).

## How it works

On an Android phone, Handset uses the same adb connection Ada set up, by cable or over Wi-Fi,
the cable when it's both:

- **The screen** comes as live H.264 video. With the `scrcpy` package installed, Handset uses its
  small server, which is the smoothest and follows the phone as it turns. Without it, Handset uses
  Android's own screen recorder. Frames go to GTK as they come from the decoder (YUV), and the
  GPU turns them into colour.
- **Touches** go through a virtual touchscreen that Handset adds to the phone (Android's own
  `uinput` tool, nothing installed on the phone). The phone takes them as a finger's. If a phone
  doesn't allow that, Handset falls back to sending each gesture whole.
- **Keys and text** go in as key presses.
- **What Ada is doing** comes from her phone service, which Handset only listens to.

On an iPhone (iOS 27, set up with Ada), a small helper in Rust (`ios/`, built by
`install.sh` where Rust is installed) does the talking, with the same toolkit Ada's phone service
uses (`idevice`):

- **It looks for the phone by its cable and on Wi-Fi at the same time**, and uses whichever
  answers first (the cable, when it's plugged in). By cable it goes through usbmuxd with the
  phone's own "Trust This Computer" pairing; over Wi-Fi, with the pairing Ada's desktop already
  made. Either way there's nothing to tap on the phone, and unplugging it carries on over Wi-Fi.
- **The screen** is Apple's own live screen-sharing stream (H.265, up to 60 frames a second, as
  Xcode shows it). The stream pads its frames out to whole blocks; Handset keeps the screen's
  own size, by the phone's model.
- **Touches, buttons and typing** go in through Apple's developer input services, which take
  them while the screen is being sent. An iPhone has no Back button: Back is a swipe in from the
  left edge, Recent apps a swipe up from the bottom held, the notifications a swipe down from
  the top. Typing is a US keyboard's letters, digits and punctuation.
- **Sharing the phone with Ada:** the phone has one screen stream for everyone, and stopping it
  stops them all, so Handset never stops it; it just leaves. If Ada's desktop stops it, Handset
  starts its own again.

## Installing

```sh
./install.sh             # the handset command, the menu entry and the icon
./install.sh --uninstall
```

Handset needs Python 3 with PyGObject, GTK 4, libadwaita and GStreamer (all part of the desktop),
plus adb for Android phones and Rust (`sudo apt install cargo`) to build the iPhone helper. For
the smoothest picture:

```sh
sudo apt install scrcpy gstreamer1.0-libav
```

With the graphics card's video driver installed (on Intel: `intel-media-va-driver`), the GPU
decodes both phones' video, and an Android phone's comes at its full size too; without it, the
CPU decodes, and an Android phone's video is sized to what the monitor can show.

**More than one phone:** each phone has a window of its own. Ctrl+Shift+N (or New Window on
Handset's icon) opens one for a phone no window shows yet; `handset --phone NAME` opens that
phone's. Handset opens the same windows next time, each at the size you gave it.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The tests use a stand-in Android phone (`tests/fakeadb.py`), a stand-in iPhone helper
(`tests/fakeios.py`) and a stand-in phone service (`tests/fakeservice.py`); they never touch a
real phone or the real service.
