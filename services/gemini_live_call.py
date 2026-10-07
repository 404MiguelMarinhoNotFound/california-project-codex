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

TOOL_DECLARATIONS = [
    {
        "name": "record_outcome",
        "description": (
            "Record what this call achieved. Call it once, before saying goodbye, "
            "with what was actually agreed or learned."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "status": {"type": "STRING", "enum": OUTCOME_STATUSES},
                "details": {
                    "type": "STRING",
                    "description": "The facts: day, time, number of people, name, price, the answer to the question.",
                },
                "summary": {
                    "type": "STRING",
                    "description": "One sentence in English for Miguel saying how the call went.",
                },
            },
            "required": ["status", "summary"],
        },
    },
    {
        "name": "end_call",
        "description": "Hang up. Call it right after your goodbye.",
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


# Outcomes that legitimately happen with nobody replying to her.
_UNANSWERED_STATUSES = {"voicemail", "no_answer", "failed"}


def _outcome_too_early(result: "CallResult", status: str) -> str:
    """
    Why record_outcome must be refused right now, or "" if it may stand.

    Found live 2026-10-07 on Vertex: given the callee's "Estou?", the model
    called record_outcome AND end_call 0.75s in, before saying a word, and
    only then started its greeting. On a real call that hangs up on the
    restaurant. The prompt already says otherwise; this is the guarantee. A
    refused call goes back to the model as an error, so it carries on talking.
    """
    she_spoke = any(l.who == "california" and l.text.strip() for l in result.lines)
    if not she_spoke:
        return "Too early: you have not spoken yet. Greet them and have the conversation first."
    if status in _UNANSWERED_STATUSES:
        return ""
    first = next(i for i, l in enumerate(result.lines) if l.who == "california" and l.text.strip())
    replied = any(l.who == "them" and l.text.strip() for l in result.lines[first + 1:])
    if not replied:
        return "Too early: they have not answered you yet. Wait for their reply."
    return ""


# ----------------------------------------------------------------- audio I/O


class LoopbackCapture:
    """The callee, as the default speaker plays them. 16 kHz mono int16 blocks."""

    def __init__(self, samplerate: int = INPUT_RATE, block_ms: int = _BLOCK_MS):
        self.samplerate = samplerate
        self.frames = int(samplerate * block_ms / 1000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_block: Callable[[bytes], None]) -> None:
        def loop():
            import soundcard as sc

            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            with mic.recorder(samplerate=self.samplerate, channels=1) as rec:
                while not self._stop.is_set():
                    block = rec.record(numframes=self.frames)[:, 0]
                    pcm = np.clip(block * 32767.0, -32768, 32767).astype("<i2").tobytes()
                    on_block(pcm)

        self._thread = threading.Thread(target=loop, name="call-loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


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
        self.capture_factory = capture_factory or LoopbackCapture
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
    ) -> CallResult:
        """
        Blocking: one call, start to finish. Never raises.

        `dial` runs once the session is connected and the loopback is
        listening, so a callee who answers on the first ring is not talking
        into nothing. It returns False when the call did not go out.
        """
        result = CallResult()
        started = time.monotonic()
        try:
            asyncio.run(self._run(prompt, max_call_s, no_answer_s, still_connected, stop or threading.Event(), dial, result))
        except Exception as exc:  # the call must always come back with a result
            logger.exception("Call agent failed")
            result.ended_by = result.ended_by or "error"
            result.error = str(exc)[:300]
        result.duration_s = round(time.monotonic() - started, 1)
        return result

    async def _run(self, prompt, max_call_s, no_answer_s, still_connected, stop, dial, result: CallResult) -> None:
        loop = asyncio.get_running_loop()
        audio_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        finished = asyncio.Event()
        clock = {"started": time.monotonic()}

        def finish(reason: str) -> None:
            if not result.ended_by:
                result.ended_by = reason
            finished.set()

        def on_block(pcm: bytes) -> None:
            def put():
                if audio_in.full():
                    audio_in.get_nowait()  # stale audio is worthless; keep it live
                audio_in.put_nowait(pcm)

            loop.call_soon_threadsafe(put)

        def add_line(who: str, text: str) -> None:
            if not text:
                return
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
                            if connected is False:
                                finish("hung_up")

                tasks = [asyncio.create_task(t()) for t in (send_audio, receive, watch)]

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
            args = dict(getattr(call, "args", None) or {})
            if call.name == "record_outcome":
                status = str(args.get("status") or "")
                status = status if status in OUTCOME_STATUSES else "failed"
                too_early = _outcome_too_early(result, status)
                if too_early:
                    logger.info("Call agent: record_outcome(%s) refused, %s", status, too_early)
                    responses.append({"id": call.id, "name": call.name, "response": {"error": too_early}})
                    continue
                result.outcome = {
                    "status": status,
                    "details": str(args.get("details") or ""),
                    "summary": str(args.get("summary") or ""),
                }
                responses.append({"id": call.id, "name": call.name, "response": {"ok": True}})
            elif call.name == "end_call":
                if result.outcome is None:
                    logger.info("Call agent: end_call refused, no outcome recorded")
                    responses.append({"id": call.id, "name": call.name, "response": {
                        "error": "Not yet: the call has no outcome. Keep talking to them; call "
                                 "record_outcome once something is agreed or learned, then end_call."}})
                    continue
                end = True
                responses.append({"id": call.id, "name": call.name, "response": {"ok": True}})
            else:
                responses.append({"id": call.id, "name": call.name, "response": {"error": "unknown tool"}})
        await session.send_tool_response(function_responses=responses)
        if end:
            finish("end_call")
