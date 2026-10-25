#!/bin/sh
# Builds the phone-side helpers (HandsetMedia.java, HandsetClip.java) into data/handset-media.jar:
# Android bytecode that app_process runs on the phone. Only needed after changing it; the jar is kept in
# the repository. Needs a JDK and R8's D8, looked for in HANDSET_BUILD (by default
# ~/.cache/handset-build: a jdk-*/ folder and r8.jar).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOLS="${HANDSET_BUILD:-$HOME/.cache/handset-build}"
JDK="$(ls -d "$TOOLS"/jdk-*/ 2>/dev/null | head -1)"
[ -x "$JDK/bin/javac" ] || { echo "No JDK in $TOOLS (a jdk-*/ folder)." >&2; exit 1; }
[ -f "$TOOLS/r8.jar" ] || { echo "No r8.jar in $TOOLS." >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
"$JDK/bin/javac" --release 11 -d "$WORK/classes" "$HERE/HandsetMedia.java" "$HERE/HandsetClip.java"
"$JDK/bin/java" -cp "$TOOLS/r8.jar" com.android.tools.r8.D8 --release --min-api 26 --lib "$JDK" \
    --output "$WORK/handset-media.jar" $(find "$WORK/classes" -name '*.class')
cp "$WORK/handset-media.jar" "$HERE/../data/handset-media.jar"
echo "Built data/handset-media.jar"
