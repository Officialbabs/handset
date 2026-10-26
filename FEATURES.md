# Handset: Feature Reference

Handset brings Android phones and iPhones onto the Linux desktop. It shows a phone's screen live in
a native GTK 4 and libadwaita window, turns mouse, touchpad and keyboard input into touches and
typing on the phone, and connects the phone to the rest of the desktop: its apps in the launcher
and dock, and its notifications, clipboard, files, sound and media.

Handset installs nothing on the phone. It works through the interfaces the phone's operating
system already provides: the Android Debug Bridge (adb) on Android, Apple's device services on
iOS, and standard Bluetooth profiles.

## Contents

1. [Feature summary](#1-feature-summary)
2. [Connection and device management](#2-connection-and-device-management)
3. [The phone window](#3-the-phone-window)
4. [Input and control](#4-input-and-control)
5. [Screenshots](#5-screenshots)
6. [Phone apps on the desktop](#6-phone-apps-on-the-desktop)
7. [Notifications](#7-notifications)
8. [Clipboard](#8-clipboard)
9. [Files](#9-files)
10. [Audio and calls](#10-audio-and-calls)
11. [Media integration](#11-media-integration)
12. [Ada integration](#12-ada-integration)
13. [Working alongside KDE Connect](#13-working-alongside-kde-connect)
14. [Settings, shortcuts and command line](#14-settings-shortcuts-and-command-line)
15. [System integration and data locations](#15-system-integration-and-data-locations)
16. [Privacy and network use](#16-privacy-and-network-use)
17. [Requirements](#17-requirements)
18. [Test platform](#18-test-platform)
19. [Known limitations](#19-known-limitations)
20. [Validation notes](#20-validation-notes)

---

## 1. Feature summary

| Capability | Android | iPhone |
|---|---|---|
| Live screen mirroring | H.264, through the scrcpy server or Android's screen recorder | H.265 from Apple's screen-sharing service, up to 60 fps |
| Touch, gesture and keyboard control | Virtual multi-touch touchscreen | Apple's developer input services |
| Screenshots at native resolution | Yes | Yes |
| Phone apps in the launcher and dock | Each app in its own window, on its own virtual display | The app is brought to the front on the phone's screen |
| Notifications on the desktop | Over adb | Over Bluetooth (Apple Notification Center Service) |
| Shared clipboard | Text and images to the phone; text from the phone | Text and images, both ways |
| Drag-and-drop file transfer | To the phone's Download folder | To Photos and to the Files app |
| Phone storage as a desktop folder | Shared storage | Media folders and apps that share their files |
| Phone files in desktop search | Yes | Yes |
| Phone audio on the computer | Media, video and games (Android 11+) | All audio, plus phone calls (Bluetooth A2DP/HFP) |
| Desktop media controls | Title, artist, cover, position, seeking | Title, artist and album, with the cover from Apple's music catalogue |
| Connection | USB and Wi-Fi, at the same time | USB and Wi-Fi, at the same time |

---

## 2. Connection and device management

### Discovery

- **Through Ada's phone service.** On Ada, Handset discovers phones through the system phone service
  (Settings › Phones). Any phone the system can use is available in Handset without further
  pairing.
- **Standalone discovery.** On other desktops, Handset discovers phones itself: Android devices
  through adb (USB and Wi-Fi) and iPhones through usbmuxd. Known phones are remembered and stay
  listed while they're out of reach.

### Guided setup

The **+** button in Phones and Settings opens a step-by-step setup for a new phone. Each step
explains what to do on the phone and moves on as soon as the phone is ready.

- **Android over USB:** enable Developer options and USB debugging, then approve the computer on the
  phone.
- **Android over Wi-Fi**, in either of two ways:
  - **Use Over Wi-Fi Too**, while the phone is connected by cable. This lasts until the phone
    restarts. It finds the phone's Wi-Fi on whichever interface it's on (a Pixel 7 on Android 17
    uses `wlan1`, not `wlan0`). On Ada it's offered for a phone the system's phone service has no
    Wi-Fi for.
  - **Connected when wanted.** Handset remembers where it found each phone on Wi-Fi. When adb has
    lost it, opening its window or one of its apps, or searching its files, connects it there at
    once, as the iPhone's helper does. The background sync also connects it as soon as the phone
    announces its adb on the network, and otherwise tries every five minutes.
  - **adb's server outlives Handset.** Handset starts it in a systemd scope of its own, so closing
    or restarting Handset never drops a phone's Wi-Fi link.
  - **Wireless debugging with a pairing code** (Android 11 and later). Pairing-ready phones are
    found on the local network through Avahi (mDNS), and Handset reconnects whenever the phone is
    back on the network.
- **iPhone over USB:**
  1. Trust the computer.
  2. Enable Developer Mode (the phone restarts).
  3. Install Apple's developer disk image, which contains the touch input service. Handset
     downloads it once (about 17 MB), verifies it, and has it signed for the specific device by
     Apple's signing server. It's put back automatically each time the phone restarts.
  4. Optionally, pair for Wi-Fi.

### Connection handling

- **Cable and Wi-Fi together.** Handset prefers the cable when one is connected, keeps Wi-Fi as a
  fallback, and carries on over Wi-Fi when the cable is unplugged.
- **Automatic reconnection.** A phone that sleeps or drops off the network reconnects when it
  becomes reachable again.
- **Light on the phone.** Handset waits for a phone to say what changed instead of asking it over
  and over, so a phone left alone can sleep:
  - a window follows the phone's own event log for its screen turning, sleeping and locking, and
    looks at the screen only then (and now and then besides)
  - the media helper is called back by the phone's media service, and reads a cover once a track
  - files are indexed when they're searched, apps listed again only when the phone's packages
    change
- **Per-phone Connected switch.** Turning it off suspends everything for that phone (window,
  notifications, clipboard and sound) until it's turned back on.

### Phones and Settings window

Opened by launching Handset, with **Ctrl+,**, from the bezel's menu, or with `handset --phones`.

- **A card for each known phone**, showing:
  - how it's connected (USB, Wi-Fi or Bluetooth)
  - its battery level
  - a **Connected** switch and a **Show** button
- **A page for each phone**, with:
  - the Connected and Over Wi-Fi switches
  - battery level and charging state
  - the Android or iOS version
  - when it was last connected
  - its Bluetooth link
  - Show, Apps, Play Sound Here and iPhone Bluetooth pairing
  - Forget
- **System readiness panel:** shows whether each component is available: adb, scrcpy's server,
  usbmuxd, the iPhone helper, hardware video decoding and Bluetooth.
- **Each phone as its own app:** every phone has an entry under its own name in search and the app
  menu, with its own dock icon that can be pinned.

---

## 3. The phone window

### Window

- **The screen, edge to edge**, under a slim bezel. There's no title bar or toolbar.
- **Moving:** drag the bezel, or drag with the middle mouse button.
- **Full screen:** double-click the bezel or press F11.
- **Resizing** from the window's edges keeps the phone's aspect ratio.
- **Bezel buttons:** a screenshot button, Minimize and Close, and on an Android phone's window,
  Phone Screen Off.
- **Remembered sizes:** each phone's window reopens at its last size. App windows share one size:
  the last size given to any of them, starting at 90% of the screen's height.
- **Click to wake:** clicking a sleeping phone's window wakes the phone.
- **Phone screen off (Android):** the bezel's brightness button, Phone Screen Off in the bezel
  menu, or **Ctrl+Shift+O** turns the phone's own display off while the window keeps showing and
  controlling it, through scrcpy's server (Android's display control).
  - **Sleeps as ever:** the phone keeps its own screen-off time. Using it from the window counts as
    using the phone; left alone, it goes to sleep and its screen stays dark.
  - **One switch per phone,** shown alike in all its windows, app windows included.
  - **Back on** when switched off, when the phone's last window closes, when the phone
    disconnects, and when Handset quits. scrcpy's server's own clean-up turns it back on even if
    Handset is stopped.
  - **The phone's own once it sleeps** (its power button, or its screen-off time): the switch goes
    off, and the screen stays dark until the phone wakes.
  - **Not remembered,** so a phone is never dark by surprise. The phone must be unlocked.
  - Verified on the test platform: display off, still awake and drawing, both put back after.

### Video pipeline

- **Android:** H.264 through the scrcpy server, which gives the smoothest picture and follows
  rotation. Handset sends the phone its own copy of the server, version 3.3.4, which `install.sh`
  fetches from the scrcpy project and checks against its published checksum. Without it, Android's
  built-in screen recorder is used instead.
- **iPhone:** H.265 from Apple's screen-sharing service, at up to 60 frames per second. The stream's
  block padding is cropped to the display's true size for each model.
- **Hardware decoding:** through VA-API, with zero-copy DMA-BUF frames passed straight to GTK and
  colour conversion on the GPU. Software decoding is used when no hardware decoder is available.
- **Image quality:** both phones stream at native resolution. With hardware decoding, frames are
  scaled with a high-quality filter to twice the window's pixel size, then halved by the GPU. This
  keeps text sharp even in small windows. With software decoding, Android video is sized to the
  monitor's resolution.

### iPhone screen sessions

- **One live window at a time.** An iPhone has a single screen stream. With several windows open
  for the same iPhone, one shows it live and the others keep their last frame, dimmed. Clicking a
  dimmed window, or bringing it forward, moves the live stream there.
- **Clean session management:**
  - Handset ends its screen-sharing sessions when its windows close, so the phone doesn't keep
    holding them open.
  - It clears out stale sessions from earlier runs when it starts.
  - It never interrupts a session another client (Ada) is using, and restarts its own if it's
    stopped from elsewhere.
  - A dark screen sends no frames; the stream resumes when the display lights up, without a
    second session being opened.

---

## 4. Input and control

### Pointer and touchpad

| Input | Action on the phone |
|---|---|
| Left button press, drag, release | Touch, drag, lift |
| Left button held | Long press |
| Scroll wheel | Scroll, as a finger drag: held, moved one step per notch, and lifted once the wheel stops |
| Touchpad pinch, or Ctrl + scroll wheel | Two-finger pinch to zoom |
| Right click | Back |
| Middle click | Home |

### Keyboard

- **Text entry** goes straight to the focused field on the phone.
- **Enter, Backspace, Tab, the arrow keys, Home and End** behave as they do on the phone. **Escape**
  goes back.
- **Editing shortcuts** work in any app on the phone:

  | Shortcut | Action |
  |---|---|
  | Ctrl+A | Select all |
  | Ctrl+C | Copy; the copied text also goes to the computer's clipboard |
  | Ctrl+X | Cut |
  | Ctrl+V | Paste; the computer's clipboard (text or an image) is sent to the phone first |
  | Ctrl+Z | Undo |
  | Ctrl+Y or Ctrl+Shift+Z | Redo |

  On an iPhone these become the matching Command shortcuts.

### How input reaches the phone

- **Android:** through a virtual multi-touch touchscreen created on the phone with Android's own
  `uinput` tool. The phone sees it as a real touchscreen, so touches register as a finger's. Where
  that isn't allowed, Handset falls back to sending each gesture whole. Keys and text are sent as
  key events.
- **iPhone:** through Apple's developer input services: multi-touch, hardware buttons and keyboard.
  An iPhone has no navigation buttons, so Handset turns these into the matching gestures:
  - **Back:** a swipe in from the left edge
  - **Recent apps:** a double press of Home (the app switcher)
  - **Notifications:** a swipe down from the top

---

## 5. Screenshots

- **Taking one:** the camera button on the bezel, **Ctrl+Shift+S**, or **Print**.
- **Source and size:** captured on the phone itself, at its native resolution.
- **Where it goes:** saved to Pictures › Screenshots and copied to the clipboard.
- **App windows** capture the app's own screen.

---

## 6. Phone apps on the desktop

### Launcher and dock integration

- **Every app in the phone's app drawer** appears in desktop search and the app grid, under its own
  icon, named "‹App› (‹Phone name›)".
- **Each app opens in its own window**, with its own dock icon, separate from Handset's.
- **Pinning:** apps can be pinned to the dock like any desktop app.
- **When the phone disconnects,** its apps leave the launcher. Pinned apps stay, and opening one
  waits for the phone to come back.
- **Phone Apps** (Ctrl+Shift+A, or from the bezel menu) lists the phone's apps for opening or
  pinning.
- **App window bezel:** a phone button that opens the full phone view, plus Minimize and Close,
  with a thin frame round the window.

### Android app windows

- **A virtual display per app.** Each app runs on its own virtual display on the phone, the same
  size as the phone's screen. The phone's own screen is left as it was, and an app already open on
  it moves to the new display.
- **Closing the window closes the app.**
- **When the phone is locked,** the app's display sleeps with it. The window wakes the phone and
  asks the user to unlock it.
- **Apps opened by other apps** get windows of their own:
  - A link that opens a video app, or an attachment that opens a file viewer, comes up in its own
    window, showing the exact content opened, not the app's start screen.
  - Content an app opens inside itself (browser tabs, the app chooser, permission prompts) stays
    in the original window.
  - Handset follows the phone's window-manager events over adb to do this. It can be switched off.

### iPhone app windows

- **The app comes to the front** on the phone's single screen, and the window shows the phone with
  that app open.
- **Closing the window** takes the iPhone back to its home screen, through whichever window has its
  screen. A dark iPhone is left dark: Home would light it.
- **Minimizing the window** (its bezel's button) takes the iPhone back to its home screen too, when
  the app was opened over the home screen (as the iPhone says, from its own word on its apps); one
  opened over another app leaves the iPhone as it is. Coming back to the window brings the app
  back.
- **A locked iPhone must be unlocked** before an app can be opened.

### Icons and synchronisation

- **Android icons** are read from each app's package over adb and drawn the way the phone's
  launcher draws them. When an icon can't be read, a tile with the app's initial is used instead.
- **iPhone icons** come from the device.
- **Caching:** icons are cached, and an app's icon is read again only when the app updates.
- **Relisting (Android):** every 30 minutes a quick list of the phone's packages and their
  versions; its apps are listed again only when that changed.
- **Background sync:** a lightweight service (`handset --sync`), started at sign-in, keeps the
  launcher entries in step with the phones.
- **Removal:** `handset --forget-apps` removes every app entry, and uninstalling does the same.

---

## 7. Notifications

### Behaviour

- **Native desktop notifications.** Phone notifications appear on the desktop under the app's name
  and icon.
- **Clicking a notification** opens that app in its own window.
- **Kept in step with the phone:** a notification cleared on the phone is removed from the desktop.
- **Only alerting notifications are shown.** Excluded:
  - ongoing notifications (media playback, downloads)
  - group summaries
  - silent notifications
  - notifications held back by Do Not Disturb

### Android

- Read from the phone's notification service over adb as they're posted. Nothing is installed on
  the phone.

### iPhone

- **Delivered over Bluetooth Low Energy** through Apple Notification Center Service, the protocol
  smartwatches use.
- **One-time pairing:** Pair an iPhone for Notifications (bezel menu) or `handset --pair-iphone`.
  Then, on the iPhone, choose the computer in Settings › Bluetooth and allow notifications.
- **Automatic reconnection:** the paired iPhone is trusted in BlueZ, so it reconnects by itself.
  The computer is discoverable only during pairing, and while the paired iPhone is out of range.
- **Incoming calls** show **Accept** and **Decline** actions.
- **Active only while the iPhone is also connected** by cable or Wi-Fi. The Bluetooth link is
  released one minute after that connection ends, and taken up again when it returns.

Notifications can be switched off in Phones and Settings.

---

## 8. Clipboard

- **Shared both ways** with every phone Handset shows.
- **Computer to phone:** copy on the computer, then click into the phone's window, and the content
  is on the phone.
- **Phone to computer:** the content arrives straight away if a Handset window is in front,
  otherwise as soon as one comes forward. The desktop only lets the window in front access the
  clipboard.
- **What can be shared:**

  | Direction | Android | iPhone |
  |---|---|---|
  | Computer to phone | Text and images | Text and images |
  | Phone to computer | Text | Text and images |

- **Images sent to the phone** go onto the phone's clipboard only, never into its gallery or files.
  On Android, a small helper run temporarily over adb gives the image to the app that pastes it.
- **Android's clipboard notice.** Android shows "pasted from your clipboard" whenever its clipboard
  is read. The notice can be turned off with "Show clipboard access" in the phone's privacy
  settings.

---

## 9. Files

### Drag and drop to the phone

- **Sending:** drop files on a phone's window. Existing files are never overwritten.
- **Android:** files go to the phone's Download folder, where apps can see them at once.
- **iPhone:**
  - **Photos and videos** go into the Photos library, in an album named Handset. This needs a
    one-time Shortcut on the phone (Handset Import), because only a Shortcut on the phone can add
    to the Photos library. Handset shows how to make it.
  - **Audio** goes to VLC, and into its library.
  - **Other files** go to the Files app, under On My iPhone, in LocalSend's folder (or Chrome's).
  - VLC, LocalSend and Chrome are ordinary App Store apps that share their files. Handset only
    uses them when they're installed.
  - **Afterwards,** Handset returns the iPhone to the app that was in front before the import.
- **Throughput,** measured on the test platform: 60 MB to an iPhone over USB in about five seconds.

### Phones as folders

- **A folder for each connected phone** appears in `~/Phones`. It works in any file manager and in
  every app's Open and Save dialogs, the same way a USB drive does.
- **What's included:**
  - **Android:** the phone's shared storage.
  - **iPhone:** its media folders, plus the apps that share their files (under On My iPhone).
- **How it works:** Handset's own FUSE file system, over the phone's existing link, so it works the
  same by cable or Wi-Fi. A file is fetched when it's opened and cached for a day; a file written
  to the folder is sent to the phone when it's closed.
- **Access:** **Open in Files** on the phone's page. The folder can be switched off with Phone Files
  in the File Manager.

### Phone files in desktop search

- **Searching:** files and folders on connected phones appear in the desktop's search results by
  name, each showing its phone and folder. Picking one opens it in its default app.
- **Indexing:** searches never wait for a phone, because results come from an index Handset keeps.
  The index is built when the phone connects (unless the one kept from before is under 30
  minutes old), and made again when a search finds it older than that: the phone is left alone
  meanwhile.
  - **Android:** one `find` over the phone's storage, which took under a second on the test
    platform.
  - **iPhone:** walked by the helper.
- **Left out:** hidden files and apps' private data.
- **Search interface:** the standard GNOME search provider interface.

### Phone Files browser

Opened with **Ctrl+Shift+F**, or from the bezel menu.

- **Browse** the phone's storage.
- **Save** files to the computer's Downloads folder.
- **Open** files with the computer's apps.
- **Send** files into the folder being viewed.
- **Search:** typing (or Ctrl+F) filters the current folder at once, then searches its subfolders.
- **Transport:** Android over adb; iPhone through Apple's file services (AFC and house arrest).

---

## 10. Audio and calls

**Play Sound Here** (bezel menu or the phone's page) plays the phone's audio on the computer instead
of the phone.

- **Remembered for each phone,** and restored when the phone's window opens again.
- **Retried automatically** if the audio can't be routed at the moment, for example when the phone
  is out of range.

| | Android (11 and later) | iPhone |
|---|---|---|
| Transport | scrcpy server over adb | Bluetooth, with the computer as headphones and hands-free (A2DP and HFP, through PipeWire) |
| Audio covered | Media, video and games; the phone itself goes quiet. Ringtones and notification sounds stay on the phone | All audio |
| Calls | Stay on the phone | Heard through the computer's speakers and answered with its microphone |

---

## 11. Media integration

### Phone media in the desktop's media controls

Each phone's now playing appears as one of the desktop's media players (MPRIS). On Ada that's the
activity pill; on other desktops, the shell's media controls.

- **Shown:** title, artist, album, cover art and playback position.
- **Controls:** play and pause, next and previous, seeking (Android), and opening the playing app's
  window.
- **Android:** a small helper runs temporarily on the phone as the adb shell user, with nothing
  installed. It provides the app's own cover, the track length and seeking. The phone's media
  service calls it back when anything changes, so it does nothing in between. Without it, Handset
  reads the phone's media sessions as text, with no cover and no seeking.
- **iPhone:** delivered over Bluetooth through Apple Media Service, the protocol car systems use.
  That protocol carries no artwork, so covers are looked up in Apple's public music catalogue by
  artist and album and cached locally.

### One thing playing at a time

Handset coordinates playback across the phones and the computer, the way headphones switch between
devices:

- A phone starting to play pauses the other phones and the computer's players (a browser video, a
  music app).
- A player on the computer starting pauses the phones.
- Whatever was paused resumes when the interruption ends.
- **Short sounds** on the computer (a message tone, a notification chime) only lower the phones'
  audio routed to the computer for a moment. They pause nothing. A phone starting to play never
  lowers its own sound.
- **A track shorter than 20 seconds is a sound, not music,** however long its player says it's
  playing: two notification chimes in a row in a web page (WhatsApp Web in Natter or a browser),
  or a page's player left saying it plays, never pause anything or hold up a resume.
- **The next track,** or a moment of buffering, counts as the same playback continuing. Nothing is
  lowered and nothing resumes in between.
- **Players that stand for a phone are left alone,** so a phone never pauses itself. These include
  BlueZ's Bluetooth players and KDE Connect's.

This can be switched off in Phones and Settings.

---

## 12. Ada integration

On Ada, Handset works with the system's phone features:

- **Discovery and pairing** come from Ada's phone service, so phones set up in Settings › Phones are
  ready to use.
- **Activity indicator:** while Ada is working on a phone, a badge in the window says what she's
  doing, and a ring goes round the screen.
- **Taking turns:** Ada pauses while the user is operating the phone.
- **Shared screen stream:** Handset and Ada share the phone's screen stream without interrupting
  each other.
- **Control Center:** Handset provides an on/off switch in Control Center.
- **Dock:** phone apps pinned to the dock behave like any other pinned app.

---

## 13. Working alongside KDE Connect

Handset and KDE Connect do different jobs and run side by side.

- **Handset** is for using the phone from the computer: its screen, apps, sound and files, with
  nothing installed on the phone.
- **KDE Connect** has an app on the phone, so the phone can act on the computer: as a touchpad, a
  presentation remote, or media and volume controls. It also runs commands from the phone, brings
  texts and contacts to the computer, and rings a lost phone.

Three features overlap. With both apps running, keep one of each:

| Overlap | Handset | KDE Connect |
|---|---|---|
| Notifications | Notifications switch in Phones and Settings | Notification sync in the phone app |
| Clipboard | Always on while the phone is connected | Clipboard plugin |
| Phone media in the desktop's media controls | Always on | Multimedia plugin |

---

## 14. Settings, shortcuts and command line

### Settings

| Setting | Where | Default |
|---|---|---|
| Notifications | Phones and Settings (`phone_notifications`) | On |
| One Thing Playing at a Time | Phones and Settings (`auto_pause`) | On |
| Opened Apps in Windows of Their Own | Phones and Settings (`pop_out_apps`) | On |
| Phone Files in the File Manager | Phones and Settings (`phone_files`) | On |
| Start at Sign-In | Phones and Settings | On |
| Connected | Each phone's page | On |
| Over Wi-Fi | Each phone's page | On |
| Play Sound Here | Each phone's page, or the bezel menu | Off |

Settings are stored in `~/.config/handset/settings.json`.

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+B | Back |
| Ctrl+H | Home |
| Ctrl+R | Recent apps |
| Ctrl+N | Notifications |
| F11 | Full screen |
| Ctrl+P | Next phone |
| Ctrl+Shift+A | Phone Apps |
| Ctrl+Shift+F | Phone Files |
| Ctrl+Shift+N | New window for another phone |
| Ctrl+Shift+S, Print | Screenshot |
| Ctrl+Shift+O | Phone screen off or on (Android) |
| Ctrl+, | Phones and Settings |
| Ctrl+W | Close the window |
| Ctrl+F (in Phone Files) | Search |

### Command line

| Command | Action |
|---|---|
| `handset` | Open Phones and Settings |
| `handset --phones` | Open Phones and Settings |
| `handset --phone NAME` | Open that phone's window |
| `handset --phone ID --app APP` | Open one of the phone's apps in its own window |
| `handset --new-window` | Open a window for a phone that has none |
| `handset --off` | Close every window and quit |
| `handset --sync` | Run the background service: phone apps, notifications, media and file search |
| `handset --pair-iphone` | Make the computer discoverable to pair an iPhone for notifications and audio |
| `handset --forget-apps` | Remove every phone app from the launcher |

---

## 15. System integration and data locations

- **Its own systemd scope.** Handset runs in a scope of its own, so the desktop attributes its
  memory and network use to Handset, not to whichever app launched it.
- **Separate dock identities.** Each phone and each phone app has its own desktop entry and Wayland
  app ID, so they appear separately in the dock.

| Location | Contents |
|---|---|
| `~/.config/handset/settings.json` | Settings |
| `~/.config/handset/phones.json` | Known phones |
| `~/.config/handset/pairings` | iPhone Wi-Fi pairings |
| `~/.config/handset/notifications.json` | iPhones paired over Bluetooth |
| `~/.cache/handset/apps` | Phone app icons |
| `~/.cache/handset/covers` | Album covers for iPhone media |
| `~/.cache/handset/files` | Phone files opened from `~/Phones`, kept a day |
| `~/.cache/handset/search` | The index of phone file names for desktop search |
| `~/.cache/handset/ddi` | Apple's developer disk image |
| `~/.local/share/handset/scrcpy-server-v3.3.4` | scrcpy's server, sent to Android phones |
| `~/.local/share/handset/source` | Handset itself, when `install.sh` fetched it |
| `~/.local/lib/handset/handset-ios` | The iPhone helper |
| `~/.local/share/applications/io.github.Officialbabs.Handset.App.*` | Launcher entries for phone apps |
| `~/.local/share/gnome-shell/search-providers/io.github.Officialbabs.Handset.search-provider.ini` | Desktop search registration |
| `~/.config/autostart/io.github.Officialbabs.Handset.Sync.desktop` | The background sync, from sign-in |
| `~/Phones` | Phone folders (while connected) |

---

## 16. Privacy and network use

- **Nothing is installed on the phone.** On Android, the scrcpy server and Handset's media and
  clipboard helper run temporarily as the adb shell user while the phone is connected, and leave
  nothing behind.
- **Phone data stays on the computer.** Notifications, files, clipboard contents and the screen
  never pass through a cloud service.
- **Handset makes two outside requests:**
  - **Developer disk image (iPhone, once):** the image is downloaded from a public copy of Xcode's,
    and Apple's server signs it for the device.
  - **Album covers (iPhone):** artist and album names are sent to Apple's public music catalogue
    search, with no account.
- **Names only in search.** Desktop search indexes file and folder names, never file contents.
- **The installer's downloads,** each only when needed:
  - the distribution's packages, from its own repositories (and RPM Fusion's or Packman's video
    codecs on Fedora or openSUSE, only if accepted)
  - scrcpy's server, from the scrcpy project's GitHub release, kept only if its checksum matches
    the published one
  - Handset itself, with git, when run outside a Handset folder
  - the ready-built iPhone helper (x86_64), from Handset's GitHub release, kept only if its
    checksum is the one pinned in `ios/prebuilt`
  - otherwise Rust, from the distribution or rustup, only if accepted, and the helper's Rust
    libraries from crates.io

---

## 17. Requirements

### Computer

`install.sh` installs all of these (see the README's Installing section).

| Component | Needed for |
|---|---|
| Python 3, PyGObject, pycairo, GTK 4.16+, libadwaita 1.6+, GStreamer | Handset itself |
| adb, with its USB rules | Android phones |
| scrcpy's server 3.3.4 (fetched by `install.sh`) | Smoothest Android video, app windows, Android audio |
| GStreamer libav plugins | Software video decoding |
| VA-API video driver (e.g. `intel-media-va-driver`) | Hardware decoding, full-resolution Android video |
| Rust 1.85 or newer, with a C compiler | Building the iPhone helper, where there's no ready-built one (other than x86_64, or a changed helper) |
| usbmuxd | iPhones over USB |
| BlueZ and PipeWire | iPhone notifications, media information and audio |
| fusermount3 (fuse3) | Phone folders in `~/Phones` |
| Avahi | Finding Android phones for Wi-Fi pairing |
| wl-clipboard, pactl | The clipboard, and lowering the phones' sound |

### Phone

- **Android:** Developer options with USB debugging. Android 11 or later for Wireless debugging
  pairing and for audio.
- **iPhone:** a trust pairing with the computer, Developer Mode, and Apple's developer disk image
  (installed by Handset). Bluetooth pairing for notifications and audio. A one-time Shortcut for
  importing into Photos.

---

## 18. Test platform

Handset is developed and validated on the following configuration:

| Component | Version |
|---|---|
| Operating system | Ada 26.04 (Ubuntu 26.04 base), Wayland session |
| Kernel | Linux 7.0 |
| GTK / libadwaita | 4.22.4 / 1.9.1 |
| GStreamer | 1.28.2 |
| Python | 3.14.4 |
| Graphics | Intel integrated graphics, iHD VA-API driver 26.1.2 |
| adb | 1.0.41 |
| scrcpy server | 3.3.4 |
| iPhone helper | Rust 1.93, idevice 0.1.68 |
| Android | Android 17 |
| iOS | iOS 27 |

**Automated tests:** 263 tests across 26 test files run against a simulated Android device, a
simulated iPhone helper and a simulated phone service, with no hardware required:

```sh
python3 -m unittest discover -s tests -t .
```

iPhone notifications over Bluetooth and Play Sound Here on iPhone have been verified end to end on
this configuration. Other desktops (GNOME, KDE Plasma), other Android and iOS versions, and other
graphics hardware haven't been tested yet.

---

## 19. Known limitations

- **Typing:** Android accepts plain letters, digits and punctuation. iPhone typing follows a US
  keyboard layout.
- **Phone screen off** is for Android only: an iPhone shares its screen only while it's lit.
- **Notification replies:** notifications can't be answered from the desktop, and dismissing one on
  the desktop doesn't dismiss it on the phone.
- **Protected content:** apps that block screen capture (banking, some video apps) show black, as
  they do with any screen mirroring.
- **iPhone apps** open only while the iPhone is unlocked.
- **Android app windows** go black while the phone is locked, until it's unlocked.
- **Clipboard:** images copied on Android don't reach the computer; only text does.
- **Android audio** needs Android 11 or later. Ringtones and notification sounds stay on the phone.
- **iPhone Photos import** needs the one-time Handset Import Shortcut.
- **Developer access:** Android needs USB or Wireless debugging and the iPhone needs Developer
  Mode. Some Android banking apps refuse to run while developer options are on.
- **Files sent to an iPhone** other than photos and videos need an app on it that shares its files:
  VLC for audio, and LocalSend or Chrome for everything else.

---

## 20. Validation notes

These features are implemented and covered by automated tests, but haven't yet been checked in a
live setup on the test platform:

- **Ada taking turns** with input from Handset's window. Ada pausing for the user's touches is
  designed to cover Handset's touches too.
- **Working alongside KDE Connect:** "One Thing Playing at a Time" leaving KDE Connect's players
  alone has been checked against a simulated player, not a running KDE Connect.
