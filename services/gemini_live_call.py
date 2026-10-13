"""
The call agent: one Gemini Live session for the length of one phone call.

Audio path, proven live on 2026-10-13 (services/phone_call_audio.py):

  callee --(phone call downlink, intercepted on the phone)--16 kHz PCM--> Gemini Live
  Gemini Live --24 kHz PCM--> (injected into the call uplink on the phone) --> callee

The downlink carries ONLY the callee and her voice only goes up the line, so
it is echo-free and Gemini's own voice-activity detection can tell exactly
when they are talking. (Until 2026-10-13 the path was Phone Link + a WASAPI
loopback + VB-CABLE; that code lives in deprecated/services/.) That is also why barge-in
needs no work here -- when they talk over her, Gemini sends `interrupted` and
the queued audio is dropped.

The session ends on the first of: the agent calls `end_call`, the callee hangs
up (the phone's call state goes idle), nobody speaks within `no_answer_s`,
`max_call_s` runs out, or the service asks it to stop.

`google.genai` is imported lazily, inside the client factory, so importing
this module never needs it -- the unit tests drive it with a fake
session and fake audio.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

INPUT_RATE = 16000
OUTPUT_RATE = 24000
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
# Loudest loopback block below this (0-1 RMS) means she heard nothing at all.
_SILENT_PEAK = 0.002
_NUDGE_MAX = 2
# With a transport that knows the moment of the answer (the phone's own call
# state, tools/phone_adb_call.py), she is nudged this long after it instead of
# waiting to hear them and then _NUDGE_AFTER_S more. Live 2026-10-13: with only
# the heard-them clock, Marta said "Hola", the model reached for end_call three
# times, and she hung up on 9 s of silence.
_ANSWERED_GRACE_S = 0.8
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
    at: float | None = None  # seconds after the dial when this line began


def _clock(seconds: float | None) -> str:
    """12.4 -> "0:12"."""
    if seconds is None:
        return "?:??"
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass
class CallResult:
    lines: list[CallLine] = field(default_factory=list)
    outcome: dict | None = None
    ended_by: str = ""  # end_call | hung_up | no_answer | max_duration | stopped | error | dial_failed
    duration_s: float = 0.0
    error: str = ""
    # What the session saw, for the call log and the report. Every key is
    # optional: a stage that did not happen is absent, never zero.
    #   connect_s        opening the Live session
    #   dial_s           the phone dialling (placeCall until the call shows in its state)
    #   talk_window_s    from the end of the dial to the end of the call
    #   answered_at_s    first words heard from them, after the dial
    #   first_spoke_at_s her first words, after the dial
    #   interruptions    times they talked over her (her audio dropped)
    #   nudges           opening nudges sent (see _NUDGE_AFTER_S)
    #   refused_hang_ups end_call refused as too early
    #   unknown_tools    tool calls the agent does not have
    #   loopback_source / loopback_peak / heard_nothing   her ears
    stats: dict = field(default_factory=dict)

    @property
    def heard_them(self) -> bool:
        return any(line.who == "them" and line.text.strip() for line in self.lines)

    def transcript(self, max_chars: int = 4000, times: bool = False) -> str:
        rows = [
            (f"[{_clock(l.at)}] " if times and l.at is not None else "")
            + f"{'Them' if l.who == 'them' else 'California'}: {l.text.strip()}"
            for l in self.lines if l.text.strip()
        ]
        text = "\n".join(rows)
        return text if len(text) <= max_chars else "...\n" + text[-max_chars:]

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1


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
        *,
        capture_factory: Callable,
        speaker_factory: Callable,
        client_factory: Callable | None = None,
        on_line: Callable | None = None,
        live_if_heard_s: float | None = None,
    ):
        self.client_kwargs = dict(client_kwargs)
        # Called with (index, who, text, at) as each piece of transcript
        # arrives, for the live UI. Never allowed to break the call.
        self.on_line = on_line
        self.model = model
        self.voice = voice
        self.language_code = language_code
        self.silence_ms = silence_ms
        self.client_factory = client_factory or _default_client_factory
        # Her ears and mouth: PhoneCallAudio's capture() and speaker() faces.
        self.capture_factory = capture_factory
        self.speaker_factory = speaker_factory
        # How long a "they hung up" reading is doubted after they last spoke.
        # Phone Link's End button vanished mid-call; the phone's own call state
        # does not, so PhoneService passes 0 and a hang-up lands at once.
        self.live_if_heard_s = _LIVE_IF_HEARD_S if live_if_heard_s is None else float(live_if_heard_s)

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
        answered: Callable[[], bool] | None = None,
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

        `answered` is True once the transport knows the call was picked up;
        she is then nudged to open after _ANSWERED_GRACE_S rather than after
        hearing them. A transport that cannot tell passes nothing.
        """
        result = CallResult()
        started = time.monotonic()
        try:
            asyncio.run(self._run(prompt, max_call_s, no_answer_s, still_connected, stop or threading.Event(), dial, result, mute, answered))
        except Exception as exc:  # the call must always come back with a result
            logger.exception("Call agent failed")
            result.ended_by = result.ended_by or "error"
            result.error = str(exc)[:300]
        result.duration_s = round(time.monotonic() - started, 1)
        return result

    async def _run(self, prompt, max_call_s, no_answer_s, still_connected, stop, dial, result: CallResult, mute=None, answered=None) -> None:
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

        def _tell(index: int, who: str, text: str, at: float) -> None:
            if self.on_line is None:
                return
            try:
                self.on_line(index, who, text, at)
            except Exception:
                pass

        def add_line(who: str, text: str) -> None:
            if not text:
                return
            if who == "them":
                clock["them_at"] = time.monotonic()
            if result.lines and result.lines[-1].who == who:
                result.lines[-1].text += text
                _tell(len(result.lines) - 1, who, text, result.lines[-1].at)
                return
            at = round(max(0.0, time.monotonic() - clock["started"]), 1)
            result.lines.append(CallLine(who, text, at))
            _tell(len(result.lines) - 1, who, text, at)
            if text.strip():
                key = "answered_at_s" if who == "them" else "first_spoke_at_s"
                result.stats.setdefault(key, at)

        client = self.client_factory(self.client_kwargs)
        speaker = self.speaker_factory()
        capture = self.capture_factory()
        speaker.start()
        connecting = time.monotonic()
        try:
            async with client.aio.live.connect(model=self.model, config=self._config(prompt)) as session:
                result.stats["connect_s"] = round(time.monotonic() - connecting, 1)
                capture.start(on_block)
                dialling = time.monotonic()
                if dial is not None and not await asyncio.to_thread(dial):
                    result.stats["dial_s"] = round(time.monotonic() - dialling, 1)
                    result.ended_by = "dial_failed"
                    return
                if dial is not None:
                    result.stats["dial_s"] = round(time.monotonic() - dialling, 1)
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
                            heard_recently = them_at is not None and time.monotonic() - them_at < self.live_if_heard_s
                            if connected is False and heard_recently:
                                # 2026-10-08: Phone Link's End button vanished
                                # 20s into a live call. Someone talking means
                                # the line is up, whatever the screen says.
                                logger.info("Call agent: the line reads ended, but they spoke %.0fs ago; staying on",
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
                        if answered is not None and nudges == 0 and "answered_at" not in clock:
                            try:
                                if await asyncio.to_thread(answered):
                                    clock["answered_at"] = time.monotonic()
                            except Exception:
                                pass
                        answered_at = clock.get("answered_at")
                        due_answer = nudges == 0 and answered_at is not None and time.monotonic() - answered_at >= _ANSWERED_GRACE_S
                        due_heard = them_at is not None and time.monotonic() - them_at >= _NUDGE_AFTER_S
                        if not (due_answer or due_heard):
                            continue
                        nudges += 1
                        result._count("nudges")
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
                result.stats["talk_window_s"] = round(time.monotonic() - clock["started"], 1)
        finally:
            capture.stop()
            speaker.stop()
            peak = getattr(capture, "peak", None)
            if isinstance(peak, (int, float)) and getattr(capture, "blocks", 0):
                result.stats["loopback_peak"] = round(float(peak), 4)
                result.stats["heard_nothing"] = peak < _SILENT_PEAK
                result.stats["loopback_source"] = str(getattr(capture, "source", "") or "")

    async def _handle(self, msg, session, speaker, add_line, finish, result: CallResult) -> None:
        content = getattr(msg, "server_content", None)
        if content is not None:
            if getattr(content, "interrupted", False):
                speaker.flush()
                result._count("interruptions")
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
                    result._count("refused_hang_ups")
                    responses.append(_reply(call, {"error": too_early}, _SILENT))
                    continue
                end = True
                responses.append(_reply(call, {"ok": True}, _SILENT))
            else:
                result._count("unknown_tools")
                responses.append(_reply(call, {"error": "unknown tool"}, _SILENT))
        await session.send_tool_response(function_responses=responses)
        if end:
            finish("end_call")
