#!/bin/sh
# Handset for one user, run from this checkout: the `handset` command, the menu entry and the
# icon, and the sync that keeps connected phones' apps among the desk's apps (from sign-in).
# `./install.sh --uninstall` removes them, and the phone apps with them.
set -e
APP_ID=io.github.Officialbabs.Handset
APP_NAME=Handset
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/handset"
APPS="$DATA/applications"
ENTRY="$APPS/$APP_ID.desktop"
ICONS="$DATA/icons/hicolor/scalable/apps"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.Sync.desktop"

# The running sync (its lock's holder), stopped: by the lock, never by a pattern.
stop_sync() {
    LOCK="${XDG_RUNTIME_DIR:-/tmp/handset-$(id -u)}/handset-sync.lock"
    if [ -e "$LOCK" ] && command -v fuser >/dev/null; then fuser -k -TERM "$LOCK" >/dev/null 2>&1 || true; fi
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

HELPER_DIR="$HOME/.local/lib/handset"

case "${1:-}" in
    "") ;;
    --uninstall)
        stop_sync
        if [ -x "$LAUNCHER" ]; then "$LAUNCHER" --forget-apps || true; fi
        rm -f "$LAUNCHER" "$ENTRY" "$AUTOSTART" "$ICONS/$APP_ID.svg" "$HELPER_DIR/handset-ios"
        rmdir "$HELPER_DIR" 2>/dev/null || true
        refresh_caches
        echo "$APP_NAME removed."
        exit 0 ;;
    *) echo "Usage: $0 [--uninstall]" >&2; exit 2 ;;
esac

# The Python to run Handset with: the system's own first, since that's the one distributions
# give PyGObject to, then the first python3 on PATH.
CHECK='import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")'
PYTHON=""
for candidate in /usr/bin/python3 "$(command -v python3 || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c "$CHECK" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "$APP_NAME needs Python 3 with PyGObject, GTK 4 and libadwaita (on Ubuntu: python3-gi gir1.2-adw-1)."
    exit 1
fi

echo "==> Installing the handset command (with $PYTHON)"
mkdir -p "$BIN"
# A copy rather than a link, so it can name its Python and this checkout.
sed -e "1s|.*|#!$(sed_escape "$PYTHON")|" \
    -e "s|^HANDSET_DIR = \"\".*|HANDSET_DIR = $(sed_escape "$(py_quote "$HERE")")|" \
    "$HERE/bin/handset" > "$LAUNCHER.new"
chmod 755 "$LAUNCHER.new"
mv -f "$LAUNCHER.new" "$LAUNCHER"

# The iPhone helper (ios/, in Rust), where Rust is installed; Android phones don't need it.
if command -v cargo >/dev/null 2>&1; then
    echo "==> Building the iPhone helper"
    if (cd "$HERE/ios" && cargo build --release --quiet); then
        mkdir -p "$HELPER_DIR"
        # Beside the old one and moved over it, so a running Handset keeps the copy it started.
        cp "$HERE/ios/target/release/handset-ios" "$HELPER_DIR/handset-ios.new"
        mv -f "$HELPER_DIR/handset-ios.new" "$HELPER_DIR/handset-ios"
    else
        echo "Note: the iPhone helper didn't build (see above); Android phones work without it."
    fi
else
    echo "Note: iPhones need Rust to build Handset's helper (on Ubuntu: sudo apt install cargo), then run this again."
fi

echo "==> Installing the menu entry and icon"
mkdir -p "$APPS" "$ICONS"
cp "$HERE/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$ICONS/"
# The icon by its full path, for shells that don't look icons up by name in ~/.local/share/icons.
sed -e "s|@EXEC@|$(sed_escape "$(exec_quote "$LAUNCHER")")|" -e "s|^Icon=.*|Icon=$(sed_escape "$ICONS/$APP_ID.svg")|" \
    "$HERE/handset.desktop.in" > "$ENTRY.new"
mv "$ENTRY.new" "$ENTRY"
refresh_caches

echo "==> Keeping connected phones' apps among your apps, from sign-in"
mkdir -p "$(dirname "$AUTOSTART")"
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
} > "$AUTOSTART.new"
mv "$AUTOSTART.new" "$AUTOSTART"
# Now too, with this copy: the one running goes first.
stop_sync
sleep 0.5
setsid -f "$LAUNCHER" --sync </dev/null >/dev/null 2>&1 || true

echo "Done. Open $APP_NAME from your apps, or run 'handset'."
