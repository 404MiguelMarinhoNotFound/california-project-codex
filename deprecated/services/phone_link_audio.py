"""
Phone Link-era call audio, cut out of services/gemini_live_call.py on
2026-10-13 when calls moved to ADB (services/phone_call_audio.py). Kept for
reference; nothing imports it.

  callee --(Phone Link, laptop speakers)--> WASAPI loopback --16 kHz PCM--> Gemini Live
  Gemini Live --24 kHz PCM--> VB-CABLE "CABLE Input" --(Phone Link's default mic)--> callee
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

INPUT_RATE = 16000
OUTPUT_RATE = 24000
_BLOCK_MS = 100
_SILENT_PEAK = 0.002


class LoopbackCapture:
    """
    The callee, as a speaker plays them. 16 kHz mono int16 blocks.

    `device` names the speaker to record (a prefix of its name). It used to be
    whatever the default speaker was at the time, and on 2026-10-08 that was a
    pair of Bluetooth headphones the call audio never reached: she heard
    nothing for 35 seconds of a real call. PhoneService now pins the default
    speaker for the call (`phone_link.SpeakerRoute`) and passes the same name
    here. Empty `device` keeps the old default-speaker behaviour.

    The capture thread used to die silently; its error is logged now, and
    `peak` (loudest block, 0-1 RMS) says afterwards whether she heard anything.
    """

    def __init__(self, samplerate: int = INPUT_RATE, block_ms: int = _BLOCK_MS, device: str = ""):
        self.samplerate = samplerate
        self.frames = int(samplerate * block_ms / 1000)
        self.device = device
        self.peak = 0.0
        self.blocks = 0
        self.source = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _speaker(self, sc):
        if self.device:
            for speaker in sc.all_speakers():
                if str(speaker.name).startswith(self.device):
                    return speaker
            logger.warning("Loopback: no speaker named %r; using the default one", self.device)
        return sc.default_speaker()

    def start(self, on_block: Callable[[bytes], None]) -> None:
        def loop():
            # soundcard calls CoInitializeEx only on the thread that first
            # imports it. When anything imported it earlier on another thread,
            # this one has no COM and every call fails with 0x800401F0
            # (CO_E_NOTINITIALIZED): measured 2026-10-08, a deaf capture. So
            # this thread initialises COM itself (MTA, as soundcard does) --
            # AFTER importing soundcard: when this thread is the first to
            # import it, soundcard's own CoInitializeEx must run first, or it
            # gets S_FALSE ("already initialised") and raises 0x100000001
            # (found on the next real call, 2026-10-08).
            com = None
            try:
                import soundcard as sc

                if sys.platform == "win32":
                    import ctypes

                    com = ctypes.windll.ole32
                    com.CoInitializeEx(None, 0)  # S_OK or S_FALSE; balanced below either way
                speaker = self._speaker(sc)
                self.source = str(speaker.name)
                logger.info("Loopback: listening to %s", self.source)
                mic = sc.get_microphone(id=self.source, include_loopback=True)
                with mic.recorder(samplerate=self.samplerate, channels=1) as rec:
                    while not self._stop.is_set():
                        block = rec.record(numframes=self.frames)[:, 0]
                        self.blocks += 1
                        level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
                        self.peak = max(self.peak, level)
                        pcm = np.clip(block * 32767.0, -32768, 32767).astype("<i2").tobytes()
                        on_block(pcm)
            except Exception:
                logger.exception("Loopback capture failed: California cannot hear the call")
            finally:
                if com is not None:
                    com.CoUninitialize()

        self._thread = threading.Thread(target=loop, name="call-loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.blocks:
            logger.info("Loopback: %d blocks from %s, peak level %.4f%s", self.blocks, self.source,
                        self.peak, " (SILENT: she heard nothing)" if self.peak < _SILENT_PEAK else "")


class CableSpeaker:
    """Her voice into the virtual cable Phone Link records from. 24 kHz int16."""

    def __init__(self, device_prefix: str = "CABLE Input", samplerate: int = OUTPUT_RATE):
        self.device_prefix = device_prefix
        self.samplerate = samplerate
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._busy = threading.Event()
        self._thread: threading.Thread | None = None

    def _device(self) -> int:
        import sounddevice as sd

        for index, dev in enumerate(sd.query_devices()):
            api = sd.query_hostapis(dev["hostapi"])["name"]
            if api == "MME" and dev["name"].startswith(self.device_prefix) and dev["max_output_channels"] > 0:
                return index
        raise RuntimeError(f"no output device starting with {self.device_prefix!r}")

    def start(self) -> None:
        import sounddevice as sd

        stream = sd.RawOutputStream(
            samplerate=self.samplerate, channels=1, dtype="int16", device=self._device()
        )
        stream.start()

        def loop():
            try:
                while True:
                    chunk = self._q.get()
                    if chunk is None:
                        return
                    self._busy.set()
                    stream.write(chunk)
                    if self._q.empty():
                        self._busy.clear()
            finally:
                stream.stop()
                stream.close()

        self._thread = threading.Thread(target=loop, name="call-cable", daemon=True)
        self._thread.start()

    def write(self, pcm: bytes) -> None:
        if pcm:
            self._q.put(pcm)

    def flush(self) -> None:
        """They talked over her: drop everything not yet played."""
        try:
            while True:
                item = self._q.get_nowait()
                if item is None:
                    self._q.put(None)
                    break
        except queue.Empty:
            pass
        self._busy.clear()

    def idle(self) -> bool:
        return self._q.empty() and not self._busy.is_set()

    def stop(self) -> None:
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=3)


