/*
 * What an Android phone plays, read on the phone: run there by app_process (as scrcpy's server
 * is), it asks the phone's media session service for its sessions and says, a line of JSON on its
 * output each time it changes, the one playing (or paused last): its app, title, artist, album,
 * length, where it is and how it plays, what it takes, and its cover (a JPEG in base64, once a
 * cover). Lines on its input work that session: {"op":"play"|"pause"|"next"|"previous"} and
 * {"op":"seek","position":ms}.
 *
 * It reaches Android through reflection only, so it builds with nothing but a JDK and d8
 * (android/build.sh); the shell user it runs as may read and work media sessions
 * (MEDIA_CONTENT_CONTROL).
 */
package io.github.officialbabs.handset;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Objects;

public final class HandsetMedia {
    private static final String CALLER = "com.android.shell";
    private static final int LOOK_MS = 500;
    private static final int COVER_EDGE = 600;
    private static final PrintStream OUT = System.out;

    private static Object sessions;  // ISessionManager
    private static Object current;  // ISessionController of the session shown
    private static String said = "";
    private static String coverSaid = "";
    private static boolean verbose;
    // Where the last line said it was, and when (the phone's clock): a position that keeps to the
    // pace isn't news (apps update theirs every moment), one that jumps is (a seek).
    private static long saidPosition;
    private static long saidAt;
    private static float saidSpeed;
    private static final long JUMP_MS = 1500;
    private static int user = 0;

    public static void main(String[] args) throws Exception {
        verbose = args.length > 0 && args[0].equals("--verbose");
        if (args.length > 2 && args[1].equals("--user")) {
            user = Integer.parseInt(args[2]);
        }
        if (args.length > 0 && args[0].equals("--methods")) {
            Object service = Class.forName("android.os.ServiceManager").getMethod("getService", String.class)
                    .invoke(null, "media_session");
            Object manager = Class.forName("android.media.session.ISessionManager$Stub")
                    .getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, service);
            for (Method method : manager.getClass().getMethods()) {
                if (method.getName().toLowerCase().contains("session")) {
                    System.err.println(method);
                }
            }
            return;
        }
        Object binder = Class.forName("android.os.ServiceManager").getMethod("getService", String.class)
                .invoke(null, "media_session");
        sessions = Class.forName("android.media.session.ISessionManager$Stub")
                .getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, binder);
        Thread commands = new Thread(HandsetMedia::readCommands);
        commands.setDaemon(true);
        commands.start();
        while (true) {
            try {
                look();
            } catch (Throwable error) {
                say("{\"event\":\"error\",\"message\":" + quote(String.valueOf(error)) + "}");
            }
            Thread.sleep(LOOK_MS);
        }
    }

    // ---- What plays ----------------------------------------------------------------------------

    private static void look() throws Exception {
        Object chosen = null;
        Object chosenState = null;
        for (Object controller : controllers()) {
            Object state = call(controller, "getPlaybackState");
            int number = state == null ? 0 : (Integer) call(state, "getState");
            if (number == 3 || number == 6) {  // playing, or buffering to
                chosen = controller;
                chosenState = state;
                break;
            }
            if (chosen == null && state != null && number == 2) {  // the first one paused
                chosen = controller;
                chosenState = state;
            }
        }
        current = chosen;
        if (chosen == null) {
            sayIfNew("{\"event\":\"none\"}");
            return;
        }
        Object metadata = call(chosen, "getMetadata");
        StringBuilder line = new StringBuilder("{\"event\":\"media\"");
        field(line, "package", call(chosen, "getPackageName"));
        if (metadata != null) {
            field(line, "title", text(metadata, "android.media.metadata.TITLE",
                    "android.media.metadata.DISPLAY_TITLE"));
            field(line, "artist", text(metadata, "android.media.metadata.ARTIST",
                    "android.media.metadata.ALBUM_ARTIST", "android.media.metadata.DISPLAY_SUBTITLE"));
            field(line, "album", text(metadata, "android.media.metadata.ALBUM"));
            line.append(",\"duration\":").append(number(metadata, "android.media.metadata.DURATION"));
        }
        line.append(",\"state\":").append(call(chosenState, "getState"));
        line.append(",\"speed\":").append(call(chosenState, "getPlaybackSpeed"));
        line.append(",\"actions\":").append(call(chosenState, "getActions"));
        String cover = metadata == null ? "" : cover(metadata);
        String coverId = cover.isEmpty() ? "" : Integer.toHexString(cover.hashCode());
        line.append(",\"cover_id\":").append(quote(coverId));
        String withoutCover = line.toString();
        // Where it is now: its position as of its last update, moved on since at its speed.
        long position = (Long) call(chosenState, "getPosition");
        long now = (Long) Class.forName("android.os.SystemClock").getMethod("elapsedRealtime").invoke(null);
        boolean moving = (Integer) call(chosenState, "getState") == 3;
        float speed = (Float) call(chosenState, "getPlaybackSpeed");
        if (moving) {
            position += (long) ((now - (Long) call(chosenState, "getLastPositionUpdateTime")) * speed);
        }
        long expected = saidPosition + (long) ((now - saidAt) * saidSpeed);
        boolean jumped = Math.abs(position - expected) > JUMP_MS;
        line.append(",\"position_now\":").append(position);
        if (!coverId.equals(coverSaid) && !cover.isEmpty()) {
            line.append(",\"cover\":").append(quote(cover));
        }
        line.append("}");
        if (!withoutCover.equals(said) || jumped) {
            said = withoutCover;
            coverSaid = coverId;
            saidPosition = position;
            saidAt = now;
            saidSpeed = moving ? speed : 0f;
            say(line.toString());
        }
    }

    /** The sessions' controllers, the most important first. */
    private static List<Object> controllers() throws Exception {
        Object found = null;
        for (Method method : sessions.getClass().getMethods()) {
            int count = method.getParameterCount();
            if (!method.getName().equals("getSessions") || count < 2 || count > 3) {
                continue;
            }
            // (ComponentName, user) up to Android 16; (ComponentName, user, calling package) on 17.
            Object who = method.getParameterTypes()[1] == int.class ? (Object) HandsetMedia.user
                    : Class.forName("android.os.UserHandle").getMethod("of", int.class).invoke(null, HandsetMedia.user);
            found = count == 2 ? method.invoke(sessions, null, who) : method.invoke(sessions, null, who, CALLER);
            break;
        }
        List<?> tokens = found instanceof List ? (List<?>) found
                : found == null ? new ArrayList<>() : (List<?>) call(found, "getList");  // a ParceledListSlice
        if (verbose) {
            System.err.println("sessions: " + (found == null ? "null" : found.getClass().getName()) + " " + tokens.size()
                    + (tokens.isEmpty() ? "" : " " + tokens.get(0).getClass().getName()));
        }
        List<Object> controllers = new ArrayList<>();
        if (tokens.isEmpty()) {
            // Android 17 lists the shell none: the session media keys go to (the one playing, or
            // played last) instead.
            Object token = call(sessions, "getMediaKeyEventSession", CALLER);
            List<Object> one = new ArrayList<>();
            if (token != null) {
                one.add(token);
            }
            tokens = one;
            if (verbose) {
                System.err.println("media key session: " + token);
            }
        }
        for (Object token : tokens) {
            Object controller = token;
            if (token.getClass().getName().endsWith("MediaSession$Token")) {
                controller = call(token, "getBinder");
            }
            if (controller != null) {
                controllers.add(controller);
            }
        }
        return controllers;
    }

    private static String text(Object metadata, String... keys) throws Exception {
        for (String key : keys) {
            Object value = metadata.getClass().getMethod("getString", String.class).invoke(metadata, key);
            if (value != null && !value.toString().isEmpty()) {
                return value.toString();
            }
        }
        return "";
    }

    private static long number(Object metadata, String key) throws Exception {
        return (Long) metadata.getClass().getMethod("getLong", String.class).invoke(metadata, key);
    }

    /** The session's cover as a JPEG in base64, its longest edge at most COVER_EDGE; or "". */
    private static String cover(Object metadata) throws Exception {
        Object bitmap = null;
        for (String key : new String[] {"android.media.metadata.ALBUM_ART", "android.media.metadata.ART",
                "android.media.metadata.DISPLAY_ICON"}) {
            bitmap = metadata.getClass().getMethod("getBitmap", String.class).invoke(metadata, key);
            if (bitmap != null) {
                break;
            }
        }
        if (bitmap == null) {
            return "";
        }
        Class<?> bitmaps = Class.forName("android.graphics.Bitmap");
        int width = (Integer) call(bitmap, "getWidth");
        int height = (Integer) call(bitmap, "getHeight");
        int edge = Math.max(width, height);
        if (edge > COVER_EDGE) {
            bitmap = bitmaps.getMethod("createScaledBitmap", bitmaps, int.class, int.class, boolean.class)
                    .invoke(null, bitmap, width * COVER_EDGE / edge, height * COVER_EDGE / edge, true);
        }
        Class<?> formats = Class.forName("android.graphics.Bitmap$CompressFormat");
        Object jpeg = formats.getField("JPEG").get(null);
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        bitmaps.getMethod("compress", formats, int.class, java.io.OutputStream.class).invoke(bitmap, jpeg, 90, bytes);
        return Base64.getEncoder().encodeToString(bytes.toByteArray());
    }

    // ---- Working it ----------------------------------------------------------------------------

    private static void readCommands() {
        try (BufferedReader input = new BufferedReader(new InputStreamReader(System.in))) {
            for (String line; (line = input.readLine()) != null; ) {
                Object controller = current;
                if (controller == null) {
                    continue;
                }
                try {
                    String op = value(line, "op");
                    if (op.equals("seek")) {
                        call(controller, "seekTo", CALLER, Long.parseLong(value(line, "position")));
                    } else if (op.equals("play") || op.equals("pause") || op.equals("next") || op.equals("previous")) {
                        call(controller, op, CALLER);
                    }
                } catch (Throwable error) {
                    say("{\"event\":\"error\",\"message\":" + quote(String.valueOf(error)) + "}");
                }
            }
        } catch (Exception ended) {
            // Handset went: so does this
        }
        System.exit(0);
    }

    /** A plain field's value in a line of JSON (the few Handset sends: no nesting, no escapes). */
    private static String value(String line, String name) {
        int at = line.indexOf("\"" + name + "\"");
        if (at < 0) {
            return "";
        }
        int colon = line.indexOf(':', at);
        String rest = line.substring(colon + 1).trim();
        if (rest.startsWith("\"")) {
            return rest.substring(1, rest.indexOf('"', 1));
        }
        int end = 0;
        while (end < rest.length() && (Character.isDigit(rest.charAt(end)) || rest.charAt(end) == '-')) {
            end++;
        }
        return rest.substring(0, end);
    }

    // ---- Reflection and JSON -------------------------------------------------------------------

    private static Object call(Object target, String name, Object... args) throws Exception {
        for (Method method : target.getClass().getMethods()) {
            if (method.getName().equals(name) && method.getParameterCount() == args.length) {
                return method.invoke(target, args);
            }
        }
        throw new NoSuchMethodException(target.getClass().getName() + "." + name);
    }

    private static void field(StringBuilder line, String name, Object value) {
        line.append(",\"").append(name).append("\":").append(quote(Objects.toString(value, "")));
    }

    private static String quote(String text) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            if (c == '"' || c == '\\') {
                out.append('\\').append(c);
            } else if (c < 0x20) {
                out.append(String.format("\\u%04x", (int) c));
            } else {
                out.append(c);
            }
        }
        return out.append('"').toString();
    }

    private static void sayIfNew(String line) {
        if (!line.equals(said)) {
            said = line;
            coverSaid = "";
            say(line);
        }
    }

    private static void say(String line) {
        OUT.println(line);
        OUT.flush();
    }
}
