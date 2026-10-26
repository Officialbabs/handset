/* Only for building: the phone's own android.os.Binder is the one used (build.sh). */
package android.os;

public class Binder {
    protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
        return false;
    }
}
