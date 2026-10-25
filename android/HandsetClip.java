/*
 * Handset's clipboard helper on an Android phone, run by adb's shell user through app_process
 * (as scrcpy's server is), from the same jar as the media helper:
 *
 *     CLASSPATH=/data/local/tmp/handset-media.jar app_process / io.github.officialbabs.handset.HandsetClip URI MIME
 *
 * puts an image on the phone's clipboard: a clip of the URI (content://com.android.shell/bugreports/
 * NAME, a file in the Shell app's own files that its FileProvider serves), so any app that takes
 * images can paste it, and the app pasting it is let read it, by the clipboard itself. The image
 * never goes into the phone's gallery or its files. The phone's clipboard preview isn't shown for
 * it. Says "set" when it's done, or why not. Reflection only: built against the JDK alone.
 */
package io.github.officialbabs.handset;

import java.lang.reflect.Method;

public final class HandsetClip {
    private static final String CALLER = "com.android.shell";
    // Shell's own clips may hide SystemUI's clipboard preview (and its editor).
    private static final String NO_PREVIEW = "com.android.systemui.SUPPRESS_CLIPBOARD_OVERLAY";

    public static void main(String[] args) {
        try {
            if (args.length == 1 && args[0].equals("--methods")) {
                // What the phone's clipboard takes, nothing set (for looking at a new Android).
                for (Method method : clipboard().getClass().getMethods()) {
                    if (method.getName().equals("setPrimaryClip")) {
                        System.out.println(java.util.Arrays.toString(method.getParameterTypes()));
                    }
                }
                return;
            }
            if (args.length < 2) {
                throw new IllegalArgumentException("usage: HandsetClip URI MIME | --methods");
            }
            set(args[0], args[1]);
            System.out.println("set");
        } catch (Throwable error) {
            Throwable cause = error.getCause() != null ? error.getCause() : error;
            System.out.println("error " + cause);
            System.exit(1);
        }
    }

    private static void set(String uriText, String mime) throws Exception {
        Class<?> uriClass = Class.forName("android.net.Uri");
        Object uri = uriClass.getMethod("parse", String.class).invoke(null, uriText);
        Class<?> descriptionClass = Class.forName("android.content.ClipDescription");
        Object description = descriptionClass.getConstructor(CharSequence.class, String[].class)
                .newInstance("Handset", new String[] {mime});
        try {
            Class<?> bundleClass = Class.forName("android.os.PersistableBundle");
            Object extras = bundleClass.getConstructor().newInstance();
            bundleClass.getMethod("putBoolean", String.class, boolean.class).invoke(extras, NO_PREVIEW, true);
            descriptionClass.getMethod("setExtras", bundleClass).invoke(description, extras);
        } catch (ReflectiveOperationException old) {
            // Before Android 12: no extras, the preview shows.
        }
        Class<?> itemClass = Class.forName("android.content.ClipData$Item");
        Object item = itemClass.getConstructor(uriClass).newInstance(uri);
        Class<?> clipClass = Class.forName("android.content.ClipData");
        Object clip = clipClass.getConstructor(descriptionClass, itemClass).newInstance(description, item);

        Object clipboard = clipboard();
        for (Method method : clipboard.getClass().getMethods()) {
            if (!method.getName().equals("setPrimaryClip")) {
                continue;
            }
            Class<?>[] kinds = method.getParameterTypes();
            // Android 14 and later: (clip, package, attribution tag, user, device); 12-13: (clip,
            // package, attribution tag, user); before: (clip, package, user).
            if (kinds.length == 5) {
                method.invoke(clipboard, clip, CALLER, null, 0, 0);
                return;
            }
            if (kinds.length == 4) {
                method.invoke(clipboard, clip, CALLER, null, 0);
                return;
            }
            if (kinds.length == 3) {
                method.invoke(clipboard, clip, CALLER, 0);
                return;
            }
        }
        throw new NoSuchMethodException("IClipboard.setPrimaryClip");
    }

    private static Object clipboard() throws Exception {
        Object binder = Class.forName("android.os.ServiceManager").getMethod("getService", String.class)
                .invoke(null, "clipboard");
        return Class.forName("android.content.IClipboard$Stub")
                .getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, binder);
    }
}
