"""
PhoneCallAudio: a phone call's audio, straight from the phone.

The callaudio helper (android/callaudio/, `bridge` mode) opens Android's call
audio interception once the call is answered: the other side of the call
(downlink) comes out as 16 kHz mono PCM16, and 24 kHz mono PCM16 sent back is
injected into the call (uplink). Both travel over one TCP socket reached with
`adb forward`.

To GeminiLiveAgent it has two faces: `capture()` replaces the old WASAPI
loopback (her ears) and `speaker()` the old VB-CABLE output (her mouth). No
laptop audio device is involved at all, so the room can never leak into a
call through the laptop.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import subprocess
import threading
import time
import wave
from typing import Callable

import numpy as np

from services.phone_adb import ACTIVE

logger = logging.getLogger(__name__)

REC_RATE = 16000  # what Gemini Live takes in
PLAY_RATE = 24000  # what Gemini Live speaks
_BLOCK = REC_RATE // 50 * 2  # 20 ms of downlink
_CHUNK = PLAY_RATE // 50 * 2  # 20 ms of her voice
# How far her voice may run ahead of real time on the phone. The sender paces
# to this, so a flush (they talked over her) cuts her off within it instead
# of after whatever sits in socket and track buffers.
_LEAD_S = 0.12
# A downlink block above this is them talking: the line is digital silence
# (0.0) between words and speech measured 0.05-0.2 (2026-10-13).
_SPEECH_RMS = 0.01


class PhoneCallAudio:
    def __init__(
        self,
        phone,
        record_path: str = "",
        connect: Callable = socket.create_connection,
        poll_s: float = 0.3,
    ):
        self.phone = phone
        self.record_path = record_path
        self._connect = connect
        self._poll_s = poll_s
        self.peak = 0.0
        self.blocks = 0
        self.source = "phone downlink"
        self.injected_s = 0.0
        self.connected_at: float | None = None
        # Per turn: their last sound -> her first audio sent, for the log.
        self.last_heard_at: float | None = None
        self.gaps: list[float] = []
        self._sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._forwarded = False
        self._stop = threading.Event()
        self._stopped = False
        self._release_lock = threading.Lock()
        # Set by PhoneService once ITS dial went out. Until then an ACTIVE
        # state is someone else's call -- his own, which the bridge must
        # never open on (final review, 2026-10-13).
        self._armed = threading.Event()
        # The helper died or never started while the call was up: the call
        # has no ears or mouth any more and must end, not sit in silence.
        self.failed = False
        self._out: queue.Queue[bytes] = queue.Queue()
        self._busy = threading.Event()
        self._wav = None

    # ------------------------------------------------------------- faces

    def capture(self) -> "_Face":
        return _Face(self, "capture")

    def speaker(self) -> "_Face":
        return _Face(self, "speaker")

    def answered(self) -> bool:
        return self.connected_at is not None

    def arm(self) -> None:
        """Our dial went out: the next ACTIVE is the call to bridge."""
        self._armed.set()

    # ------------------------------------------------------------- ears

    def start(self, on_block: Callable[[bytes], None]) -> None:
        threading.Thread(target=self._run, args=(on_block,), name="phone-downlink", daemon=True).start()

    def _wait_answered(self) -> bool:
        while not self._stop.is_set():
            if self._armed.is_set() and self.phone.call_state() == ACTIVE:
                return True
            self._stop.wait(self._poll_s)
        return False

    def _run(self, on_block) -> None:
        try:
            if not self._wait_answered():
                return
            args = ["bridge", str(self.phone.port), str(REC_RATE), str(PLAY_RATE)]
            if getattr(self.phone, "mute_phone_mic", False):
                args.append("mutemic")
            self._proc = self.phone.helper(*args)
            line = (self._proc.stdout.readline() or "").strip()
            if line == "HELPER_MISMATCH":
                logger.error("callaudio.dex on the phone is not this build; refusing to run it")
                self.failed = True
                return
            if not line.startswith("LISTENING"):
                logger.error("Call audio helper did not start: %r", line[:200])
                self.failed = True
                return
            self.phone.forward(self.phone.port)
            self._forwarded = True
            sock = self._connect(("127.0.0.1", self.phone.port), timeout=5)
            self._sock = sock
            if self._stop.is_set():
                return  # stopped while the helper started: the finally closes it all
            sock.settimeout(None)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass  # a socketpair in tests
            self.connected_at = time.monotonic()
            logger.info("Phone audio bridge connected")
            threading.Thread(target=self._log_helper, daemon=True).start()
            threading.Thread(target=self._send_loop, name="phone-uplink", daemon=True).start()
            self._open_wav()
            pending = b""
            while not self._stop.is_set():
                data = sock.recv(4096)
                if not data:
                    break
                pending += data
                while len(pending) >= _BLOCK:
                    pcm, pending = pending[:_BLOCK], pending[_BLOCK:]
                    self._heard(pcm)
                    on_block(pcm)
            if not self._stop.is_set():
                logger.error("The phone closed the call audio while the call was up")
                self.failed = True
        except Exception:
            if not self._stop.is_set():
                logger.exception("Phone audio bridge failed")
                self.failed = True
        finally:
            if self._stop.is_set() or self.failed:
                self._release()

    def _heard(self, pcm: bytes) -> None:
        self.blocks += 1
        samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        level = float(np.sqrt(np.mean(samples * samples)))
        self.peak = max(self.peak, level)
        if level > _SPEECH_RMS:
            self.last_heard_at = time.monotonic()
        if self._wav is not None:
            self._wav.writeframes(pcm)

    def _open_wav(self) -> None:
        if not self.record_path:
            return
        os.makedirs(os.path.dirname(self.record_path) or ".", exist_ok=True)
        self._wav = wave.open(self.record_path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(REC_RATE)

    def _log_helper(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            for line in proc.stderr:
                logger.info("callaudio: %s", line.rstrip())
        except Exception:
            pass

    # ------------------------------------------------------------- mouth

    def _send_loop(self) -> None:
        bytes_per_s = PLAY_RATE * 2
        clock = None  # [wall time, audio seconds sent] of the current run of speech
        while not self._stop.is_set():
            try:
                chunk = self._out.get(timeout=0.1)
            except queue.Empty:
                self._busy.clear()
                clock = None
                continue
            self._busy.set()
            for i in range(0, len(chunk), _CHUNK):
                if self._stop.is_set():
                    return
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
                piece = chunk[i:i + _CHUNK]
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

    # ------------------------------------------------------------- end

    def _release(self) -> None:
        """
        Close what exists right now; safe to call from stop() and from the
        bridge thread, in either order. A stop that lands while the helper is
        still starting would otherwise leave the socket, the adb forward and
        the phone-side helper (with his mic muted) behind.
        """
        with self._release_lock:
            sock, self._sock = self._sock, None
            if sock is not None:
                for close in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                    try:
                        close()
                    except OSError:
                        pass
            proc, self._proc = self._proc, None
            if proc is not None:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if self._forwarded:
                self._forwarded = False
                self.phone.unforward(self.phone.port)
            wav, self._wav = self._wav, None
            if wav is not None:
                wav.close()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        self._release()
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
