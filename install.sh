#!/bin/sh
# Handset on this computer, everything it needs at once: the distribution's packages it uses
# (asked for once, through sudo), scrcpy's server, Rust and Handset's iPhone helper, then Handset
# for this user: the `handset` command, the menu entry and the icon, the phones' files in the
# desktop's search, and the sync that keeps connected phones' apps among the desk's apps.
#
#   ./install.sh               everything, asking before it installs packages or Rust
#   ./install.sh --yes         without asking
#   ./install.sh --check       what's missing, changing nothing
#   ./install.sh --no-deps     Handset only: the packages are seen to already
#   ./install.sh --deps-only   the packages only
#   ./install.sh --uninstall   Handset gone again, with the phone apps (the packages stay)
#
# Run from anywhere but a Handset folder (piped from curl, say), it fetches Handset with git into
# ~/.local/share/handset/source first, and runs from there; run again, it updates it.
set -e
APP_ID=io.github.Officialbabs.Handset
APP_NAME=Handset
REPOSITORY="${HANDSET_REPOSITORY:-https://github.com/Officialbabs/handset.git}"  # another, for the tests
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/handset"
APPS="$DATA/applications"
ENTRY="$APPS/$APP_ID.desktop"
ICONS="$DATA/icons/hicolor/scalable/apps"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.Sync.desktop"
SEARCH="$DATA/gnome-shell/search-providers/$APP_ID.search-provider.ini"
HELPER_DIR="$HOME/.local/lib/handset"
SOURCE="$DATA/handset/source"

# scrcpy's server, the version Handset is made for, as the scrcpy project publishes it (its
# install_release.sh pins the same checksum). Apache License 2.0.
SCRCPY_VERSION=3.3.4
SCRCPY_URL="https://github.com/Genymobile/scrcpy/releases/download/v$SCRCPY_VERSION/scrcpy-server-v$SCRCPY_VERSION"
SCRCPY_SHA256=8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e
SCRCPY_SERVER="$DATA/handset/scrcpy-server-v$SCRCPY_VERSION"

# What Handset's libraries must be at least (libadwaita's button rows and spinners, GTK's colour
# states), and Rust for its helper (the 2024 edition).
GTK_LEAST=4.16
ADW_LEAST=1.6
RUST_LEAST=1.85

YES=""
CHECK=""
DEPS=1
HANDSET=1
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --check) CHECK=1 ;;
        --no-deps) DEPS="" ;;
        --deps-only) HANDSET="" ;;
        --uninstall) UNINSTALL=1 ;;
        *) echo "Usage: $0 [--yes] [--check] [--no-deps] [--deps-only] [--uninstall]" >&2; exit 2 ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# A yes or no from the person, at the terminal even when this script came down a pipe; yes
# without asking with --yes, and no when there's no one to ask.
ask() {
    [ -n "$YES" ] && return 0
    if ! { true </dev/tty; } 2>/dev/null; then
        return 1
    fi
    printf '%s [Y/n] ' "$1" >/dev/tty
    read -r answer </dev/tty || return 1
    case "$answer" in [Nn]*) return 1 ;; *) return 0 ;; esac
}

# A command as root: itself when this is root, else through sudo (or doas).
as_root() {
    if [ "$(id -u)" = 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v doas >/dev/null 2>&1; then
        doas "$@"
    else
        echo "Run this as root, or install sudo: $*" >&2
        return 1
    fi
}

# Whether version $1 is at least $2 (dotted numbers).
at_least() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]
}

# ---- Handset's own folder ------------------------------------------------------------------------

# Not run from a Handset folder (piped from curl, say): Handset fetched first (git, from the
# distribution's packages if need be), and this script run again from it.
in_checkout() { [ -f "$HERE/handset/__main__.py" ] && [ -f "$HERE/bin/handset" ]; }

# ---- The distribution's packages ---------------------------------------------------------------

OS_RELEASE="${HANDSET_OS_RELEASE:-/etc/os-release}"  # another, for the tests

# Which family of distributions this is: apt, dnf, pacman, zypper, or nothing known.
family() {
    ids=$(. "$OS_RELEASE" 2>/dev/null && echo "$ID $ID_LIKE")
    for id in $ids; do
        case "$id" in
            debian|ubuntu) echo apt; return ;;
            fedora) echo dnf; return ;;
            arch|archlinux) echo pacman; return ;;
            opensuse*|suse|sles) echo zypper; return ;;
        esac
    done
    echo unknown
}

distro_name() { (. "$OS_RELEASE" 2>/dev/null && echo "${PRETTY_NAME:-$ID}") || echo "this system"; }

# The packages Handset uses, by family. Python with PyGObject and pycairo; GTK 4 and libadwaita,
# GStreamer (video in, sound out: base, good, bad for VA-API, libav for decoding in software);
# adb (with its udev rules, for phones by cable), usbmuxd (iPhones by cable), Avahi (phones
# over Wi-Fi), fuse3 (the phones' folders), wl-clipboard and pactl (the clipboard, the sound),
# BlueZ (an iPhone's notifications and music), psmisc (fuser), git and curl; Rust and a C
# compiler, for the iPhone helper.
packages() {
    case "$1" in
        apt) echo "python3 python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1
              gir1.2-graphene-1.0 gir1.2-gdkpixbuf-2.0 gir1.2-rsvg-2.0 gir1.2-gstreamer-1.0
              gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
              gstreamer1.0-plugins-bad gstreamer1.0-libav adb android-udev-rules|android-sdk-platform-tools-common
              usbmuxd avahi-daemon fuse3 wl-clipboard pulseaudio-utils bluez psmisc git curl cargo rustc gcc" ;;
        dnf) echo "python3 python3-gobject python3-cairo gtk4 libadwaita graphene gdk-pixbuf2 librsvg2
              gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free
              gstreamer1-plugin-openh264 gstreamer1-plugin-libav android-tools usbmuxd avahi fuse3
              wl-clipboard pulseaudio-utils bluez psmisc git curl cargo rust gcc" ;;
        pacman) echo "python python-gobject python-cairo gtk4 libadwaita graphene gdk-pixbuf2 librsvg
              gstreamer gst-plugins-base-libs gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugin-va
              gst-libav android-tools android-udev usbmuxd avahi fuse3 wl-clipboard libpulse bluez
              psmisc git curl rust gcc" ;;
        zypper) echo "python3 python3-gobject python3-gobject-Gdk python3-gobject-cairo python3-pycairo
              typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-Graphene-1_0 typelib-1_0-GdkPixbuf-2_0
              typelib-1_0-Rsvg-2_0 typelib-1_0-Gst-1_0 typelib-1_0-GstVideo-1_0 typelib-1_0-GstAllocators-1_0
              gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugins-libav
              android-tools android-udev-rules usbmuxd avahi fuse3 wl-clipboard
              pipewire-pulseaudio|pulseaudio-daemon pulseaudio-utils bluez psmisc git curl cargo rust gcc" ;;
    esac
}

# The video driver for this computer's graphics, so phones' video is decoded there (VA-API); without
# it, it's decoded in software, which works too.
graphics_driver() {
    vendors=$(cat "${HANDSET_DRM:-/sys/class/drm}"/card*/device/vendor 2>/dev/null | sort -u)  # another, for the tests
    for vendor in $vendors; do
        case "$1:$vendor" in
            apt:0x8086) echo intel-media-va-driver ;;
            apt:0x1002) echo "mesa-va-drivers|mesa-libgallium" ;;  # in Mesa's own since Ubuntu 26.04
            pacman:0x8086) echo intel-media-driver ;;
            zypper:0x8086) echo intel-media-driver ;;
        esac
    done
}

# Whether package $2 is installed, by family $1.
installed() {
    case "$1" in
        apt) dpkg-query -W -f='${db:Status-Abbrev}' "$2" 2>/dev/null | grep -q '^ii' ;;
        dnf|zypper) rpm -q --whatprovides "$2" >/dev/null 2>&1 ;;
        pacman) [ -z "$(pacman -T "$2" 2>/dev/null)" ] ;;
    esac
}

# Whether the distribution has package $2 to install, by family $1 (asked of apt only: the others'
# lists are the same on each release).
offered() {
    case "$1" in
        apt) apt-cache show "$2" >/dev/null 2>&1 ;;
        *) true ;;
    esac
}

# Of packages $2…, the ones not installed, by family $1. "a|b": either does (one release has one,
# another the other); if neither is here, the first the distribution has.
missing() {
    kind=$1
    shift
    for wanted in "$@"; do
        choices=$(echo "$wanted" | tr '|' ' ')
        there=""
        for package in $choices; do
            if installed "$kind" "$package"; then there=1; break; fi
        done
        [ -z "$there" ] || continue
        pick=""
        for package in $choices; do
            if offered "$kind" "$package"; then pick=$package; break; fi
        done
        echo "${pick:-${choices%% *}}"
    done
}

install_packages() {
    kind=$1
    shift
    case "$kind" in
        apt) as_root apt-get update -q && as_root apt-get install -y "$@" ;;
        dnf) as_root dnf install -y "$@" ;;
        pacman) as_root pacman -S --needed --noconfirm "$@" ;;
        zypper) as_root zypper --non-interactive install "$@" ;;
    esac
}

# Services some distributions leave off: Bluetooth (an iPhone's notifications) and Avahi (phones
# over Wi-Fi). usbmuxd starts by itself when an iPhone is plugged in.
services_off() {
    command -v systemctl >/dev/null 2>&1 || return 0
    for unit in bluetooth avahi-daemon; do
        if systemctl list-unit-files "$unit.service" 2>/dev/null | grep -q "^$unit.service" &&
                ! systemctl is-active --quiet "$unit"; then
            echo "$unit"
        fi
    done
}

deps() {
    kind=$(family)
    if [ "$kind" = unknown ]; then
        say "Packages: $(distro_name) isn't one this script knows"
        note "Install these yourself (names differ by distribution): Python 3 with PyGObject and pycairo,"
        note "GTK $GTK_LEAST+, libadwaita $ADW_LEAST+, GStreamer (base, good, bad, libav), adb, usbmuxd, Avahi,"
        note "fuse3, wl-clipboard, pactl, BlueZ, psmisc, git, curl, and Rust $RUST_LEAST+ with a C compiler."
        return 0
    fi
    # shellcheck disable=SC2046
    needed=$(missing "$kind" $(packages "$kind") $(graphics_driver "$kind") | tr '\n' ' ')
    off=$(services_off | tr '\n' ' ')
    if [ -z "${needed% }" ] && [ -z "${off% }" ]; then
        say "Packages: everything Handset needs is installed"
        return 0
    fi
    if [ -n "${needed% }" ]; then
        say "Packages: Handset needs these from $(distro_name):"
        note "$needed"
    fi
    if [ -n "${off% }" ]; then
        say "Services to switch on: $off"
    fi
    [ -n "$CHECK" ] && return 0
    if ! ask "Do that now (through sudo)?"; then
        note "Left as they are: Handset may not reach some phones until they're installed."
        return 0
    fi
    # shellcheck disable=SC2086
    if [ -n "${needed% }" ] && ! install_packages "$kind" $needed; then
        note "They didn't all install (see above)."
        if [ "$kind" = apt ]; then note "On Ubuntu some come from universe: sudo add-apt-repository universe, then run this again."; fi
        if [ "$kind" = pacman ]; then note "If pacman couldn't find some, bring the system up to date (sudo pacman -Syu) and run this again."; fi
        return 1
    fi
    # Those just installed too (Bluetooth on Arch comes switched off).
    for unit in $(services_off); do
        as_root systemctl enable --now "$unit" || note "$unit didn't start: Handset works without it, with less."
    done
}

# ---- What can't be a package: versions, scrcpy's server, Rust ---------------------------------

PYTHON=""
find_python() {
    # The system's own Python first, since that's the one distributions give PyGObject to.
    for candidate in /usr/bin/python3 "$(command -v python3 || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ] &&
                "$candidate" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' \
                >/dev/null 2>&1; then
            PYTHON="$candidate"
            return 0
        fi
    done
    return 1
}

libraries() {
    if ! find_python; then
        say "Libraries: no Python here has PyGObject with GTK 4 and libadwaita"
        return 1
    fi
    versions=$("$PYTHON" -c '
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
print(f"{Gtk.get_major_version()}.{Gtk.get_minor_version()} {Adw.get_major_version()}.{Adw.get_minor_version()}")')
    gtk=${versions% *}
    adw=${versions#* }
    if at_least "$gtk" "$GTK_LEAST" && at_least "$adw" "$ADW_LEAST"; then
        say "Libraries: GTK $gtk, libadwaita $adw"
        return 0
    fi
    say "Libraries: GTK $gtk and libadwaita $adw are too old"
    note "Handset needs GTK $GTK_LEAST and libadwaita $ADW_LEAST or newer: Ubuntu 24.10 or later, Debian 13,"
    note "Fedora 41 or later, Arch, openSUSE Tumbleweed or Leap 16."
    return 1
}

# Which decoder this computer has for each phone's video (decoder.py's DECODERS): "H.264 NAME" for
# Android phones, "HEVC NAME" for iPhones, NAME "-" for none.
video_decoders() {
    if [ -n "${HANDSET_DECODERS:-}" ]; then printf '%s\n' "$HANDSET_DECODERS"; return 0; fi  # the tests'
    "$PYTHON" -c '
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
for codec, names in (("H.264", ("vah264dec", "avdec_h264", "openh264dec")),
                     ("HEVC", ("vah265dec", "avdec_h265", "libde265dec"))):
    print(codec, next((n for n in names if Gst.ElementFactory.find(n)), "-"))' 2>/dev/null
}

# Fedora's own packages leave H.264 and HEVC out (its ffmpeg, its graphics drivers; its openh264
# can be a stand-in that decodes nothing): RPM Fusion's, as rpmfusion.org says to add them.
rpmfusion() {
    fedora=$(rpm -E %fedora)
    as_root dnf install -y \
        "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$fedora.noarch.rpm" \
        "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$fedora.noarch.rpm" || return 1
    as_root dnf swap -y ffmpeg-free ffmpeg --allowerasing || return 1
    as_root dnf install -y gstreamer1-plugin-libav
    for vendor in $(cat "${HANDSET_DRM:-/sys/class/drm}"/card*/device/vendor 2>/dev/null | sort -u); do
        case "$vendor" in
            0x8086) as_root dnf install -y intel-media-driver || true ;;
            0x1002) as_root dnf swap -y mesa-va-drivers mesa-va-drivers-freeworld || true ;;
        esac
    done
}

# Packman's folder for this openSUSE: Tumbleweed's, Slowroll's, or a Leap release's.
packman_tree() {
    (. "$OS_RELEASE" 2>/dev/null
     case "$ID" in
         opensuse-tumbleweed) echo openSUSE_Tumbleweed ;;
         opensuse-slowroll) echo openSUSE_Slowroll ;;
         *) echo "openSUSE_Leap_$VERSION_ID" ;;
     esac)
}

# openSUSE's own packages leave them out too: Packman's Essentials, as openSUSE's wiki says to add
# them, swapping in its codec libraries for the ones installed (so GStreamer's libav plugin finds
# the ones it was built for).
packman() {
    if ! zypper --non-interactive repos packman-essentials >/dev/null 2>&1; then
        as_root zypper --non-interactive addrepo -cfp 90 \
            "https://ftp.gwdg.de/pub/linux/misc/packman/suse/$(packman_tree)/Essentials/" packman-essentials || return 1
    fi
    as_root zypper --non-interactive --gpg-auto-import-keys refresh packman-essentials || return 1
    as_root zypper --non-interactive dist-upgrade --from packman-essentials --allow-vendor-change
}

# Whether this computer can decode each phone's video, and if not, the codecs from where the
# distribution says to get them (asked first: another repository).
decoders() {
    found=$(video_decoders) || return 0
    h264=$(echo "$found" | sed -n 's/^H.264 //p')
    hevc=$(echo "$found" | sed -n 's/^HEVC //p')
    # Fedora's and openSUSE's stand-in openh264 registers its decoder but decodes nothing.
    if [ "$h264" = openh264dec ] && command -v rpm >/dev/null 2>&1 &&
            { rpm -q noopenh264 >/dev/null 2>&1 || rpm -q --qf '%{VERSION}' libopenh264-8 2>/dev/null | grep -q noopenh264; }; then
        h264=-
    fi
    if [ "$h264" != - ] && [ "$hevc" != - ]; then
        say "Video: Android phones' with $h264, iPhones' with $hevc"
        return 0
    fi
    [ "$h264" != - ] || say "Video: nothing here decodes H.264, so Android phones' screens won't show"
    [ "$hevc" != - ] || say "Video: nothing here decodes HEVC, so iPhones' screens won't show"
    case "$(family)" in
        dnf) where="RPM Fusion (rpmfusion.org), as Fedora leaves them out"; add=rpmfusion ;;
        zypper) where="Packman (packman.links2linux.org), as openSUSE leaves them out"; add=packman ;;
        *) note "Install GStreamer's libav plugin (gst-libav), or your graphics card's VA-API driver."
           return 0 ;;
    esac
    [ -z "$CHECK" ] || { note "They'd come from $where."; return 0; }
    if ask "Add the video codecs from $where?"; then
        say "Adding the video codecs from $where"
        if $add; then
            decoders_again=$(video_decoders)
            say "Video now: $(echo "$decoders_again" | tr '\n' ' ')"
        else
            note "That didn't finish (see above); the phones' video waits for it."
        fi
    fi
}

sha256_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

scrcpy_server() {
    if [ "$(sha256_of "$SCRCPY_SERVER")" = "$SCRCPY_SHA256" ]; then
        say "scrcpy's server $SCRCPY_VERSION: here"
        return 0
    fi
    # A scrcpy package's own may be the very same file: no need to fetch it then.
    packaged=""
    for candidate in /usr/share/scrcpy/scrcpy-server /usr/local/share/scrcpy/scrcpy-server; do
        if [ "$(sha256_of "$candidate")" = "$SCRCPY_SHA256" ]; then packaged=$candidate; fi
    done
    if [ -n "$packaged" ]; then how="copied from $packaged"; else how="fetched from the scrcpy project"; fi
    if [ -n "$CHECK" ]; then
        say "scrcpy's server $SCRCPY_VERSION: to be $how"
        return 0
    fi
    mkdir -p "$(dirname "$SCRCPY_SERVER")"
    if [ -n "$packaged" ]; then
        cp "$packaged" "$SCRCPY_SERVER.part"
    else
        say "Fetching scrcpy's server $SCRCPY_VERSION (Android video, apps and sound; 90 kB)"
        if ! curl -fsSL -o "$SCRCPY_SERVER.part" "$SCRCPY_URL" 2>/dev/null &&
                ! wget -q -O "$SCRCPY_SERVER.part" "$SCRCPY_URL" 2>/dev/null; then
            rm -f "$SCRCPY_SERVER.part"
            note "It didn't come: Android phones show through their own screen recorder until this runs again."
            return 0
        fi
    fi
    if [ "$(sha256_of "$SCRCPY_SERVER.part")" != "$SCRCPY_SHA256" ]; then
        rm -f "$SCRCPY_SERVER.part"
        note "What came isn't what the scrcpy project published (its checksum differs): not kept."
        return 0
    fi
    mv -f "$SCRCPY_SERVER.part" "$SCRCPY_SERVER"
    say "scrcpy's server $SCRCPY_VERSION: $how, its checksum as published"
}

CARGO=""
find_cargo() {
    for candidate in "$(command -v cargo || true)" "$HOME/.cargo/bin/cargo"; do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        version=$("$(dirname "$candidate")/rustc" --version 2>/dev/null | cut -d' ' -f2)
        if [ -n "$version" ] && at_least "${version%%-*}" "$RUST_LEAST"; then
            CARGO="$candidate"
            return 0
        fi
    done
    return 1
}

# Handset's iPhone helper (ios/, in Rust), built here: with the distribution's Rust if it's new
# enough, else with rustup's (in ~/.cargo, asked first). Android phones don't need it.
iphone_helper() {
    if ! find_cargo; then
        if [ -n "$CHECK" ]; then
            say "Rust $RUST_LEAST or newer, for the iPhone helper: missing (rustup would add it, in ~/.cargo)"
            return 0
        fi
        say "The iPhone helper needs Rust $RUST_LEAST or newer, which isn't here"
        if command -v rustup >/dev/null 2>&1; then
            # rustup is here (the distribution's), with no toolchain yet, or an old one.
            if ask "Set up Rust's current version with rustup?"; then
                rustup toolchain install stable --profile minimal && rustup default stable
                find_cargo || true
            fi
        elif ask "Install Rust with rustup (in your home folder, ~/.cargo)?"; then
            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
            find_cargo || true
        fi
        if [ -z "$CARGO" ]; then
            note "Without it, iPhones aren't reached; Android phones work. Run this again to add it."
            return 0
        fi
    fi
    if [ -n "$CHECK" ]; then
        say "The iPhone helper: built with $("$CARGO" --version)"
        return 0
    fi
    say "Building the iPhone helper (a few minutes the first time)"
    if (cd "$HERE/ios" && "$CARGO" build --release --quiet); then
        mkdir -p "$HELPER_DIR"
        # Beside the old one and moved over it, so a running Handset keeps the copy it started.
        cp "$HERE/ios/target/release/handset-ios" "$HELPER_DIR/handset-ios.new"
        mv -f "$HELPER_DIR/handset-ios.new" "$HELPER_DIR/handset-ios"
    else
        note "It didn't build (see above); Android phones work without it."
    fi
}

# ---- Handset for this user -----------------------------------------------------------------------

# The running sync (its lock's holder), stopped: by the lock, never by a pattern.
stop_sync() {
    LOCK="${XDG_RUNTIME_DIR:-/tmp/handset-$(id -u)}/handset-sync.lock"
    if [ -e "$LOCK" ] && command -v fuser >/dev/null; then fuser -k -TERM "$LOCK" >/dev/null 2>&1 || true; fi
}

# The sync started as the desktop starts apps: in a unit of its own where there's systemd (so it
# takes nothing from this terminal, its limits included), else on its own.
start_sync() {
    if command -v systemd-run >/dev/null 2>&1 &&
            systemd-run --user --collect --quiet --unit="handset-sync-$(date +%s)" "$LAUNCHER" --sync 2>/dev/null; then
        return 0
    fi
    setsid -f "$LAUNCHER" --sync </dev/null >/dev/null 2>&1 || true
}

# $1 made safe as the replacement in a sed s|…|…| command.
sed_escape() { printf '%s\n' "$1" | sed -e 's/[\\&|]/\\&/g'; }

# $1 as one argument of a desktop entry's Exec=: quoted when it needs to be, then escaped
# again as a string value, as the Desktop Entry spec says.
exec_quote() {
    case "$1" in
        *[!A-Za-z0-9_./+-]*)
            printf '"%s"' "$(printf '%s\n' "$1" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g')" ;;
        *) printf '%s' "$1" ;;
    esac
}

# $1 as a Python string literal.
py_quote() { printf '"%s"' "$(printf '%s\n' "$1" | sed -e 's/[\\"]/\\&/g')"; }

refresh_caches() {
    if command -v update-desktop-database >/dev/null; then update-desktop-database -q "$APPS" || true; fi
    if command -v gtk-update-icon-cache >/dev/null; then gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true; fi
}

uninstall() {
    stop_sync
    if [ -x "$LAUNCHER" ]; then "$LAUNCHER" --forget-apps || true; fi
    rm -f "$LAUNCHER" "$ENTRY" "$AUTOSTART" "$SEARCH" "$ICONS/$APP_ID.svg" "$HELPER_DIR/handset-ios" "$SCRCPY_SERVER"
    rmdir "$HELPER_DIR" 2>/dev/null || true
    refresh_caches
    echo "$APP_NAME removed (the packages it used stay; $SOURCE too, if it fetched itself there)."
}

install_handset() {
    say "Installing the handset command (with $PYTHON)"
    mkdir -p "$BIN"
    # A copy rather than a link, so it can name its Python and this checkout.
    sed -e "1s|.*|#!$(sed_escape "$PYTHON")|" \
        -e "s|^HANDSET_DIR = \"\".*|HANDSET_DIR = $(sed_escape "$(py_quote "$HERE")")|" \
        "$HERE/bin/handset" > "$LAUNCHER.new"
    chmod 755 "$LAUNCHER.new"
    mv -f "$LAUNCHER.new" "$LAUNCHER"

    say "Installing the menu entry and icon"
    mkdir -p "$APPS" "$ICONS"
    cp "$HERE/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$ICONS/"
    # The icon by its full path, for shells that don't look icons up by name in ~/.local/share/icons.
    sed -e "s|@EXEC@|$(sed_escape "$(exec_quote "$LAUNCHER")")|" -e "s|^Icon=.*|Icon=$(sed_escape "$ICONS/$APP_ID.svg")|" \
        "$HERE/handset.desktop.in" > "$ENTRY.new"
    mv "$ENTRY.new" "$ENTRY"
    refresh_caches

    say "Putting the phones' files in the desktop's search"
    # Answered by the sync (phonesearch.py), while it runs: never started for a search.
    mkdir -p "$(dirname "$SEARCH")"
    {
        echo "[Shell Search Provider]"
        echo "DesktopId=$APP_ID.desktop"
        echo "BusName=$APP_ID.Sync"
        echo "ObjectPath=/io/github/Officialbabs/Handset/SearchProvider"
        echo "Version=2"
        echo "AutoStart=false"
    } > "$SEARCH.new"
    mv "$SEARCH.new" "$SEARCH"

    say "Keeping connected phones' apps among your apps, from sign-in"
    mkdir -p "$(dirname "$AUTOSTART")"
    # Switched off in Handset's window (Start at Sign-In): kept off.
    HIDDEN=""
    if [ -f "$AUTOSTART" ] && grep -q '^Hidden=true' "$AUTOSTART"; then HIDDEN=1; fi
    {
        echo "[Desktop Entry]"
        echo "Type=Application"
        echo "Name=$APP_NAME phone apps"
        echo "Comment=Your connected phones' apps among your apps"
        echo "Exec=$(exec_quote "$LAUNCHER") --sync"
        echo "Icon=$ICONS/$APP_ID.svg"
        echo "Terminal=false"
        echo "NoDisplay=true"
        echo "X-GNOME-Autostart-enabled=true"
        if [ -n "$HIDDEN" ]; then echo "Hidden=true"; fi
    } > "$AUTOSTART.new"
    mv "$AUTOSTART.new" "$AUTOSTART"
    # Now too, with this copy: the one running goes first.
    stop_sync
    sleep 0.5
    start_sync
}

# ---- In order ---------------------------------------------------------------------------------------

# Read by the tests for its functions alone (sourced, with HANDSET_LIB set): nothing done.
if [ -n "${HANDSET_LIB:-}" ]; then return 0; fi

if [ -n "$UNINSTALL" ]; then
    uninstall
    exit 0
fi

if ! in_checkout; then
    if ! command -v git >/dev/null 2>&1 && [ -n "$DEPS" ]; then
        deps
    fi
    if ! command -v git >/dev/null 2>&1; then
        echo "Handset is fetched with git: install git, then run this again." >&2
        exit 1
    fi
    if [ -d "$SOURCE/.git" ]; then
        say "Updating Handset in $SOURCE"
        git -C "$SOURCE" pull --ff-only --quiet
    else
        say "Fetching Handset into $SOURCE"
        mkdir -p "$(dirname "$SOURCE")"
        git clone --quiet --depth 1 "$REPOSITORY" "$SOURCE"
    fi
    exec sh "$SOURCE/install.sh" "$@"
fi

[ -z "$DEPS" ] || deps
[ -n "$HANDSET" ] || exit 0
libraries || { [ -n "$CHECK" ] && exit 0; exit 1; }
decoders
scrcpy_server
iphone_helper
[ -z "$CHECK" ] || exit 0
install_handset
echo "Done. Open $APP_NAME from your apps, or run 'handset'."
