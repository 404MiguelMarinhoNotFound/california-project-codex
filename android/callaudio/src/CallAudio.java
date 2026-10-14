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
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * California's phone-side call audio helper. Runs as the shell user:
 *
 *   CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio <mode> ...
 *
 * Modes:
 *   check                      is PSTN call audio interceptable on this phone?
 *   accept <TC@key>            answer the ring telecom calls <TC@key>, only if it
 *                              is still the sole call and RINGING; no audio routing
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
        @Override public String getAttributionTag() { return null; }
        // AudioManager.setMicrophoneMute reads the caller's user id off its
        // Context; null here crashed the bridge mid-call (2026-10-13 20:18).
        @Override public int getUserId() { return 0; }
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

    /**
     * Find my phone: a two-tone beep on the ALARM stream, which plays through
     * silent mode, at the alarm stream's max volume. The previous alarm
     * volume is put back in a finally, whatever happens.
     */
    static int ring(Object am, int seconds) throws Exception {
        int alarm = field("android.media.AudioManager", "STREAM_ALARM");
        Class<?>[] one = {int.class};
        int before = (Integer) call(am, "getStreamVolume", one, alarm);
        int max = (Integer) call(am, "getStreamMaxVolume", one, alarm);
        Object track = null;
        try {
            call(am, "setStreamVolume", new Class<?>[]{int.class, int.class, int.class}, alarm, max, 0);
            Object ab = Class.forName("android.media.AudioAttributes$Builder").getConstructor().newInstance();
            call(ab, "setUsage", one, field("android.media.AudioAttributes", "USAGE_ALARM"));
            call(ab, "setContentType", one, field("android.media.AudioAttributes", "CONTENT_TYPE_SONIFICATION"));
            Object attrs = call(ab, "build", new Class<?>[]{});
            Object tb = Class.forName("android.media.AudioTrack$Builder").getConstructor().newInstance();
            call(tb, "setAudioAttributes", new Class<?>[]{Class.forName("android.media.AudioAttributes")}, attrs);
            call(tb, "setAudioFormat", new Class<?>[]{Class.forName("android.media.AudioFormat")},
                    format(TRACK_RATE, "CHANNEL_OUT_MONO"));
            // Android 16's AudioTrack.Builder reads the attribution tag off a
            // Context; app_process has none, so it gets the shell's.
            call(tb, "setContext", new Class<?>[]{Context.class}, new FakeContext());
            track = call(tb, "build", new Class<?>[]{});
            call(track, "play", new Class<?>[]{});
            System.out.println("RINGING");
            System.out.flush();
            short[] beep880 = tone(880, 300), beep660 = tone(660, 300), gap = new short[TRACK_RATE / 5];
            Class<?>[] write = {short[].class, int.class, int.class};
            long end = System.currentTimeMillis() + seconds * 1000L;
            boolean high = true;
            while (System.currentTimeMillis() < end) {
                short[] beep = high ? beep880 : beep660;
                call(track, "write", write, beep, 0, beep.length);
                call(track, "write", write, gap, 0, gap.length);
                high = !high;
            }
            return 0;
        } finally {
            if (track != null) {
                try { call(track, "stop", new Class<?>[]{}); call(track, "release", new Class<?>[]{}); } catch (Throwable t) { }
            }
            call(am, "setStreamVolume", new Class<?>[]{int.class, int.class, int.class}, alarm, before, 0);
            log("alarm volume " + before + " -> " + max + " -> restored " + call(am, "getStreamVolume", one, alarm));
        }
    }

    static short[] tone(int hz, int ms) {
        short[] out = new short[TRACK_RATE * ms / 1000];
        for (int i = 0; i < out.length; i++) {
            out[i] = (short) (Math.sin(2 * Math.PI * hz * i / TRACK_RATE) * 0.6 * 32767);
        }
        return out;
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

    static final Pattern CALL_KEY = Pattern.compile("[A-Za-z0-9@_.:-]{1,128}");
    static final Pattern ROW_ID = Pattern.compile("\\bid=([A-Za-z0-9@_.:-]+)");
    static final Pattern ROW_STATE = Pattern.compile("\\bstate=([A-Z_]+)");

    static String dumpsys(String service) throws Exception {
        Process p = new ProcessBuilder("dumpsys", service).redirectErrorStream(true).start();
        byte[] out = p.getInputStream().readAllBytes();
        if (p.waitFor() != 0) throw new IllegalStateException("dumpsys failed");
        return new String(out, StandardCharsets.UTF_8);
    }

    /**
     * Why telecom's live calls do not allow answering <expected>, or null when
     * it is the sole call and RINGING. Rows of CallsManager.mCalls only, each
     * with exactly one id= and one state= (the same rules as the laptop's
     * services/phone_adb.parse_call_topology).
     */
    static String acceptRefusal(String dump, String expected) {
        int a = dump.indexOf("mCalls:");
        int b = a < 0 ? -1 : dump.indexOf("mCallAudioManager:", a);
        if (a < 0 || b < 0) return "REFUSED_UNREADABLE";
        String key = null, state = null;
        int rows = 0;
        for (String row : dump.substring(a + "mCalls:".length(), b).split("\r?\n")) {
            if (row.trim().isEmpty()) continue;
            rows++;
            Matcher i = ROW_ID.matcher(row), s = ROW_STATE.matcher(row);
            if (!i.find() || !s.find()) return "REFUSED_UNREADABLE";
            key = i.group(1);
            state = s.group(1);
            if (i.find() || s.find()) return "REFUSED_UNREADABLE";
        }
        if (rows == 0) return "REFUSED_NO_RING";
        if (rows > 1) return "REFUSED_BUSY";
        if (!expected.equals(key) || !"RINGING".equals(state)) return "REFUSED_STALE";
        return null;
    }

    /**
     * One acceptRingingCall for the ring <expected>, re-checked against
     * telecom immediately before it. acceptRingingCall answers whatever rings,
     * so the re-check is what binds it to this call; the laptop then reads the
     * same key back ACTIVE. Prints fixed markers only, never a dump, a name or
     * a number. Does not wait for ACTIVE and touches no audio route.
     */
    static int accept(String[] args) {
        if (args.length != 2 || !CALL_KEY.matcher(args[1]).matches()) {
            System.out.println("REFUSED_BAD_KEY");
            return 2;
        }
        Object t;
        Method acceptCall;
        try {
            t = telecom();
            acceptCall = t.getClass().getMethod("acceptRingingCall", String.class);
        } catch (Throwable e) {
            System.out.println("ACCEPT_UNSUPPORTED");
            return 4;
        }
        String refusal;
        try {
            refusal = acceptRefusal(dumpsys("telecom"), args[1]);
        } catch (Throwable e) {
            refusal = "REFUSED_UNREADABLE";
        }
        if (refusal != null) {
            System.out.println(refusal);
            return 3;
        }
        try {
            acceptCall.invoke(t, "com.android.shell");
        } catch (Throwable e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            System.out.println("ACCEPT_FAILED " + cause.getClass().getSimpleName());
            return 1;
        }
        System.out.println("ACCEPT_REQUESTED");
        return 0;
    }

    public static void main(String[] args) throws Exception {
        String mode = args.length > 0 ? args[0] : "check";
        if (mode.equals("accept")) {
            int code = accept(args);
            System.out.flush();
            System.exit(code); // binder threads would otherwise keep app_process alive
        }
        if (mode.equals("dial") || mode.equals("hangup")) {
            int code;
            try {
                code = telecomCommand(mode, args);
            } catch (Throwable t) {
                Throwable cause = t.getCause() != null ? t.getCause() : t;
                System.out.println("ERROR " + cause.getClass().getSimpleName() + ": " + cause.getMessage());
                code = 1;
            }
            System.out.flush();
            System.exit(code);
        }
        Object am = audioManager();
        if (mode.equals("ring")) {
            int code;
            try {
                code = ring(am, Integer.parseInt(args.length > 1 ? args[1] : "20"));
            } catch (Throwable t) {
                Throwable cause = t.getCause() != null ? t.getCause() : t;
                System.out.println("ERROR " + cause.getClass().getSimpleName() + ": " + cause.getMessage());
                code = 1;
            }
            System.out.flush();
            System.exit(code);
        }
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
        // The laptop connects within a second of LISTENING. If it never does
        // (it stopped while the helper started), exit rather than wait forever
        // holding the port and, later, his muted mic (final review 2026-10-13).
        server.setSoTimeout(20000);
        Socket sock;
        try {
            sock = server.accept();
        } catch (java.net.SocketTimeoutException e) {
            log("no client within 20 s, exiting");
            server.close();
            System.exit(5);
            return;
        }
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
        // The phone's own mic would put the room (him, the TV) into the call
        // under her voice. Injection is a separate uplink mix, so muting the
        // mic leaves her audible -- verified live, see android/callaudio/README.md.
        // Optional: a mute that fails must never take her voice with it -- an
        // uncaught exception here killed the whole bridge on a live call.
        boolean muteMic = args.length > 4 && args[4].equals("mutemic");
        boolean micWasMuted = false;
        if (muteMic) {
            try {
                micWasMuted = (Boolean) call(am, "isMicrophoneMute", new Class<?>[]{});
                call(am, "setMicrophoneMute", new Class<?>[]{boolean.class}, true);
                log("mic muted (was " + micWasMuted + ")");
            } catch (Throwable t) {
                muteMic = false;
                log("mic mute failed, call continues unmuted: " + (t.getCause() != null ? t.getCause() : t));
            }
        }

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
        if (muteMic) {
            try { call(am, "setMicrophoneMute", new Class<?>[]{boolean.class}, micWasMuted); } catch (Throwable t) { log("mic restore failed: " + t); }
        }
        try { call(record, "stop", new Class<?>[]{}); call(track, "stop", new Class<?>[]{}); } catch (Throwable t) { }
        try { call(record, "release", new Class<?>[]{}); call(track, "release", new Class<?>[]{}); } catch (Throwable t) { }
        sock.close();
        server.close();
        System.exit(0);
    }
}
