"""
PhoneService: California places a phone call from Master Miguel's own number
and holds the conversation herself, then reports back.

    control_phone(phone_call, brief) -> read back once -> "go" -> background call:
        Gemini Live session up, the phone dials over ADB (services/phone_adb.py),
        call audio straight from the phone (services/phone_call_audio.py),
        conversation, hang up -> CallReport queued
    Orchestrator._idle_loop picks the report up and runs a turn on it, so the
    report lands in her conversation history and she tells him how it went.

Same contract as WhatsAppService and DeebotService: never raises at
construction, self-disables with a logged reason, returns PhoneCommandResult
(falsy on failure, so `is not None` for existence checks), one command in
flight on a daemon worker.

Every call is read back before it is placed, even to an exact contact. A call
to the wrong place is as unrecoverable as a message to the wrong person, and
the brief -- what she may agree to on his behalf -- is worth hearing once.
The confirmation is server-side: `confirm=True` only works against a pending
read-back of the SAME number and the SAME brief, exactly like WhatsApp's.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from services.gemini_live_call import CallResult
from services.phone_digest import (
    app_texts_line,
    digest_line,
    missed_calls_line,
    parse_call_log,
    parse_notifications,
)
from services.phone_prompts import CallBrief, build_call_prompt
from services.whatsapp_service import looks_like_phone, normalize_phone

if TYPE_CHECKING:
    from services.incoming_call import RingOffer

logger = logging.getLogger(__name__)

_MSG_DISABLED = "Phone calls aren't set up right now."
_MSG_BUSY = "I'm already on a call. Let me finish that one first."
_MSG_NO_NUMBER = (
    "{who} is not in his contacts. If it is a business (restaurant, clinic, shop), "
    "find its number with web search and call again with it as number. If it is a "
    "person, ask him for the number; never search for a private person's number."
)
_MSG_BLOCKED = "I won't call that number myself."
# Refusals while HIS call (one answered natively) holds the phone.
_MSG_HUMAN_DIAL = "You're on a call on your phone, so I won't dial over it."
_MSG_HUMAN_UNKNOWN = "I can't tell yet whether the call on your phone has ended, so I won't dial over it."
_MSG_HUMAN_FIND = "You're on a call on your phone, so I won't ring it."
_MSG_HUMAN_FIND_UNKNOWN = "I can't tell yet whether the call on your phone has ended, so I won't ring it."

# "Answer it on speakerphone": the phone answers its own ring and taps its own
# Speaker button. No Gemini, no call audio, no recording, no report.
_MSG_NATIVE_DISABLED = "Answering calls on your phone isn't set up right now."
_MSG_NATIVE_BUSY = "There's already another call on your phone. Answer this one on the phone."
_MSG_ALREADY_ANSWERED = "Looks like it's already been answered."
_MSG_RINGING_UNBOUND = "It's ringing, but I couldn't lock onto that call. Ask me again or answer it on the phone."
_MSG_PARTIAL = "The call is answered, but I couldn't switch it to speakerphone. Use the phone's speaker button."
_ANSWER_LINES = {
    "ANSWERED_SPEAKER": "Answered on your phone's speakerphone.",
    # Answered, the route not read back: he is on the call, on whatever route it is.
    "ANSWERED": _MSG_PARTIAL,
    "ANSWERED_ROUTE_FAILED": _MSG_PARTIAL,
    "NO_RING": "Your phone isn't ringing now.",
    "STALE_RING": "That call stopped ringing. I haven't answered the new one.",
    # They hung up as it was answered: there is no new call, so never the line above.
    "HUNG_UP": "They hung up as I answered.",
    "BUSY": _MSG_NATIVE_BUSY,
    "ANSWER_UNCERTAIN": "I couldn't confirm whether the call answered. Check your phone.",
    "UNREADABLE": "I couldn't read your phone's calls just now, so I didn't answer it. Answer it on the phone.",
    "NOT_ANSWERED": "Your phone didn't take the answer. Answer it on the phone.",
    "UNSUPPORTED": "Your phone won't let me answer calls. Answer it on the phone.",
    "NO_PHONE": "I can't reach your phone over Wi-Fi. Turn Wireless debugging back on in Developer options.",
    "LOCKED_DOWN": "Your phone is blocking me from answering. Turn on USB debugging (Security settings) "
                   "in Developer options.",
    "HELPER_MISMATCH": "The call helper on your phone isn't the one I installed, so I won't run it. "
                       "Something may have changed it.",
}
_ANSWERED = ("ANSWERED_SPEAKER", "ANSWERED", "ANSWERED_ROUTE_FAILED")

_SPEAKER_FAILED = "I couldn't switch the speaker. Use the phone's speaker button."
_MSG_NOT_CONNECTED = ("I can only switch the speaker once the call is connected and it's the only one. "
                      "Use the phone's speaker button.")
_SPEAKER_LINES = {
    "NO_PHONE": _ANSWER_LINES["NO_PHONE"],
    "LOCKED_DOWN": "Your phone is blocking me from switching the speaker. Turn on USB debugging "
                   "(Security settings) in Developer options.",
    "NO_CALL": "There's no call on your phone right now.",
    "RINGING": "Your phone is still ringing. Answer it first.",
    "UNREADABLE": "I couldn't read your phone's calls just now. Use the phone's speaker button.",
    # Only a genuine second live call is "another call". A sole call not
    # connected yet, or one that changed before the tap, is neither.
    "OTHER_CALL": _MSG_NATIVE_BUSY,
    "NOT_CONNECTED": _MSG_NOT_CONNECTED,
    "STALE_CALL": _MSG_NOT_CONNECTED,
}

# Find my phone, one line per way it can end. The fixes differ, so none is
# collapsed into "couldn't".
_FIND_LINES = {
    "PLAYING": "It's ringing on his phone now, a sound for about twenty seconds. Say 'again' if he needs longer.",
    "ALREADY_PLAYING": "His phone is already ringing from a moment ago. Tell him to listen for it.",
    "NO_PHONE": "I can't reach his phone over Wi-Fi, so I couldn't ring it. Wireless debugging may be off on it.",
    "LOCKED_DOWN": "His phone is blocking me, so I couldn't ring it. USB debugging (Security settings) needs to be on.",
    "RING_FAILED": "His phone didn't start the sound. Try again in a moment.",
}
_MSG_FIND_FAILED = "I couldn't ring the phone, it didn't answer."

# "What did I miss?" when the phone could not be read. Never "nothing new":
# an unreadable phone and a quiet one need opposite answers.
_READ_FAILURES = {
    "NO_PHONE": "I can't reach his phone over Wi-Fi, so I can't see what he missed. "
                "Wireless debugging may be off on it.",
    "": "His phone didn't answer just now, so I can't say what he missed.",
}

_DEFAULT_DIGEST_IGNORE = [
    "android", "com.android.systemui", "com.miui.securitycenter", "com.xiaomi.mirror",
    "com.xiaomi.aicr", "com.miui.misound", "com.microsoft.appmanager",
]

_DIALED = ("DIALED",)

# Why a dial did not go out, in words California can say. Each needs a
# different fix, so they are never collapsed into "couldn't call".
_DIAL_FAILURES = {
    "NO_PHONE": "I can't reach your phone over Wi-Fi. Turn Wireless debugging back on in Developer options.",
    "LOCKED_DOWN": "Your phone is blocking me from calling. Turn on USB debugging (Security settings) "
                   "in Developer options.",
    "NO_INTERCEPTION": "Your phone won't let me into the call audio, so I can't talk on it.",
    "HELPER_MISMATCH": "The call helper on your phone isn't the one I installed, so I won't run it. "
                       "Something may have changed it.",
    "NOT_PLACED": "I asked your phone to call but the call never started.",
    "BUSY": "You're already on a call on your phone, so I didn't dial over it.",
    "": "I couldn't reach your phone to place the call.",
}

# Portuguese premium-rate and adult-line prefixes: never dialled by her.
_DEFAULT_BLOCKED_PREFIXES = ["+351760", "+351761", "+351762", "+351707", "+351708", "+351646", "+351648"]


@dataclass
class PhoneCommandResult:
    success: bool
    message: str = ""
    # A read-back or "which one?": nothing dialled, waiting for his answer.
    awaiting: bool = False
    # Answering on speakerphone only: the phone accepted the ring (the same
    # call read back ACTIVE), and separately whether its speaker route was read
    # back. A partial answer is answered=True, speaker_verified=False.
    answered: bool = False
    speaker_verified: bool = False

    def __bool__(self) -> bool:
        return self.success


@dataclass(frozen=True)
class IncomingTarget:
    """The ring a command is about: the watcher's episode and telecom's own key
    for that call, read together once at the start of the turn and never
    re-discovered afterwards."""

    offer: "RingOffer"
    call_key: str


# How a call ended, in words for the report.
_ENDED_HOW = {
    "end_call": "California said goodbye and hung up",
    "hung_up": "they hung up",
    "no_answer": "nobody answered",
    "max_duration": "the time limit ended it",
    "stopped": "it was stopped (California shut down)",
    "error": "it failed",
    "dial_failed": "it never went out",
}


@dataclass
class CallReport:
    label: str
    number: str
    brief: CallBrief
    result: CallResult
    started_at: str = ""
    # Everything about the call that is not the conversation itself, so the
    # log and the spoken report can say how it went, not just what was said.
    call_id: str = ""
    ended_at: str = ""
    number_source: str = ""  # "from your contacts" | "the number I found" | "the number you said"
    dial_status: str = ""  # the phone's answer to the dial (DIALED, NO_PHONE, LOCKED_DOWN...)
    hang_up_status: str = ""  # the phone's answer to the hang-up (ENDED, NO_CALL...)
    backend: str = ""
    model: str = ""
    voice: str = ""

    def metadata(self) -> dict:
        """The numbers that describe the call, derived once for the log and the report."""
        r, stats = self.result, self.result.stats
        hers = [l.text.strip() for l in r.lines if l.who == "california" and l.text.strip()]
        theirs = [l.text.strip() for l in r.lines if l.who == "them" and l.text.strip()]
        answered = stats.get("answered_at_s")
        window = stats.get("talk_window_s")
        meta = {
            "call_id": self.call_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total_s": r.duration_s,
            "ring_s": answered,  # dial -> first words heard (None: nobody spoke)
            "talk_s": round(window - answered, 1) if answered is not None and window is not None else None,
            "she_first_spoke_s": stats.get("first_spoke_at_s"),
            "ended_by": r.ended_by,
            "ended_how": _ENDED_HOW.get(r.ended_by, r.ended_by or "unknown"),
            "status": self.status,
            "her_turns": len(hers),
            "their_turns": len(theirs),
            "her_words": sum(len(t.split()) for t in hers),
            "their_words": sum(len(t.split()) for t in theirs),
            "number_source": self.number_source,
            "dial_status": self.dial_status,
            "hang_up_status": self.hang_up_status,
            "backend": self.backend,
            "model": self.model,
            "voice": self.voice,
        }
        for key in ("connect_s", "dial_s", "interruptions", "nudges", "refused_hang_ups",
                    "unknown_tools", "loopback_peak", "heard_nothing", "loopback_source"):
            if key in stats:
                meta[key] = stats[key]
        return meta

    @property
    def status(self) -> str:
        if self.result.outcome and self.result.outcome.get("status"):
            return self.result.outcome["status"]
        if self.result.ended_by == "dial_failed":
            return "dial_failed"
        if self.result.ended_by == "error" and not self.result.heard_them:
            # Failed before anyone spoke (backend, mic switch): never "no answer".
            return "failed"
        if self.result.ended_by == "no_answer" or not self.result.heard_them:
            return "no_answer"
        return "unknown"


def _user_adc_path() -> str:
    """Where `gcloud auth application-default login` saves its credentials."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "")
        return os.path.join(base, "gcloud", "application_default_credentials.json") if base else ""
    return os.path.join(os.path.expanduser("~"), ".config", "gcloud", "application_default_credentials.json")


def call_record(report: CallReport, include_transcript: bool = True) -> dict:
    """One call as the log stores it (logs/calls.jsonl); also what phone_log reads back."""
    record = {
        "ts": report.started_at,  # kept first and under its old name: older lines use it
        "to": report.label,
        "number": report.number,
        "kind": report.brief.normalized_kind(),
        "goal": report.brief.goal,
        "brief": {
            "details": report.brief.details,
            "may_agree": report.brief.may_agree,
            "must_not": report.brief.must_not,
        },
        "status": report.status,
        "ended_by": report.result.ended_by,
        "duration_s": report.result.duration_s,
        "outcome": report.result.outcome,
        "error": report.result.error,
        "meta": report.metadata(),
    }
    if include_transcript:
        record["transcript"] = [
            {"who": l.who, "at": l.at, "text": l.text.strip()} for l in report.result.lines if l.text.strip()
        ]
    return record


def short_duration(seconds) -> str:
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def call_log_line(record: dict) -> str:
    """One spoken-friendly line for a logged call. Works on old log lines too (no "meta")."""
    meta = record.get("meta") or {}
    when = str(record.get("ts") or "")
    try:
        when = datetime.fromisoformat(when).strftime("%d %b %H:%M")
    except ValueError:
        pass
    status = str(record.get("status") or "unknown").replace("_", " ")
    line = f"{when}, {record.get('to') or record.get('number')}: {status}, {short_duration(record.get('duration_s'))} in all"
    if meta.get("talk_s") is not None:
        line += f" (talked {short_duration(meta['talk_s'])})"
    summary = (record.get("outcome") or {}).get("summary")
    if summary:
        line += f". {summary}"
    elif record.get("error"):
        line += f". {record['error']}"
    return line.rstrip(".") + "."


def _is_human(active: dict) -> bool:
    """A call he answered on the phone (native), as opposed to one of hers."""
    return active.get("owner") == "human"


def _live_legs(topology) -> list:
    """The legs still calls: a DISCONNECTED row can linger a moment after a hang-up."""
    return [leg for leg in topology.legs if leg.state != "ended"]


def _rings_alone(topology) -> bool:
    """Exactly one live call, and it is ringing."""
    live = _live_legs(topology)
    return len(live) == 1 and live[0].state == "ringing"


def _sole_active_call(topology) -> tuple[str, str]:
    """("OK", key) for exactly one live call and it active, else (why, "")."""
    if topology is None:
        return "UNREADABLE", ""
    live = _live_legs(topology)
    if any(leg.state == "unknown" for leg in live):
        return "UNREADABLE", ""
    if not live:
        return "NO_CALL", ""
    if len(live) > 1:
        return "OTHER_CALL", ""
    if live[0].state == "ringing":
        return "RINGING", ""
    if live[0].state != "active":
        return "NOT_CONNECTED", ""   # dialing, holding, answered
    return "OK", live[0].key


def _spoken_number(number: str) -> str:
    """+351961136786 -> '961 136 786' (local) for the read-back."""
    digits = number.lstrip("+")
    if digits.startswith("351") and len(digits) == 12:
        local = digits[3:]
        return f"{local[:3]} {local[3:6]} {local[6:]}"
    return number


class PhoneService:
    def __init__(
        self,
        config: dict,
        resolve_contact: Callable | None = None,
        dialer=None,
        agent_factory: Callable | None = None,
        summarize: Callable | None = None,
        publish: Callable | None = None,
        read_phone=None,
        control_phone=None,
        current_ring: Callable[[], "RingOffer | None"] | None = None,
    ):
        cfg = config.get("phone", {}) or {}
        # The live UI's EventBus.publish, or None. Call lines go out as they
        # are heard; with logging.include_transcripts off, only the call's
        # state does.
        self._publish_event = publish
        self._publish_text = ((config.get("logging") or {}).get("include_transcripts", True)) is not False
        self.owner = str(cfg.get("owner_name") or "Miguel")
        self.callback_number = str(cfg.get("callback_number") or "")
        # Which Gemini endpoint runs the call. "vertex" bills the Google Cloud
        # project (and its credits); "ai_studio" bills the AI Studio prepayment,
        # a separate balance.
        self.backend = str(cfg.get("backend") or "vertex").strip().lower()
        vertex_cfg = cfg.get("vertex", {}) or {}
        self.vertex_project = str(vertex_cfg.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")).strip()
        self.vertex_location = str(vertex_cfg.get("location") or "us-central1").strip()
        if self.backend == "vertex":
            self.model = str(vertex_cfg.get("model") or "gemini-live-2.5-flash-native-audio")
        else:
            self.model = str(cfg.get("model") or "gemini-2.5-flash-native-audio-latest")
        self.voice = str(cfg.get("voice") or "Aoede")
        self.language_code = str(cfg.get("language_code") or "")
        self.max_call_s = float(cfg.get("max_call_s") or 300)
        self.no_answer_s = float(cfg.get("no_answer_s") or 45)
        self.silence_ms = int(cfg.get("silence_duration_ms") or 700)
        self.confirm_timeout_s = max(10.0, float(cfg.get("confirm_timeout_ms") or 120000) / 1000.0)
        # The phone over Wireless debugging. No serial: found by mDNS or its
        # attached IP:port (services/phone_adb.py), never the Mi Box.
        adb_cfg = cfg.get("adb", {}) or {}
        self.adb_path = str(adb_cfg.get("path") or "adb")
        self.adb_serial = str(adb_cfg.get("serial") or "")
        self.adb_port = int(adb_cfg.get("port") or 27183)
        # The phone's own mic stays out of the call, or the room goes in under her voice.
        self.mute_phone_mic = adb_cfg.get("mute_phone_mic", True) is not False
        # "What did I miss?": read-only, see missed_calls() and digest().
        digest_cfg = cfg.get("digest", {}) or {}
        self.digest_window_hours = int(digest_cfg.get("missed_window_hours") or 24)
        self.digest_max_apps = int(digest_cfg.get("max_apps") or 8)
        self.digest_max_senders = int(digest_cfg.get("max_senders_per_app") or 5)
        ignore = digest_cfg.get("ignore_packages")
        self.digest_ignore_packages = frozenset(
            str(p) for p in (_DEFAULT_DIGEST_IGNORE if ignore is None else ignore)
        )
        self.digest_app_names = {str(k): str(v) for k, v in (digest_cfg.get("app_names") or {}).items()}
        self.default_country = str(cfg.get("default_country") or "351")
        self.allowed_country_codes = [str(c) for c in (cfg.get("allowed_country_codes") or [self.default_country])]
        self.blocked_prefixes = [str(p) for p in (cfg.get("blocked_prefixes") or _DEFAULT_BLOCKED_PREFIXES)]
        log_cfg = config.get("logging", {}) or {}
        self.log_path = str(cfg.get("log_path") or os.path.join(log_cfg.get("dir", "logs"), "calls.jsonl"))
        self.include_transcripts = log_cfg.get("include_transcripts", True) is not False

        self.resolve_contact = resolve_contact
        self._dialer = dialer
        self._read_phone = read_phone
        self._read_lock = threading.Lock()
        self._agent_factory = agent_factory
        # What the call achieved is read from the transcript after it ends
        # (services/call_outcome.py), with the same Claude model the room
        # uses. Only for the real agent: tests that inject one never reach
        # the network unless they inject a summarizer too.
        self._summarize = summarize
        self._outcome_model = str(((config.get("llm", {}) or {}).get("claude", {}) or {}).get("model") or "")

        self._lock = threading.Lock()
        self._pending: tuple[str, str, float] | None = None
        self._active: dict | None = None
        self._last: CallReport | None = None
        self._reports: queue.Queue[CallReport] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Native answering ("answer it on speakerphone", "speaker off") runs on
        # its own transport: never the outgoing call's, the digest's or the
        # ring watcher's, whose cached serials are written without a lock.
        # `current_ring` is the watcher's current(), for the episode checks.
        self._control_phone = control_phone
        self._control_lock = threading.Lock()  # one adb conversation at a time on it; never _lock
        self._current_ring = current_ring
        self._native_poll_s = 2.0
        self._native_stop = threading.Event()
        self._monitor: threading.Thread | None = None

        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client_kwargs: dict = {}
        configured = bool(cfg.get("enabled", False))
        self.enabled = configured and self._probe()
        incoming_cfg = cfg.get("incoming", {}) or {}
        # The ring watcher is the only trusted source of a ring to answer: with
        # it off there is never a target, so the feature is off, not half on.
        self.native_answer_enabled = (
            incoming_cfg.get("answer_speaker_enabled", False) is True
            and incoming_cfg.get("enabled", False) is True
            and self._probe_native()
        )

    # ------------------------------------------------------------- setup

    def _probe(self) -> bool:
        """Never raises. Each missing piece logs its own fix."""
        if self._dialer is not None and self._agent_factory is not None:
            return True  # injected (tests)
        if not (shutil.which(self.adb_path) or os.path.isfile(self.adb_path)):
            logger.warning("Phone calls disabled: adb not found at %r (phone.adb.path)", self.adb_path)
            return False
        kwargs, why = self._client_kwargs()
        if kwargs is None:
            logger.warning("Phone calls disabled: %s", why)
            return False
        self.client_kwargs = kwargs
        # find_spec, not an import: google.genai is heavy, and boot should not
        # pay for it when no call is ever made.
        for module in ("google.genai",):
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                logger.warning(
                    "Phone calls disabled: %s is missing. Install it with: uv sync --extra default", module
                )
                return False
        return True

    def _probe_native(self) -> bool:
        """Windows and adb only: no Gemini credentials, no google.genai. Never raises."""
        if self._control_phone is not None:
            return True  # injected (tests)
        if sys.platform != "win32":
            logger.warning("Answering on the phone disabled: Windows only")
            return False
        if not (shutil.which(self.adb_path) or os.path.isfile(self.adb_path)):
            logger.warning("Answering on the phone disabled: adb not found at %r (phone.adb.path)", self.adb_path)
            return False
        return True

    def _client_kwargs(self) -> tuple[dict | None, str]:
        """
        How to reach Gemini Live for the configured backend, or why we can't.

        Vertex prefers a service account (GOOGLE_APPLICATION_CREDENTIALS, the
        standard ADC variable) with a project id, and falls back to an API key
        in Vertex express mode. An AI Studio key only reaches AI Studio.
        """
        if self.backend == "ai_studio":
            if not self.api_key:
                return None, "GEMINI_API_KEY is not set in .env"
            return {"api_key": self.api_key}, ""
        if self.backend != "vertex":
            return None, f"unknown phone.backend {self.backend!r} (use vertex or ai_studio)"
        credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if credentials and not os.path.isfile(credentials):
            return None, f"GOOGLE_APPLICATION_CREDENTIALS points at a missing file: {credentials}"
        # Application Default Credentials: an explicit key file, or the user
        # login `gcloud auth application-default login` saves. The login is
        # the route on new projects, whose organisation blocks key files by
        # default (iam.disableServiceAccountKeyCreation, "secure by default").
        if credentials or os.path.isfile(_user_adc_path()):
            if not self.vertex_project:
                return None, "set phone.vertex.project in config.yaml (the Google Cloud project id)"
            return {"vertexai": True, "project": self.vertex_project, "location": self.vertex_location}, ""
        # Only an explicit Vertex key. The AI Studio key is restricted to the
        # Gemini API and fails on Vertex with "Invalid resource field value".
        express_key = os.environ.get("VERTEX_API_KEY", "").strip()
        if express_key:
            return {"vertexai": True, "api_key": express_key}, ""
        return None, (
            "no Vertex credentials: run `gcloud auth application-default login`, or set "
            "GOOGLE_APPLICATION_CREDENTIALS (a key file) or VERTEX_API_KEY in .env"
        )

    def _get_dialer(self):
        if self._dialer is None:
            from services.phone_adb import AdbPhone

            self._dialer = AdbPhone.from_config({"phone": {"adb": {
                "path": self.adb_path, "serial": self.adb_serial, "port": self.adb_port,
                "mute_phone_mic": self.mute_phone_mic,
            }}})
        return self._dialer

    def _get_read_phone(self):
        """A separate transport; callers hold _read_lock through discovery and reads."""
        if self._read_phone is None:
            from services.phone_adb import AdbPhone

            self._read_phone = AdbPhone.from_config({"phone": {"adb": {
                "path": self.adb_path, "serial": self.adb_serial, "port": self.adb_port,
                "mute_phone_mic": self.mute_phone_mic,
            }}})
        return self._read_phone

    def _get_control_phone(self):
        """PhoneService's own transport for answering and the speaker; callers hold _control_lock."""
        if self._control_phone is None:
            from services.phone_adb import AdbPhone

            self._control_phone = AdbPhone.from_config({"phone": {"adb": {
                "path": self.adb_path, "serial": self.adb_serial, "port": self.adb_port,
                "mute_phone_mic": self.mute_phone_mic,
            }}})
        return self._control_phone

    def _make_agent(self, audio=None):
        if self._agent_factory is not None:
            return self._agent_factory()
        from services.gemini_live_call import GeminiLiveAgent

        return GeminiLiveAgent(
            client_kwargs=self.client_kwargs,
            model=self.model,
            voice=self.voice,
            language_code=self.language_code,
            silence_ms=self.silence_ms,
            capture_factory=audio.capture,
            speaker_factory=audio.speaker,
            on_line=self._on_call_line,
            live_if_heard_s=0,
        )

    def _publish(self, kind: str, data: dict) -> None:
        if self._publish_event is None:
            return
        try:
            self._publish_event(kind, data)
        except Exception:
            pass

    def _on_call_line(self, index: int, who: str, text: str, at: float) -> None:
        if self._publish_text:
            self._publish("call_line", {"index": index, "who": who, "text": text, "at": at})

    # ------------------------------------------------------------- numbers

    def _resolve(self, brief: CallBrief, number: str) -> tuple[str, str, str, PhoneCommandResult | None]:
        """
        (number, label, source, error). Exactly one of number or error is set.

        The contact book is asked first, even when the brief carries a number:
        a number Claude passes for a PERSON is a number it may have made up,
        while the book is what his phone actually holds. A certain contact
        match therefore wins over a passed number. A passed number wins over a
        loose or tied match, because "Taberna da Praia" brushing against some
        contact's surname must not redirect a call meant for the restaurant.
        `source` is spoken in the read-back so he hears where the number came from.
        """
        who = (brief.to or "").strip()
        if looks_like_phone(who):
            normalized = normalize_phone(who, self.default_country)
            if not normalized:
                return "", "", "", PhoneCommandResult(False, _MSG_BLOCKED)
            return normalized, _spoken_number(normalized), "the number you said", None
        if not who and not (number or "").strip():
            return "", "", "", PhoneCommandResult(False, "Who should I call?")

        match = self.resolve_contact(who) if (who and self.resolve_contact is not None) else None
        phone = getattr(match, "phone", "") if match is not None else ""
        if phone and getattr(match, "certain", False) is True:
            return phone, getattr(match, "key", "") or who, "from your contacts", None

        given = (number or "").strip()
        if given:
            normalized = normalize_phone(given, self.default_country)
            if not normalized:
                return "", "", "", PhoneCommandResult(False, _MSG_BLOCKED)
            return normalized, who or _spoken_number(normalized), "the number I found", None

        candidates = list(getattr(match, "candidates", None) or [])
        if candidates:
            names = ", ".join(candidates[:-1]) + f" or {candidates[-1]}" if len(candidates) > 1 else candidates[0]
            return "", "", "", PhoneCommandResult(False, f"Which {who}: {names}?", awaiting=True)
        if phone:
            # A loose match: the read-back names the contact, so he hears who it picked.
            return phone, getattr(match, "key", "") or who, "from your contacts", None
        return "", "", "", PhoneCommandResult(False, _MSG_NO_NUMBER.format(who=who))

    def _allowed(self, number: str) -> bool:
        if any(number.startswith(prefix) for prefix in self.blocked_prefixes):
            return False
        return any(number.startswith("+" + cc) for cc in self.allowed_country_codes)

    # ------------------------------------------------------------- confirmation

    def _confirmed(self, number: str, signature: str) -> bool:
        with self._lock:
            pending, self._pending = self._pending, None
        if not pending:
            return False
        p_number, p_signature, deadline = pending
        if time.monotonic() > deadline:
            return False
        return p_number == number and p_signature == signature

    def _arm(self, number: str, signature: str) -> None:
        with self._lock:
            self._pending = (number, signature, time.monotonic() + self.confirm_timeout_s)

    # ------------------------------------------------------------- public API

    def call(self, brief: CallBrief, number: str = "", confirm: bool = False) -> PhoneCommandResult:
        if not self.enabled:
            return PhoneCommandResult(False, _MSG_DISABLED)
        with self._lock:
            refusal = self._dial_refusal(self._active)
        if refusal is not None:
            return refusal

        resolved, label, source, error = self._resolve(brief, number)
        if error is not None:
            return error
        if not self._allowed(resolved):
            return PhoneCommandResult(False, _MSG_BLOCKED)

        signature = brief.signature()
        if not (confirm and self._confirmed(resolved, signature)):
            self._arm(resolved, signature)
            what = (brief.goal or "").strip() or brief.normalized_kind().replace("_", " ")
            return PhoneCommandResult(
                False,
                f"Ready to call {label} at {_spoken_number(resolved)} ({source}) to {what}. "
                "Read that back to Master Miguel, including where the number came from, "
                "and only call again with confirm true once he says go.",
                awaiting=True,
            )

        with self._lock:
            refusal = self._dial_refusal(self._active)
            if refusal is not None:
                return refusal
            self._active = {"owner": "outgoing", "label": label, "number": resolved, "since": time.monotonic()}
        self._thread = threading.Thread(
            target=self._run_call, args=(brief, resolved, label, source), name="phone-call", daemon=True
        )
        self._thread.start()
        return PhoneCommandResult(True, f"Calling {label} now. The report comes when the call ends.")

    def find_phone(self) -> PhoneCommandResult:
        """
        Ring the phone over adb: an alarm-stream beep, about 20 seconds, that
        plays through silent mode.

        Not during a call: the beep would play into his ear on the call. Blocks for the few
        seconds the UI takes, on the caller's thread, like the other tools.
        """
        if not self.enabled:
            return PhoneCommandResult(False, _MSG_DISABLED)
        with self._lock:
            active = self._active
        if active is not None:
            if not _is_human(active):
                return PhoneCommandResult(False, _MSG_BUSY)
            # Same split as a dial: "you're on a call" only when a read says so.
            return PhoneCommandResult(
                False, _MSG_HUMAN_FIND if active.get("state") == "connected" else _MSG_HUMAN_FIND_UNKNOWN
            )
        try:
            status = self._get_dialer().play_sound()
        except Exception:
            logger.exception("Find my phone failed")
            status = ""
        line = _FIND_LINES.get(status)
        if line is None:
            return PhoneCommandResult(False, _MSG_FIND_FAILED)
        return PhoneCommandResult(status in ("PLAYING", "ALREADY_PLAYING"), line)

    # ------------------------------------------------------------- what he missed

    def missed_calls(self) -> PhoneCommandResult:
        """
        Missed calls in the last `phone.digest.missed_window_hours`, from the
        phone's call log. Read-only. success=True even when there are none;
        False only when the phone could not be read.
        """
        if not self.enabled:
            return PhoneCommandResult(False, _MSG_DISABLED)
        now = time.time()
        with self._read_lock:
            failure = self._unreachable()
            if failure is None:
                calls, failure = self._read_missed(now)
        if failure is not None:
            return failure
        return PhoneCommandResult(True, missed_calls_line(calls, now, self.digest_window_hours))

    def digest(self, app: str = "") -> PhoneCommandResult:
        """
        Missed calls plus who is waiting in which app -- no message text. With
        `app`, that app's waiting texts instead, each quoted as the sender's
        words. Read-only, so allowed during a call (unlike find_phone).
        """
        if not self.enabled:
            return PhoneCommandResult(False, _MSG_DISABLED)
        now = time.time()
        app = (app or "").strip()
        with self._read_lock:
            failure = self._unreachable()
            if failure is not None:
                return failure
            calls = []
            if not app:
                calls, failure = self._read_missed(now)
                if failure is not None:
                    return failure
            notes, failure = self._read_notifications()
            if failure is not None:
                return failure
        if app:
            line = app_texts_line(app, notes, self.digest_app_names, now, max_items=self.digest_max_senders)
        else:
            line = digest_line(
                calls, notes, self.digest_app_names, now, self.digest_window_hours,
                max_apps=self.digest_max_apps, max_senders=self.digest_max_senders,
            )
        return PhoneCommandResult(True, line)

    def _unreachable(self) -> PhoneCommandResult | None:
        """The NO_PHONE line when the phone is not on adb, else None. Never raises."""
        try:
            status = self._get_read_phone().reachable()
        except Exception:
            logger.exception("Checking the phone over adb failed")
            return PhoneCommandResult(False, _READ_FAILURES[""])
        if status != "OK":
            return PhoneCommandResult(False, _READ_FAILURES["NO_PHONE"])
        return None

    def _read_missed(self, now: float) -> tuple[list, PhoneCommandResult | None]:
        """(calls, None), or ([], the failure line). Never raises."""
        try:
            raw = self._get_read_phone().missed_calls_raw(int((now - self.digest_window_hours * 3600) * 1000))
        except Exception:
            logger.exception("Reading the phone's call log failed")
            raw = None
        if raw is None:
            return [], PhoneCommandResult(False, _READ_FAILURES[""])
        return parse_call_log(raw), None

    def _read_notifications(self) -> tuple[list, PhoneCommandResult | None]:
        """(notifications without the ignored packages, None), or ([], the failure line). Never raises."""
        try:
            raw = self._get_read_phone().notifications_raw()
        except Exception:
            logger.exception("Reading the phone's notifications failed")
            raw = None
        if raw is None:
            return [], PhoneCommandResult(False, _READ_FAILURES[""])
        notes = [n for n in parse_notifications(raw) if n.package not in self.digest_ignore_packages]
        return notes, None

    def busy(self) -> bool:
        """True while one of her own calls is in progress (not a call he answered)."""
        with self._lock:
            return self._active is not None and not _is_human(self._active)

    def human_call_active(self) -> bool:
        """True while a call he had her answer holds the slot, in any state
        (unknown included). Read-only. The room keeps quiet about a second
        ring then: on speakerphone the first caller would hear the name."""
        with self._lock:
            return self._active is not None and _is_human(self._active)

    def status_line(self) -> str:
        with self._lock:
            active, last = self._active, self._last
        if active is not None and _is_human(active):
            if active.get("state") == "connected":
                seconds = int(time.monotonic() - active["since"])
                return f"You're on a call on your phone, {seconds // 60}m{seconds % 60:02d}s since I answered it."
            return "You may still be on a call on your phone; I can't read it right now."
        if active is not None:
            seconds = int(time.monotonic() - active["since"])
            return f"On the phone with {active['label']}, {seconds // 60}m{seconds % 60:02d}s in."
        if last is not None:
            return "No call right now. The last one: " + call_log_line(call_record(last, include_transcript=False))
        return "No call right now, and none earlier this session."

    def recent_calls(self, count: int = 5) -> list[dict]:
        """The last `count` calls from the call log, newest first. Survives restarts."""
        count = max(1, min(int(count or 5), 20))
        try:
            with open(self.log_path, encoding="utf-8") as handle:
                rows = handle.readlines()[-count:]
        except OSError:
            return []
        records = []
        for row in reversed(rows):
            try:
                records.append(json.loads(row))
            except ValueError:
                continue
        return records

    def pop_report(self) -> CallReport | None:
        try:
            return self._reports.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        # Stops the native monitor and nothing else: his call is his to end.
        self._native_stop.set()
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=15)
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread() and monitor.is_alive():
            monitor.join(timeout=5)

    def _dial_refusal(self, active: dict | None) -> PhoneCommandResult | None:
        """Why the slot cannot take an outgoing call, or None. Callers hold _lock."""
        if active is None:
            return None
        if _is_human(active):
            return PhoneCommandResult(
                False, _MSG_HUMAN_DIAL if active.get("state") == "connected" else _MSG_HUMAN_UNKNOWN
            )
        return PhoneCommandResult(False, _MSG_BUSY)

    # ------------------------------------------------------------- native answer

    def _read_control_topology(self):
        """The phone's calls on the control transport, or None. Never raises."""
        try:
            with self._control_lock:
                return self._get_control_phone().call_topology()
        except Exception:
            logger.exception("Reading the phone's calls failed")
            return None

    def _ring_now(self):
        """(wired, the watcher's live offer or None). Never raises."""
        if self._current_ring is None:
            return False, None
        try:
            return True, self._current_ring()
        except Exception:
            logger.exception("Reading the current ring failed")
            return True, None

    def _same_ring(self, offer) -> bool:
        """Is the watcher still on this ring episode? True when no watcher is wired."""
        wired, now = self._ring_now()
        return not wired or (now is not None and now.ring_id == offer.ring_id)

    def bind_incoming(self, offer: "RingOffer | None") -> IncomingTarget | None:
        """
        Bind the ring the watcher offers to telecom's key for it: exactly one
        call on the phone and it RINGING, with the watcher on the same episode
        before and after the read. None for anything else (no ring, call
        waiting, another call, unreadable). Read-only: never touches the phone.
        """
        if not self.native_answer_enabled or offer is None or offer.call.kind != "incoming":
            return None
        if not self._same_ring(offer):
            return None
        topology = self._read_control_topology()
        if topology is None:
            return None
        live = _live_legs(topology)   # a lingering ended leg is not a second call
        if len(live) != 1 or live[0].state != "ringing":
            return None
        if not self._same_ring(offer):
            return None
        return IncomingTarget(offer, live[0].key)

    def answer_speaker(self, target: IncomingTarget | None) -> PhoneCommandResult:
        """
        Answer the bound ring on the phone itself and put it on the phone's
        speaker. No read-back: he asked for exactly this. The slot is reserved
        under the lock, the adb work runs outside it, and the transport
        re-checks the key right before its one acceptance. An answered call is
        then watched read-only until it ends; nothing here ever hangs it up.
        """
        if not self.native_answer_enabled:
            return PhoneCommandResult(False, _MSG_NATIVE_DISABLED)
        if target is None:
            return self._why_no_target()
        wired, now = self._ring_now()
        if wired and now is None:
            return self._ring_gone(target, "NO_RING")
        if wired and now.ring_id != target.offer.ring_id:
            return PhoneCommandResult(False, _ANSWER_LINES["STALE_RING"])

        token = object()
        with self._lock:
            if self._active is not None:
                return PhoneCommandResult(False, _MSG_NATIVE_BUSY)
            self._active = {
                "owner": "human", "state": "answering", "label": "your call", "since": time.monotonic(),
                "ring_id": target.offer.ring_id, "call_key": target.call_key, "token": token,
            }

        accepted = verified = False
        try:
            with self._control_lock:
                result = self._get_control_phone().answer_incoming(target.call_key, speaker=True)
            status = str(result.status)
            accepted = getattr(result, "accepted", False) is True
            verified = accepted and getattr(result, "speaker_verified", False) is True
        except Exception:
            logger.exception("Answering the phone failed")
            status = "ANSWER_UNCERTAIN"

        if status in _ANSWERED:
            state = "connected"
        elif status not in _ANSWER_LINES or status == "ANSWER_UNCERTAIN":
            status = "ANSWER_UNCERTAIN"
            state = "unknown"   # it may have answered: keep it until a read says otherwise
        else:
            state = ""          # refused before any acceptance: nothing to own
        with self._lock:
            mine = self._active is not None and self._active.get("token") is token
            if mine:
                if state:
                    self._active["state"] = state
                else:
                    self._active = None
        if not mine:
            logger.warning("Phone answer: the reservation changed under the answer (%s)", status)
        elif state:
            self._start_monitor()
        logger.info("Phone answer: %s", status)

        if status in ("NO_RING", "STALE_RING"):
            return self._ring_gone(target, status)
        return PhoneCommandResult(status in _ANSWERED, _ANSWER_LINES[status],
                                  answered=accepted, speaker_verified=verified)

    def _ring_gone(self, target: IncomingTarget, status: str) -> PhoneCommandResult:
        """NO_RING / STALE_RING, unless the same call is now up: he answered it by hand."""
        topology = self._read_control_topology()
        if topology is not None and any(
            leg.key == target.call_key and leg.state in ("active", "holding", "answered") for leg in topology.legs
        ):
            return PhoneCommandResult(False, _MSG_ALREADY_ANSWERED)
        if status == "NO_RING" and topology is not None and _rings_alone(topology):
            # The watcher lost it, telecom still shows it: never "isn't ringing".
            return PhoneCommandResult(False, _MSG_RINGING_UNBOUND)
        return PhoneCommandResult(False, _ANSWER_LINES[status])

    def _why_no_target(self) -> PhoneCommandResult:
        """No ring was bound at the start of the turn: one read-only look says why."""
        topology = self._read_control_topology()
        if topology is None:
            return PhoneCommandResult(False, _ANSWER_LINES["UNREADABLE"])
        live = _live_legs(topology)
        if any(leg.state == "ringing" for leg in live) and len(live) > 1:
            return PhoneCommandResult(False, _MSG_NATIVE_BUSY)
        if _rings_alone(topology):
            return PhoneCommandResult(False, _MSG_RINGING_UNBOUND)
        return PhoneCommandResult(False, _ANSWER_LINES["NO_RING"])

    def _start_monitor(self) -> None:
        # Start and exit are both decided under _lock, so a monitor on its way
        # out can never leave a slot reserved a moment later unwatched.
        with self._lock:
            if self._monitor is not None:
                return
            monitor = self._monitor = threading.Thread(
                target=self._monitor_native, name="phone-native-call", daemon=True
            )
        monitor.start()

    def _monitor_native(self) -> None:
        """Read-only, every ~2 s, while he holds the slot. Never ends a call."""
        while not self._native_stop.wait(self._native_poll_s):
            try:
                self._check_native()
            except Exception:
                logger.exception("Watching the call on the phone failed")
            with self._lock:
                if self._active is None or not _is_human(self._active):
                    self._monitor = None
                    return

    def _check_native(self) -> bool:
        """
        One read-only look at the call he answered. The slot is released only
        when telecom no longer lists its key, or lists it ended; an unreadable
        read keeps the slot as "unknown", which blocks every new mutation.
        True while there is still something to watch.
        """
        with self._lock:
            active = self._active
        if active is None or not _is_human(active):
            return False
        topology = self._read_control_topology()
        key = active.get("call_key")
        with self._lock:
            if self._active is not active:
                return self._active is not None and _is_human(self._active)
            legs = [] if topology is None else [leg for leg in topology.legs if leg.key == key]
            if topology is None or any(leg.state == "unknown" for leg in legs):
                active["state"] = "unknown"
                return True
            if not legs or legs[0].state == "ended":
                self._active = None
                logger.info("Phone answer: his call ended")
                return False
            active["state"] = "connected" if legs[0].state in ("active", "holding", "answered") else "unknown"
            return True

    def set_speaker(self, on: bool) -> PhoneCommandResult:
        """
        Speaker on or off for the call on his phone -- one he answered by hand
        too. Never during her own call. The Security-settings gate first (the
        tap needs it), then exactly one live call and it active, then one tap
        on the phone's own Speaker button, verified by the audio route.
        """
        if not self.native_answer_enabled:
            return PhoneCommandResult(False, _MSG_NATIVE_DISABLED)
        with self._lock:
            active = self._active
        if active is not None and not _is_human(active):
            return PhoneCommandResult(False, _MSG_BUSY)
        try:
            with self._control_lock:
                phone = self._get_control_phone()
                status = phone.ready(need_interception=False, need_helper=False)
                if status == "OK":
                    status, key = _sole_active_call(phone.call_topology())
                if status == "OK":
                    result = phone.set_speaker(key, on)
                    if result.speaker_verified is True:
                        status = result.status
                    else:
                        status = result.status if result.status in ("NO_PHONE", "STALE_CALL") else "FAILED"
        except Exception:
            logger.exception("Switching the phone's speaker failed")
            status = "FAILED"
        if status == "ALREADY":
            return PhoneCommandResult(True, "It's already on speaker." if on else "It's already off speaker.")
        if status in ("SPEAKER_ON", "SPEAKER_OFF"):
            return PhoneCommandResult(True, "Speaker's on." if on else "Speaker's off.")
        return PhoneCommandResult(False, _SPEAKER_LINES.get(status, _SPEAKER_FAILED))

    # ------------------------------------------------------------- the call

    def _run_call(self, brief: CallBrief, number: str, label: str, source: str = "") -> None:
        started = datetime.now()
        started_at = started.isoformat(timespec="seconds")
        hang_up_status = ""
        result = CallResult()
        dial_status: list[str] = []
        try:
            dialer = self._get_dialer()

            audio_box: list = []

            def dial() -> bool:
                self._publish("call_state", {"state": "dialing", "label": label})
                dial_status.append(dialer.dial(number))
                placed = dial_status[-1] in _DIALED
                if placed and audio_box and audio_box[0] is not None:
                    audio_box[0].arm()  # only now may the bridge open on an ACTIVE call
                return placed

            def still_connected():
                # A dead bridge means no ears and no mouth: end the call.
                if audio_box and getattr(audio_box[0], "failed", False) is True:
                    return False
                return dialer.in_call()

            prompt = build_call_prompt(brief, owner=self.owner, callback_number=self.callback_number)
            self._publish("call_state", {
                "state": "starting", "label": label, "kind": brief.kind, "started_at": started_at,
            })
            # The other side's audio, recorded for debugging only when
            # transcripts are kept at all (it is their voice).
            record = (os.path.join("debug", "phone_adb", f"{started.strftime('%Y%m%d-%H%M%S')}-downlink.wav")
                      if self.include_transcripts else "")
            audio = dialer.audio(record) if hasattr(dialer, "audio") else None
            audio_box.append(audio)
            agent = self._make_agent(audio)
            result = agent.run(
                prompt,
                max_call_s=self.max_call_s,
                no_answer_s=self.no_answer_s,
                still_connected=still_connected,
                stop=self._stop,
                dial=dial,
                answered=audio.answered if audio is not None else None,
            )
            if audio is not None and getattr(audio, "failed", False) is True:
                result.error = result.error or "The call audio on your phone stopped, so I hung up."
            if result.ended_by == "dial_failed":
                result.error = _DIAL_FAILURES.get(dial_status[-1] if dial_status else "", _DIAL_FAILURES[""])
            elif dial_status:
                # Always try: if a "they hung up" reading was ever wrong, this
                # closes the line. NO_CALL is the normal answer after they hung up.
                hung = dialer.hang_up()
                hang_up_status = hung or ""
                if hung == "ENDED" and result.ended_by == "hung_up":
                    logger.warning("Call read as hung up was still live; ended it now")
                if hung not in ("ENDED", "NO_CALL"):
                    result.error = result.error or (
                        "I couldn't confirm the call hung up. Check the phone, it may still be connected."
                    )
        except Exception as exc:
            logger.exception("Phone call to %s failed", label)
            result.ended_by = result.ended_by or "error"
            result.error = str(exc)[:300]
        self._publish("call_state", {
            "state": "ended", "label": label, "ended_by": result.ended_by or "",
            "error": result.error or "",
        })
        if result.outcome is None and result.lines:
            result.outcome = self._read_outcome(brief, result)
        report = CallReport(
            label=label, number=number, brief=brief, result=result, started_at=started_at,
            call_id=started.strftime("%Y%m%d-%H%M%S"),
            ended_at=datetime.now().isoformat(timespec="seconds"),
            number_source=source,
            dial_status=dial_status[-1] if dial_status else "",
            hang_up_status=hang_up_status,
            backend=self.backend, model=self.model, voice=self.voice,
        )
        self._log(report)
        with self._lock:
            self._active = None
            self._last = report
        self._reports.put(report)
        logger.info("Call to %s ended (%s, %s)", label, result.ended_by, report.status)

    def _read_outcome(self, brief: CallBrief, result: CallResult) -> dict | None:
        summarize = self._summarize
        if summarize is None and self._agent_factory is None:
            from services.call_outcome import summarize_call

            summarize = lambda b, t: summarize_call(b, t, model=self._outcome_model)
        if summarize is None:
            return None
        try:
            return summarize(brief, result.transcript())
        except Exception:
            logger.exception("Reading the outcome of the call failed")
            return None

    def _log(self, report: CallReport) -> None:
        record = call_record(report, include_transcript=self.include_transcripts)
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Could not write call log: %s", exc)
