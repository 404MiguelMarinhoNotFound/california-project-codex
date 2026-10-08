"""
The call agent: one Gemini Live session for the length of one phone call.

Audio path, proven on the real laptop (2026-10-06):

  callee --(Phone Link, laptop speakers)--> WASAPI loopback --16 kHz PCM--> Gemini Live
  Gemini Live --24 kHz PCM--> VB-CABLE "CABLE Input" --(Phone Link's default mic)--> callee

The loopback carries ONLY the callee: her voice goes into the cable, never out
of the speakers, so the line is echo-free and Gemini's own voice-activity
detection can tell exactly when they are talking. That is also why barge-in
needs no work here -- when they talk over her, Gemini sends `interrupted` and
the queued audio is dropped.

The session ends on the first of: the agent calls `end_call`, the callee hangs
up (Phone Link's call window goes away), nobody speaks within `no_answer_s`,
`max_call_s` runs out, or the service asks it to stop.

`google.genai` and `soundcard` are imported lazily, inside the factories, so
importing this module never needs them -- the unit tests drive it with a fake
session and fake audio.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

INPUT_RATE = 16000
OUTPUT_RATE = 24000
_BLOCK_MS = 100
_WATCH_INTERVAL_S = 2.0
# After end_call, how long to let her goodbye finish playing before hanging up.
_GOODBYE_DRAIN_S = 8.0
# Minimum wait after end_call before trusting speaker.idle().
_GOODBYE_GRACE_S = 0.8
# The opening nudge. Measured 2026-10-08 on simulated calls: in roughly one
# call in three the native-audio model heard "Estou?" and never began -- it
# reaches for end_call instead, and however the refusal is delivered it either
# stays silent (SILENT) or repeats its opener (WHEN_IDLE). Prompt wording did
# not fix it, so this does: if they have spoken and she has not said a word
# this long after their last words, she is told to speak. Opening only, so a
# normal pause later in the call is never stepped on. Up to _NUDGE_MAX times.
_NUDGE_AFTER_S = 2.5
# A "they hung up" reading is not believed if they spoke this recently.
_LIVE_IF_HEARD_S = 8.0
_NUDGE_MAX = 2
_NUDGE_TEXT = (
    "[The call has been answered and they are waiting for you. Greet them and "
    "say why you are calling, now, in their language.]"
)

OUTCOME_STATUSES = [
    "booked",
    "alternative_agreed",
    "not_available",
    "answered",
    "message_delivered",
    "callback_needed",
    "voicemail",
    "no_answer",
    "failed",
]

# The live agent has ONE tool: hang up. It used to have record_outcome too, and
# that tool was the single biggest cause of her sounding broken. Measured with
# tools/eval_phone_conversation.py on 2026-10-08: the native-audio model calls
# record_outcome + end_call the instant it hears "Taberna da Praia, boa noite",
# before a word of its own -- it plays the whole call out in its head. Refusing
# those calls made her restart her sentence (221 refusals over 8 calls) or go
# silent; replying SILENTLY cut the restarts but not the dead air. With the
# tool removed: 8 early refusals instead of 221, every call ran to a natural
# end, and the transcript judge went from 2.4 (old prompt) to 3.5. What the
# call achieved is now read from the transcript AFTER the call, by
# services/call_outcome.py, which is a text task and does not need the voice.
TOOL_DECLARATIONS = [
    {
        "name": "end_call",
        "description": (
            "Hang up. Only after you have said goodbye out loud and they have said "
            "goodbye too (on voicemail: after your message)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"reason": {"type": "STRING"}},
        },
    },
]


@dataclass
class CallLine:
    who: str  # "them" | "california"
    text: str


@dataclass
class CallResult:
    lines: list[CallLine] = field(default_factory=list)
    outcome: dict | None = None
    ended_by: str = ""  # end_call | hung_up | no_answer | max_duration | stopped | error
    duration_s: float = 0.0
    error: str = ""

    @property
    def heard_them(self) -> bool:
        return any(line.who == "them" and line.text.strip() for line in self.lines)

    def transcript(self, max_chars: int = 4000) -> str:
        rows = [f"{'Them' if l.who == 'them' else 'California'}: {l.text.strip()}" for l in self.lines if l.text.strip()]
        text = "\n".join(rows)
        return text if len(text) <= max_chars else "...\n" + text[-max_chars:]


def _hang_up_too_early(result: "CallResult") -> str:
    """
    Why end_call must be refused right now, or "" if she may hang up.

    Found live 2026-10-07 on Vertex: given the callee's "Estou?", the model
    hung up 0.75s in, before saying a word. So she must have spoken, and
    someone must have been heard -- their "Estou?" or a voicemail greeting
    both count, so a left message can still end the call. Whether the call
    was answered by nobody at all is the watchdog's job (`no_answer_s`), never
    the model's.
    """
    if not any(l.who == "california" and l.text.strip() for l in result.lines):
        return "Too early: you have not spoken yet. Greet them and have the conversation first."
    if not result.heard_them:
        return "Too early: nobody has spoken on the line yet. Wait for them."
    return ""


# How a tool reply is delivered. Live API tool calls are non-blocking by
# default (Google's async function calling docs), and a reply with no
# `scheduling` is handled the old way, which interrupts her: measured on
# 2026-10-08, 8 simulated calls produced 221 refused record_outcome calls, and
# each refusal made her restart her sentence ("...É possível?Boa noite! Fala a
# Califórnia..."). SILENT lets her keep talking and use the reply later.
# Every reply is SILENT, refused hang-ups included: a refusal only ever happens
# at the start of a call, and WHEN_IDLE made the model start a fresh turn when
# she paused -- a doubled opener, once in English ("Let me check").
_SILENT = "SILENT"


def _reply(call, response: dict, scheduling: str) -> dict:
    return {"id": call.id, "name": call.name, "response": response, "scheduling": scheduling}


# ----------------------------------------------------------------- audio I/O


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
            # this thread initialises COM itself (MTA, as soundcard does).
            com = None
            if sys.platform == "win32":
                import ctypes

                com = ctypes.windll.ole32
                com.CoInitializeEx(None, 0)
            try:
                import soundcard as sc

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
                        self.peak, " (SILENT: she heard nothing)" if self.peak < 0.002 else "")


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


# ----------------------------------------------------------------- the agent


def _default_client_factory(client_kwargs: dict):
    """
    `client_kwargs` is what PhoneService resolved for the configured backend:
    {"api_key": ...} for AI Studio, {"vertexai": True, "project": ..., "location": ...}
    for Vertex with a service account, or {"vertexai": True, "api_key": ...} for
    Vertex express mode.
    """
    from google import genai

    return genai.Client(**client_kwargs)


class GeminiLiveAgent:
    def __init__(
        self,
        client_kwargs: dict,
        model: str,
        voice: str = "Aoede",
        language_code: str = "",
        silence_ms: int = 700,
        cable_device: str = "CABLE Input",
        listen_device: str = "",
        client_factory: Callable | None = None,
        capture_factory: Callable | None = None,
        speaker_factory: Callable | None = None,
    ):
        self.client_kwargs = dict(client_kwargs)
        self.model = model
        self.voice = voice
        self.language_code = language_code
        self.silence_ms = silence_ms
        self.client_factory = client_factory or _default_client_factory
        self.capture_factory = capture_factory or (lambda: LoopbackCapture(device=listen_device))
        self.speaker_factory = speaker_factory or (lambda: CableSpeaker(cable_device))

    def _config(self, prompt: str):
        from google.genai import types

        speech = {"voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}}}
        if self.language_code:
            speech["language_code"] = self.language_code
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=prompt,
            speech_config=speech,
            input_audio_transcription={},
            output_audio_transcription={},
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            realtime_input_config={
                "automatic_activity_detection": {"silence_duration_ms": int(self.silence_ms)}
            },
        )

    def run(
        self,
        prompt: str,
        max_call_s: float,
        no_answer_s: float,
        still_connected: Callable[[], bool | None] | None = None,
        stop: threading.Event | None = None,
        dial: Callable[[], bool] | None = None,
        mute: Callable[[], bool] | None = None,
    ) -> CallResult:
        """
        Blocking: one call, start to finish. Never raises.

        `dial` runs once the session is connected and the loopback is
        listening, so a callee who answers on the first ring is not talking
        into nothing. It returns False when the call did not go out.

        `mute` is True while California is talking to the room: the loopback
        hears the laptop speakers, so without it her room voice reaches the
        call agent as if the callee said it (seen 2026-10-07: "looks like it
        didn't go through" transcribed as theirs). Muted blocks go as silence.
        """
        result = CallResult()
        started = time.monotonic()
        try:
            asyncio.run(self._run(prompt, max_call_s, no_answer_s, still_connected, stop or threading.Event(), dial, result, mute))
        except Exception as exc:  # the call must always come back with a result
            logger.exception("Call agent failed")
            result.ended_by = result.ended_by or "error"
            result.error = str(exc)[:300]
        result.duration_s = round(time.monotonic() - started, 1)
        return result

    async def _run(self, prompt, max_call_s, no_answer_s, still_connected, stop, dial, result: CallResult, mute=None) -> None:
        loop = asyncio.get_running_loop()
        audio_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        finished = asyncio.Event()
        clock = {"started": time.monotonic()}

        def finish(reason: str) -> None:
            if not result.ended_by:
                result.ended_by = reason
            finished.set()

        def on_block(pcm: bytes) -> None:
            try:
                muted = mute is not None and mute() is True
            except Exception:
                muted = False  # never let it kill the capture thread
            if muted:
                pcm = bytes(len(pcm))

            def put():
                if audio_in.full():
                    audio_in.get_nowait()  # stale audio is worthless; keep it live
                audio_in.put_nowait(pcm)

            loop.call_soon_threadsafe(put)

        def add_line(who: str, text: str) -> None:
            if not text:
                return
            if who == "them":
                clock["them_at"] = time.monotonic()
            if result.lines and result.lines[-1].who == who:
                result.lines[-1].text += text
            else:
                result.lines.append(CallLine(who, text))

        client = self.client_factory(self.client_kwargs)
        speaker = self.speaker_factory()
        capture = self.capture_factory()
        speaker.start()
        try:
            async with client.aio.live.connect(model=self.model, config=self._config(prompt)) as session:
                capture.start(on_block)
                if dial is not None and not await asyncio.to_thread(dial):
                    result.ended_by = "dial_failed"
                    return
                # The clocks run from the dial, not from connecting.
                clock["started"] = time.monotonic()

                async def send_audio():
                    from google.genai import types

                    while not finished.is_set():
                        pcm = await audio_in.get()
                        await session.send_realtime_input(
                            audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={INPUT_RATE}")
                        )

                async def receive():
                    # Keeps reading after end_call: the goodbye's audio can
                    # still be in flight when the tool call lands, and the
                    # drain below waits for it. Cancelled with the others.
                    while True:
                        async for msg in session.receive():
                            await self._handle(msg, session, speaker, add_line, finish, result)
                            if finished.is_set() and result.ended_by != "end_call":
                                return
                        await asyncio.sleep(0)

                async def watch():
                    while not finished.is_set():
                        await asyncio.sleep(_WATCH_INTERVAL_S)
                        elapsed = time.monotonic() - clock["started"]
                        if stop.is_set():
                            finish("stopped")
                        elif elapsed > max_call_s:
                            finish("max_duration")
                        elif not result.heard_them and elapsed > no_answer_s:
                            finish("no_answer")
                        elif still_connected is not None:
                            connected = await asyncio.to_thread(still_connected)
                            them_at = clock.get("them_at")
                            heard_recently = them_at is not None and time.monotonic() - them_at < _LIVE_IF_HEARD_S
                            if connected is False and heard_recently:
                                # 2026-10-08: Phone Link's End button vanished
                                # 20s into a live call. Someone talking means
                                # the line is up, whatever the screen says.
                                logger.info("Call agent: Phone Link says ended, but they spoke %.0fs ago; staying on",
                                            time.monotonic() - them_at)
                            elif connected is False:
                                finish("hung_up")

                async def opening_nudge():
                    # See _NUDGE_AFTER_S.
                    nudges = 0
                    while not finished.is_set() and nudges < _NUDGE_MAX:
                        await asyncio.sleep(0.25)
                        if any(l.who == "california" and l.text.strip() for l in result.lines):
                            return
                        them_at = clock.get("them_at")
                        if them_at is None or time.monotonic() - them_at < _NUDGE_AFTER_S:
                            continue
                        nudges += 1
                        logger.info("Call agent: they spoke and she has not; nudge %d", nudges)
                        try:
                            await session.send_client_content(
                                turns={"role": "user", "parts": [{"text": _NUDGE_TEXT}]}, turn_complete=True
                            )
                        except Exception as exc:  # a failed nudge must never end the call
                            logger.warning("Call agent: nudge failed (%s)", exc)
                            return
                        clock["them_at"] = time.monotonic()  # give her the full wait again

                tasks = [asyncio.create_task(t()) for t in (send_audio, receive, watch, opening_nudge)]

                def task_died(task: asyncio.Task) -> None:
                    # A dropped Live session kills receive/send with an
                    # exception; without this the call stays open in silence
                    # until max_call_s.
                    if not task.cancelled() and task.exception() is not None and not finished.is_set():
                        logger.warning("Call agent task failed: %r", task.exception())
                        result.error = result.error or str(task.exception())[:300]
                        finish("error")

                for task in tasks:
                    task.add_done_callback(task_died)
                await finished.wait()
                if result.ended_by == "end_call":
                    # A short floor first: the goodbye may not have reached the
                    # speaker yet when end_call arrives, and idle() would be True.
                    await asyncio.sleep(_GOODBYE_GRACE_S)
                    deadline = time.monotonic() + _GOODBYE_DRAIN_S
                    while not speaker.idle() and time.monotonic() < deadline:
                        await asyncio.sleep(0.1)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            capture.stop()
            speaker.stop()

    async def _handle(self, msg, session, speaker, add_line, finish, result: CallResult) -> None:
        content = getattr(msg, "server_content", None)
        if content is not None:
            if getattr(content, "interrupted", False):
                speaker.flush()
            turn = getattr(content, "model_turn", None)
            for part in (getattr(turn, "parts", None) or []):
                data = getattr(getattr(part, "inline_data", None), "data", None)
                if data:
                    speaker.write(data)
            heard = getattr(content, "input_transcription", None)
            if heard is not None:
                add_line("them", getattr(heard, "text", "") or "")
            said = getattr(content, "output_transcription", None)
            if said is not None:
                add_line("california", getattr(said, "text", "") or "")

        tool_call = getattr(msg, "tool_call", None)
        calls = getattr(tool_call, "function_calls", None) or []
        if not calls:
            return
        responses = []
        end = False
        for call in calls:
            if call.name == "end_call":
                too_early = _hang_up_too_early(result)
                if too_early:
                    logger.info("Call agent: end_call refused, %s", too_early)
                    responses.append(_reply(call, {"error": too_early}, _SILENT))
                    continue
                end = True
                responses.append(_reply(call, {"ok": True}, _SILENT))
            else:
                responses.append(_reply(call, {"error": "unknown tool"}, _SILENT))
        await session.send_tool_response(function_responses=responses)
        if end:
            finish("end_call")
