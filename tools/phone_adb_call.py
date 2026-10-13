"""
Phone calls without Phone Link: ADB to the phone for control, the phone's own
call-audio interception for her ears and mouth. Test harness, not wired into
PhoneService yet.

    uv run python tools/phone_adb_call.py check
    uv run python tools/phone_adb_call.py state
    uv run python tools/phone_adb_call.py probe <number> [--seconds 20]
    uv run python tools/phone_adb_call.py gemini <number|contact> --kind personal --goal "..."

`check`  pushes the helper and asks Android whether PSTN call audio can be
         intercepted (AudioManager.isPstnCallAudioInterceptable).
`probe`  dials, waits for an answer, records the other side to
         debug/phone_adb/<stamp>-downlink.wav while injecting a spoken test line
         (Google TTS, the room voice) into the call, then hangs up.
`gemini` the real thing: Gemini Live on config.yaml's phone backend (Vertex),
         audio through the phone instead of Phone Link + VB-CABLE.

Phone side: tools/android_call_audio (build.ps1 -> callaudio.dex), run as the
shell user through app_process. Needs Wireless debugging on and paired once.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logger = logging.getLogger("phone_adb")

HELPER_LOCAL = os.path.join(ROOT, "tools", "android_call_audio", "callaudio.dex")
HELPER_REMOTE = "/data/local/tmp/callaudio.dex"
PORT = 27183
REC_RATE = 16000  # what Gemini Live takes in
PLAY_RATE = 24000  # what Gemini Live speaks
_LEAD_S = 0.12  # how far her voice may run ahead of real time on the phone
_SPEECH_RMS = 0.01  # them talking; the line is digital silence (0.0) between words, speech 0.05-0.2

# PreciseCallState foreground values (android.telephony.PreciseCallState).
IDLE, ACTIVE, HOLDING, DIALING, ALERTING, INCOMING, WAITING, DISCONNECTED, DISCONNECTING = range(9)
_STATE_NAMES = {0: "idle", 1: "active", 2: "holding", 3: "dialing", 4: "alerting", 5: "incoming",
                6: "waiting", 7: "disconnected", 8: "disconnecting"}


class PhoneAdb:
    """The phone over Wireless debugging. Every call goes through `sh`."""

    def __init__(self, serial: str = "", adb: str = "adb"):
        self.adb = adb
        self.serial = serial or self.discover()

    def _run(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run([self.adb, *args], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")

    def discover(self) -> str:
        """The paired phone: an attached tls-connect device, else connect to the mDNS one."""
        services = self._run("mdns", "services").stdout
        match = re.search(r"(\S+)\s+_adb-tls-connect\._tcp\s+(\d+\.\d+\.\d+\.\d+:\d+)", services)
        names = {f"{match.group(1)}._adb-tls-connect._tcp", match.group(2)} if match else set()
        for line in self._run("devices").stdout.splitlines():
            parts = line.split()
            # The phone shows under its mDNS name, or under IP:port after a manual connect.
            # mDNS stops advertising when the phone's screen is off; an attached
            # IP:port device is still the phone (the Mi Box is on :5555, not ours).
            wireless = re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", parts[0] or "") if parts else None
            if len(parts) == 2 and parts[1] == "device" and (
                parts[0] in names or "_adb-tls-connect._tcp" in parts[0]
                or (wireless and not parts[0].endswith(":5555"))
            ):
                return parts[0]
        if not match:
            raise RuntimeError("phone not found: is Wireless debugging on?")
        self._run("connect", match.group(2))
        return match.group(2)

    def sh(self, command: str, timeout: float = 20) -> str:
        return self._run("-s", self.serial, "shell", command, timeout=timeout).stdout

    def call_state(self) -> int | None:
        out = self.sh("dumpsys telephony.registry | grep -m1 mPreciseCallState")
        match = re.search(r"Foreground call state: (\d+)", out)
        return int(match.group(1)) if match else None

    # HyperOS refuses the shell both `am start -a CALL` (its PKMSImpl hook denies
    # CALL_PHONE on the Activity path) and `input keyevent` (INJECT_EVENTS), with
    # "USB debugging (Security settings)" off. ITelecomService through the
    # helper is a plain binder call and is not hooked.
    def dial(self, number: str) -> bool:
        if not re.fullmatch(r"\+?\d{3,15}", number):
            return False
        return "DIALED" in self.sh(self._helper_cmd("dial", number) + " 2>&1")

    def hang_up(self) -> None:
        self.sh(self._helper_cmd("hangup") + " 2>&1")

    # /data/local/tmp is shared by everything that runs as shell, and the file
    # was pushed world-writable (-rw-rw-rw-). The helper runs with the shell's
    # call and audio permissions, so it is pushed read-only and its sha256 is
    # checked against the local build in the SAME shell command that runs it:
    # no window between the check and app_process for a swap (security
    # review, 2026-10-13).
    def _local_hash(self) -> str:
        if not hasattr(self, "_hash"):
            with open(HELPER_LOCAL, "rb") as handle:
                self._hash = hashlib.sha256(handle.read()).hexdigest()
        return self._hash

    def _helper_cmd(self, *args: str) -> str:
        return (
            f'[ "$(sha256sum {HELPER_REMOTE} | cut -d" " -f1)" = "{self._local_hash()}" ] '
            f"&& CLASSPATH={HELPER_REMOTE} exec app_process / CallAudio {' '.join(args)} "
            f"|| echo HELPER_MISMATCH"
        )

    def push_helper(self) -> None:
        """Push the helper read-only, only when the phone's copy is not this build."""
        remote = self.sh(f"sha256sum {HELPER_REMOTE} 2>/dev/null").split()
        if not remote or remote[0] != self._local_hash():
            self.sh(f"rm -f {HELPER_REMOTE}")
            self._run("-s", self.serial, "push", HELPER_LOCAL, HELPER_REMOTE)
        self.sh(f"chmod 444 {HELPER_REMOTE}")

    def helper(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            [self.adb, "-s", self.serial, "shell", self._helper_cmd(*args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )

    def forward(self, port: int) -> None:
        self._run("-s", self.serial, "forward", f"tcp:{port}", f"tcp:{port}")

    def unforward(self, port: int) -> None:
        self._run("-s", self.serial, "forward", "--remove", f"tcp:{port}")


class PhoneCallAudio:
    """
    The call's audio, from the phone. One object, two faces: `capture()` looks
    like LoopbackCapture (her ears) and `speaker()` like CableSpeaker (her
    mouth), so GeminiLiveAgent takes it through its factories unchanged.

    The helper can only open its streams during a call, so `start` waits on a
    thread for the call to be answered before launching it.
    """

    def __init__(self, phone: PhoneAdb, record_path: str = ""):
        self.phone = phone
        self.record_path = record_path
        self.peak = 0.0
        self.blocks = 0
        self.source = "phone downlink"
        self.injected_s = 0.0
        self.connected_at: float | None = None
        # Per-turn response gap: last loud block from them -> her first audio sent.
        self.last_heard_at: float | None = None
        self.gaps: list[float] = []
        self._sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._out: queue.Queue[bytes] = queue.Queue()
        self._busy = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wav = None

    # -- the two faces the agent sees
    def capture(self):
        return _Face(self, "capture")

    def speaker(self):
        return _Face(self, "speaker")

    # -- lifecycle
    def start(self, on_block) -> None:
        thread = threading.Thread(target=self._connect_and_read, args=(on_block,), name="phone-downlink", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _wait_answered(self) -> bool:
        while not self._stop.is_set():
            state = self.phone.call_state()
            if state == ACTIVE:
                return True
            time.sleep(0.3)
        return False

    def _connect_and_read(self, on_block) -> None:
        try:
            if not self._wait_answered():
                return
            self._proc = self.phone.helper("bridge", str(PORT), str(REC_RATE), str(PLAY_RATE))
            line = self._proc.stdout.readline().strip()
            if line == "HELPER_MISMATCH":
                logger.error("callaudio.dex on the phone is not this build; refusing to run it")
                return
            if not line.startswith("LISTENING"):
                logger.error("helper did not start: %r %s", line, self._proc.stderr.read()[:500])
                return
            self.phone.forward(PORT)
            self._sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
            self._sock.settimeout(None)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connected_at = time.monotonic()
            logger.info("Phone audio bridge connected")
            threading.Thread(target=self._log_helper, daemon=True).start()
            sender = threading.Thread(target=self._send_loop, name="phone-uplink", daemon=True)
            sender.start()
            self._threads.append(sender)
            if self.record_path:
                os.makedirs(os.path.dirname(self.record_path), exist_ok=True)
                self._wav = wave.open(self.record_path, "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(REC_RATE)
            import numpy as np

            block = REC_RATE // 50 * 2
            pending = b""
            while not self._stop.is_set():
                data = self._sock.recv(4096)
                if not data:
                    break
                pending += data
                while len(pending) >= block:
                    pcm, pending = pending[:block], pending[block:]
                    self.blocks += 1
                    samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
                    level = float(np.sqrt(np.mean(samples * samples)))
                    self.peak = max(self.peak, level)
                    if level > _SPEECH_RMS:
                        self.last_heard_at = time.monotonic()
                    if self._wav is not None:
                        self._wav.writeframes(pcm)
                    on_block(pcm)
        except Exception:
            if not self._stop.is_set():
                logger.exception("Phone audio bridge failed")

    def _log_helper(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for line in proc.stderr:
            logger.info("helper: %s", line.rstrip())

    def _send_loop(self) -> None:
        """Her voice to the phone, paced to real time so a flush cuts her off now."""
        bytes_per_s = PLAY_RATE * 2
        clock = None  # (wall time, audio seconds sent) of the current run of speech
        while not self._stop.is_set():
            try:
                chunk = self._out.get(timeout=0.1)
            except queue.Empty:
                self._busy.clear()
                clock = None
                continue
            self._busy.set()
            for i in range(0, len(chunk), 960):  # 20 ms at 24 kHz
                if self._stop.is_set():
                    return
                piece = chunk[i:i + 960]
                if clock is None:
                    clock = [time.monotonic(), 0.0]
                    if self.last_heard_at is not None:
                        gap = clock[0] - self.last_heard_at
                        self.gaps.append(round(gap, 2))
                        logger.info("Response gap: %.2fs from their last sound to her first audio", gap)
                ahead = clock[1] - (time.monotonic() - clock[0])
                if ahead > _LEAD_S:
                    time.sleep(ahead - _LEAD_S)
                if not self._busy.is_set():  # flushed mid-chunk
                    break
                try:
                    self._sock.sendall(piece)
                except OSError:
                    return
                clock[1] += len(piece) / bytes_per_s
                self.injected_s += len(piece) / bytes_per_s
            if self._out.empty():
                self._busy.clear()

    def write(self, pcm: bytes) -> None:
        if pcm:
            self._out.put(pcm)

    def flush(self) -> None:
        try:
            while True:
                self._out.get_nowait()
        except queue.Empty:
            pass
        self._busy.clear()

    def idle(self) -> bool:
        return self._out.empty() and not self._busy.is_set()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for closer in (lambda: self._sock and self._sock.shutdown(socket.SHUT_RDWR), lambda: self._sock and self._sock.close()):
            try:
                closer()
            except OSError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self.phone.unforward(PORT)
        if self._wav is not None:
            self._wav.close()
        logger.info("Phone audio: %d blocks heard, peak %.4f, %.1fs of her voice injected; response gaps %s",
                    self.blocks, self.peak, self.injected_s, self.gaps)


class _Face:
    """What GeminiLiveAgent calls; both faces share one bridge and stop it once."""

    def __init__(self, bridge: PhoneCallAudio, kind: str):
        self.bridge, self.kind = bridge, kind

    def start(self, on_block=None) -> None:
        if self.kind == "capture":
            self.bridge.start(on_block)

    def write(self, pcm: bytes) -> None:
        self.bridge.write(pcm)

    def flush(self) -> None:
        self.bridge.flush()

    def idle(self) -> bool:
        return self.bridge.idle()

    def stop(self) -> None:
        self.bridge.stop()

    def __getattr__(self, name):  # peak / blocks / source for the agent's stats
        return getattr(self.bridge, name)


# ------------------------------------------------------------------ commands


def _config() -> dict:
    import yaml
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _resolve_number(config: dict, who: str) -> tuple[str, str]:
    if re.fullmatch(r"\+?[\d ]{3,}", who):
        return who.replace(" ", ""), who
    from services.whatsapp_service import WhatsAppService

    match = WhatsAppService(config).resolve_contact(who)
    if not match.phone or not match.certain:
        raise SystemExit(f"no certain contact for {who!r} (candidates: {match.candidates})")
    return match.phone, match.key or who


def _wait_for(phone: PhoneAdb, wanted: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = phone.call_state()
        if state != last:
            print(f"  {time.strftime('%H:%M:%S')} call state: {_STATE_NAMES.get(state, state)}", flush=True)
            last = state
        if state in wanted:
            return state
        time.sleep(0.3)
    return last


def cmd_check(phone: PhoneAdb) -> int:
    phone.push_helper()
    proc = phone.helper("check")
    out, err = proc.communicate(timeout=30)
    print(out.strip() or err.strip())
    print("call state:", _STATE_NAMES.get(phone.call_state(), "?"))
    return 0 if "INTERCEPTABLE=true" in out else 1


def _tts_line(config: dict, text: str) -> bytes:
    """A spoken test line at PLAY_RATE, in the room voice."""
    import numpy as np

    from services.tts import TTSService

    audio, rate = TTSService(config).synthesize(text)
    audio = np.asarray(audio, dtype="float32").reshape(-1)
    if rate != PLAY_RATE:
        positions = np.linspace(0, len(audio) - 1, int(len(audio) * PLAY_RATE / rate))
        audio = np.interp(positions, np.arange(len(audio)), audio)
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def cmd_probe(phone: PhoneAdb, number: str, seconds: float) -> int:
    config = _config()
    line = _tts_line(config, "Hi, this is California testing a phone line. One, two, three. Bye for now.")
    record = os.path.join(ROOT, "debug", "phone_adb", f"{_stamp()}-downlink.wav")
    phone.push_helper()
    bridge = PhoneCallAudio(phone, record_path=record)
    bridge.start(lambda pcm: None)
    print(f"dialing {number}")
    if not phone.dial(number):
        print("dial failed")
        bridge.stop()
        return 1
    state = _wait_for(phone, {ACTIVE, IDLE, DISCONNECTED}, 60)
    try:
        if state != ACTIVE:
            print("not answered")
            return 1
        deadline = time.monotonic() + 8
        while bridge.connected_at is None and time.monotonic() < deadline:
            time.sleep(0.1)
        print("bridge connected" if bridge.connected_at else "bridge did NOT connect")
        time.sleep(1.0)
        bridge.write(line)
        end = time.monotonic() + seconds
        while time.monotonic() < end and phone.call_state() == ACTIVE:
            time.sleep(0.5)
    finally:
        phone.hang_up()
        bridge.stop()
    print(f"heard {bridge.blocks} blocks, peak {bridge.peak:.4f}; injected {bridge.injected_s:.1f}s; wav: {record}")
    return 0


def cmd_gemini(phone: PhoneAdb, who: str, kind: str, goal: str, details: str, silence_ms: int = 0) -> int:
    config = _config()
    config.setdefault("phone", {})["enabled"] = True
    from services.gemini_live_call import GeminiLiveAgent
    from services.phone_prompts import CallBrief, build_call_prompt
    from services.phone_service import PhoneService

    number, name = _resolve_number(config, who)
    service = PhoneService(config)
    kwargs, why = service._client_kwargs()
    if kwargs is None:
        print(f"backend not configured: {why}")
        return 1
    print(f"calling {name} ({number}) on {service.backend} / {service.model}")
    phone.push_helper()
    record = os.path.join(ROOT, "debug", "phone_adb", f"{_stamp()}-downlink.wav")
    bridge = PhoneCallAudio(phone, record_path=record)
    agent = GeminiLiveAgent(
        client_kwargs=kwargs, model=service.model, voice=service.voice, language_code=service.language_code,
        silence_ms=silence_ms or int(config["phone"].get("silence_duration_ms", 700)),
        capture_factory=bridge.capture, speaker_factory=bridge.speaker,
        on_line=lambda i, w, t, at: print(f"  [{at:5.1f}] {w}: {t}", flush=True),
    )
    prompt = build_call_prompt(CallBrief(to=name, kind=kind, goal=goal, details=details))

    seen = {"call": False}

    def still_connected() -> bool | None:
        # Idle right after the dial means "not registered yet", not "hung up":
        # only believe idle once the call has been seen.
        state = phone.call_state()
        if state is None:
            return None
        live = state in (ACTIVE, HOLDING, DIALING, ALERTING)
        if live:
            seen["call"] = True
            return True
        return False if seen["call"] else None

    def dial() -> bool:
        if not phone.dial(number):
            return False
        return _wait_for(phone, {DIALING, ALERTING, ACTIVE}, 10) in (DIALING, ALERTING, ACTIVE)

    try:
        result = agent.run(
            prompt,
            max_call_s=float(config["phone"].get("max_call_s", 300)),
            no_answer_s=float(config["phone"].get("no_answer_s", 45)),
            still_connected=still_connected,
            dial=dial,
            # The bridge opens the instant the phone says "active": that IS the answer.
            answered=lambda: bridge.connected_at is not None,
        )
    finally:
        if phone.call_state() not in (IDLE, None):
            phone.hang_up()
    print(f"\nended_by={result.ended_by} duration={result.duration_s}s error={result.error or '-'}")
    print("stats:", result.stats)
    print("transcript:\n" + result.transcript(times=True))
    print("downlink wav:", record)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", default="")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("state")
    probe = sub.add_parser("probe")
    probe.add_argument("number")
    probe.add_argument("--seconds", type=float, default=20)
    gem = sub.add_parser("gemini")
    gem.add_argument("who")
    gem.add_argument("--kind", default="general")
    gem.add_argument("--goal", default="")
    gem.add_argument("--details", default="")
    gem.add_argument("--silence-ms", type=int, default=0, help="Gemini end-of-turn silence; default config.yaml's")
    args = parser.parse_args()

    phone = PhoneAdb(args.serial)
    if args.cmd == "check":
        return cmd_check(phone)
    if args.cmd == "state":
        print(_STATE_NAMES.get(phone.call_state(), "unknown"))
        return 0
    if args.cmd == "probe":
        return cmd_probe(phone, args.number, args.seconds)
    return cmd_gemini(phone, args.who, args.kind, args.goal, args.details, args.silence_ms)


if __name__ == "__main__":
    raise SystemExit(main())
