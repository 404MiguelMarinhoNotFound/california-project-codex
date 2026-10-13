import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;

import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * California's phone-side call audio helper. Runs as the shell user:
 *
 *   CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio <mode> ...
 *
 * Modes:
 *   check                      is PSTN call audio interceptable on this phone?
 *   bridge <port> <rec> <play> one TCP client on 127.0.0.1:<port>; the call's
 *                              downlink (the other person) goes out as PCM16
 *                              mono at <rec> Hz, and PCM16 mono at <play> Hz
 *                              coming in is injected into the call's uplink.
 *
 * The shell holds CALL_AUDIO_INTERCEPTION, CAPTURE_AUDIO_OUTPUT and
 * RECORD_AUDIO on this phone; the APIs are @SystemApi, so everything below the
 * fake context is reflection. Nothing here is compiled against android.jar:
 * the android.* classes imported above are stubs that are not dexed.
 */
public final class CallAudio {

    /** scrcpy's trick: an identity for AudioRecord without an app. */
    static final class FakeContext extends ContextWrapper {
        FakeContext() { super(null); }
        @Override public String getPackageName() { return "com.android.shell"; }
        @Override public String getOpPackageName() { return "com.android.shell"; }
        @Override public AttributionSource getAttributionSource() {
            return new AttributionSource.Builder(2000).setPackageName("com.android.shell").build();
        }
        @Override public int getDeviceId() { return 0; }
        @Override public Context getApplicationContext() { return this; }
    }

    static void log(String s) { System.err.println("[callaudio] " + s); System.err.flush(); }

    static Object call(Object target, String name, Class<?>[] types, Object... args) throws Exception {
        Class<?> c = target instanceof Class ? (Class<?>) target : target.getClass();
        Method m = c.getMethod(name, types);
        m.setAccessible(true);
        return m.invoke(target instanceof Class ? null : target, args);
    }

    static String describe(Object device) {
        if (device == null) return "none";
        try {
            return call(device, "getType", new Class<?>[]{}) + "/" + call(device, "getProductName", new Class<?>[]{})
                    + "/" + call(device, "getAddress", new Class<?>[]{});
        } catch (Throwable t) { return String.valueOf(device); }
    }

    static int field(String cls, String name) throws Exception {
        return Class.forName(cls).getField(name).getInt(null);
    }

    static Object audioManager() throws Exception {
        Class<?> am = Class.forName("android.media.AudioManager");
        Constructor<?> ctor = am.getConstructor(Context.class);
        return ctor.newInstance(new FakeContext());
    }

    static Object format(int rate, String channels) throws Exception {
        Object b = Class.forName("android.media.AudioFormat$Builder").getConstructor().newInstance();
        call(b, "setSampleRate", new Class<?>[]{int.class}, rate);
        call(b, "setEncoding", new Class<?>[]{int.class}, field("android.media.AudioFormat", "ENCODING_PCM_16BIT"));
        call(b, "setChannelMask", new Class<?>[]{int.class}, field("android.media.AudioFormat", channels));
        return call(b, "build", new Class<?>[]{});
    }

    /** The phone's "Telephony Tx" output (AudioDeviceInfo.TYPE_TELEPHONY), or null. */
    static Object telephonyTx(Object am) throws Exception {
        int outputs = field("android.media.AudioManager", "GET_DEVICES_OUTPUTS");
        Object[] devices = (Object[]) call(am, "getDevices", new Class<?>[]{int.class}, outputs);
        for (Object d : devices) {
            if ((Integer) call(d, "getType", new Class<?>[]{}) == 18) return d;
        }
        log("no TYPE_TELEPHONY among " + devices.length + " outputs");
        return null;
    }

    // incall_music_uplink takes 44.1/48 kHz stereo only (dumpsys media.audio_policy).
    static final int TRACK_RATE = 48000;
    static int prev = 0;

    /** 24 kHz mono PCM16 -> 48 kHz stereo: a midpoint and the sample, each on both channels. */
    static int upsample(byte[] in, int n, byte[] out) {
        int o = 0;
        for (int i = 0; i + 1 < n; i += 2) {
            int x = (short) ((in[i] & 0xff) | (in[i + 1] << 8));
            int mid = (prev + x) / 2;
            for (int v : new int[]{mid, mid, x, x}) {
                out[o++] = (byte) v;
                out[o++] = (byte) (v >> 8);
            }
            prev = x;
        }
        return o;
    }

    /** ITelecomService straight from the service manager: no Activity, so HyperOS's start hook never sees it. */
    static Object telecom() throws Exception {
        Object binder = Class.forName("android.os.ServiceManager")
                .getMethod("getService", String.class).invoke(null, "telecom");
        Class<?> stub = Class.forName("com.android.internal.telecom.ITelecomService$Stub");
        return stub.getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, binder);
    }

    static Method byName(Object target, String name) {
        for (Method m : target.getClass().getMethods()) {
            if (m.getName().equals(name)) return m;
        }
        throw new IllegalStateException("no method " + name);
    }

    static int telecomCommand(String mode, String[] args) throws Exception {
        Object t = telecom();
        if (mode.equals("dial")) {
            Object uri = Class.forName("android.net.Uri").getMethod("fromParts", String.class, String.class, String.class)
                    .invoke(null, "tel", args[1], null);
            Object extras = Class.forName("android.os.Bundle").getConstructor().newInstance();
            Method place = byName(t, "placeCall");
            Object[] params = new Object[place.getParameterCount()];
            params[0] = uri;
            params[1] = extras;
            for (int i = 2; i < params.length; i++) params[i] = i == 2 ? "com.android.shell" : null;
            place.invoke(t, params);
            System.out.println("DIALED");
            return 0;
        }
        if (mode.equals("hangup")) {
            Method end = byName(t, "endCall");
            Object r = end.getParameterCount() == 1 ? end.invoke(t, "com.android.shell") : end.invoke(t);
            System.out.println("ENDED=" + r);
            return 0;
        }
        return 2;
    }

    public static void main(String[] args) throws Exception {
        String mode = args.length > 0 ? args[0] : "check";
        if (mode.equals("dial") || mode.equals("hangup")) {
            System.exit(telecomCommand(mode, args));
        }
        Object am = audioManager();
        boolean ok = (Boolean) call(am, "isPstnCallAudioInterceptable", new Class<?>[]{});
        if (mode.equals("check")) {
            System.out.println("INTERCEPTABLE=" + ok);
            int callState = -1;
            try { callState = (Integer) call(am, "getMode", new Class<?>[]{}); } catch (Throwable t) { }
            System.out.println("AUDIO_MODE=" + callState);
            System.out.println("TELEPHONY_TX=" + describe(telephonyTx(am)));
            System.out.flush();
            System.exit(0);
            return;
        }
        if (!mode.equals("bridge")) { log("unknown mode " + mode); System.exit(2); }
        if (!ok) { log("not interceptable"); System.exit(3); }

        int port = Integer.parseInt(args[1]);
        int recRate = Integer.parseInt(args[2]);
        int playRate = Integer.parseInt(args[3]);
        Class<?> afmt = Class.forName("android.media.AudioFormat");

        ServerSocket server = new ServerSocket(port, 1, InetAddress.getByName("127.0.0.1"));
        System.out.println("LISTENING " + port);
        System.out.flush();
        Socket sock = server.accept();
        sock.setTcpNoDelay(true);
        log("client connected");

        final Object record = call(am, "getCallDownlinkExtractionAudioRecord", new Class<?>[]{afmt}, format(recRate, "CHANNEL_IN_MONO"));
        final Object track = call(am, "getCallUplinkInjectionAudioTrack", new Class<?>[]{afmt}, format(TRACK_RATE, "CHANNEL_OUT_STEREO"));
        // 2026-10-13: without an explicit device MediaTek's policy opened the
        // track on TELEPHONY_TX, then resolved startOutput to the earpiece and
        // invalidated it ("Device doesn't sync between startOutput[0x1] and
        // getOutputForAttr"), 51 times in 3 s, and nothing was ever played.
        Object tx = telephonyTx(am);
        if (tx != null) {
            Object pinned = call(track, "setPreferredDevice", new Class<?>[]{Class.forName("android.media.AudioDeviceInfo")}, tx);
            log("preferred device " + describe(tx) + " -> " + pinned);
        }
        // A streaming track holds its first audio until the start threshold is
        // filled, and that defaults to the whole buffer: dead time in front of
        // every turn that starts from an empty track. 20 ms is enough here,
        // because the laptop paces her voice 120 ms ahead of real time.
        try {
            log("track buffer=" + call(track, "getBufferSizeInFrames", new Class<?>[]{})
                    + " capacity=" + call(track, "getBufferCapacityInFrames", new Class<?>[]{})
                    + " startThreshold=" + call(track, "getStartThresholdInFrames", new Class<?>[]{}));
            call(track, "setStartThresholdInFrames", new Class<?>[]{int.class}, TRACK_RATE / 50);
            log("track startThreshold now " + call(track, "getStartThresholdInFrames", new Class<?>[]{})
                    + ", latency ms " + call(track, "getLatency", new Class<?>[]{}));
            log("record buffer=" + call(record, "getBufferSizeInFrames", new Class<?>[]{}));
        } catch (Throwable t) { log("buffer tuning failed: " + (t.getCause() != null ? t.getCause() : t)); }
        log("record state=" + call(record, "getState", new Class<?>[]{})
                + " track state=" + call(track, "getState", new Class<?>[]{}));
        call(record, "startRecording", new Class<?>[]{});
        call(track, "play", new Class<?>[]{});

        // Once a second: where each stream was routed, how far the track has
        // actually played, and the audio mode. 2026-10-13: the track took one
        // buffer (5760 bytes) and never drained, so her voice never reached
        // the call; this says why the next time.
        Thread diag = new Thread(() -> {
            try {
                while (true) {
                    Object rdev = call(track, "getRoutedDevice", new Class<?>[]{});
                    Object cdev = call(record, "getRoutedDevice", new Class<?>[]{});
                    log("track head=" + call(track, "getPlaybackHeadPosition", new Class<?>[]{})
                            + " underruns=" + call(track, "getUnderrunCount", new Class<?>[]{})
                            + " playState=" + call(track, "getPlayState", new Class<?>[]{})
                            + " routed=" + describe(rdev)
                            + " | record routed=" + describe(cdev)
                            + " | mode=" + call(am, "getMode", new Class<?>[]{}));
                    Thread.sleep(1000);
                }
            } catch (Throwable t) { log("diag ended: " + t); }
        }, "diag");
        diag.setDaemon(true);
        diag.start();

        final OutputStream out = sock.getOutputStream();
        final InputStream in = sock.getInputStream();
        final int writeBlocking = field("android.media.AudioTrack", "WRITE_BLOCKING");

        Thread down = new Thread(() -> {
            byte[] buf = new byte[recRate / 50 * 2]; // 20 ms
            try {
                while (true) {
                    int n = (Integer) call(record, "read", new Class<?>[]{byte[].class, int.class, int.class}, buf, 0, buf.length);
                    if (n < 0) { log("record read " + n); break; }
                    if (n > 0) { out.write(buf, 0, n); out.flush(); }
                }
            } catch (Throwable t) { log("downlink ended: " + t); }
        }, "downlink");
        down.setDaemon(true);
        down.start();

        if (playRate * 2 != TRACK_RATE) { log("play rate must be " + TRACK_RATE / 2); System.exit(4); }
        byte[] buf = new byte[playRate / 50 * 2];
        byte[] wide = new byte[buf.length * 4 + 8];
        long injected = 0;
        try {
            while (true) {
                int n = in.read(buf);
                if (n < 0) break;
                if (n % 2 == 1) { int m = in.read(buf, n, 1); if (m > 0) n += m; }
                int w = upsample(buf, n, wide);
                call(track, "write", new Class<?>[]{byte[].class, int.class, int.class, int.class}, wide, 0, w, writeBlocking);
                injected += n;
            }
        } catch (Throwable t) {
            Throwable cause = t.getCause() != null ? t.getCause() : t;
            log("uplink ended: " + cause);
        }
        log("injected bytes=" + injected);
        try { call(record, "stop", new Class<?>[]{}); call(track, "stop", new Class<?>[]{}); } catch (Throwable t) { }
        try { call(record, "release", new Class<?>[]{}); call(track, "release", new Class<?>[]{}); } catch (Throwable t) { }
        sock.close();
        server.close();
        System.exit(0);
    }
}
