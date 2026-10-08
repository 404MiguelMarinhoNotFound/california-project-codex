"""
PhoneService: California places a phone call from Master Miguel's own number
and holds the conversation herself, then reports back.

    control_phone(phone_call, brief) -> read back once -> "go" -> background call:
        default mic -> VB-CABLE, Gemini Live session up, Phone Link dials,
        conversation, hang up, mic restored -> CallReport queued
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
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from services.gemini_live_call import CallResult
from services.phone_prompts import CallBrief, build_call_prompt
from services.whatsapp_service import looks_like_phone, normalize_phone

logger = logging.getLogger(__name__)

_MSG_DISABLED = "Phone calls aren't set up right now."
_MSG_BUSY = "I'm already on a call. Let me finish that one first."
_MSG_NO_NUMBER = (
    "{who} is not in his contacts. If it is a business (restaurant, clinic, shop), "
    "find its number with web search and call again with it as number. If it is a "
    "person, ask him for the number; never search for a private person's number."
)
_MSG_BLOCKED = "I won't call that number myself."

# A dial that went out. DIALED_UNCONFIRMED: Enter reached the verified Call
# button, but Phone Link's call screen is invisible to automation (2026-10-07:
# the call rang and was answered while the old check reported failure).
_DIALED = ("DIALED", "DIALED_UNCONFIRMED")

# Why a dial did not go out, in words California can say. Each needs a
# different fix, so they are never collapsed into "couldn't call".
_DIAL_FAILURES = {
    "PHONE_NOT_CONNECTED": "Phone Link isn't connected to the phone for calls. Toggle Bluetooth "
                           "on the phone and the laptop, then press Try again in Phone Link's Calls tab.",
    "NOT_PREFILLED": "Phone Link never showed the number on its dial pad, so I didn't press call.",
    "NO_BUTTON": "Phone Link's call button wasn't available.",
    "NOT_FOREGROUND": "I couldn't bring Phone Link to the front, so I didn't press anything.",
    "NOT_FOCUSED": "Phone Link's call button didn't take focus, so I didn't press anything.",
    "NO_CALL_WINDOW": "I pressed call but couldn't see the call start in Phone Link.",
    "": "I couldn't drive Phone Link to place the call.",
}

# Portuguese premium-rate and adult-line prefixes: never dialled by her.
_DEFAULT_BLOCKED_PREFIXES = ["+351760", "+351761", "+351762", "+351707", "+351708", "+351646", "+351648"]


@dataclass
class PhoneCommandResult:
    success: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.success


@dataclass
class CallReport:
    label: str
    number: str
    brief: CallBrief
    result: CallResult
    started_at: str = ""

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
        mic_route_factory: Callable | None = None,
        room_speaking: Callable[[], bool] | None = None,
        summarize: Callable | None = None,
    ):
        cfg = config.get("phone", {}) or {}
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
        self.cable_output = str(cfg.get("cable_output_device") or "CABLE Input")
        self.cable_mic_endpoint = str(cfg.get("cable_mic_endpoint") or "CABLE Output (VB-Audio Virtual Cable)")
        self.default_country = str(cfg.get("default_country") or "351")
        self.allowed_country_codes = [str(c) for c in (cfg.get("allowed_country_codes") or [self.default_country])]
        self.blocked_prefixes = [str(p) for p in (cfg.get("blocked_prefixes") or _DEFAULT_BLOCKED_PREFIXES)]
        log_cfg = config.get("logging", {}) or {}
        self.log_path = str(cfg.get("log_path") or os.path.join(log_cfg.get("dir", "logs"), "calls.jsonl"))
        self.include_transcripts = log_cfg.get("include_transcripts", True) is not False

        self.resolve_contact = resolve_contact
        self._dialer = dialer
        self._agent_factory = agent_factory
        self._mic_route_factory = mic_route_factory
        self._room_speaking = room_speaking
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

        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client_kwargs: dict = {}
        configured = bool(cfg.get("enabled", False))
        self.enabled = configured and self._probe()

    # ------------------------------------------------------------- setup

    def _probe(self) -> bool:
        """Never raises. Each missing piece logs its own fix."""
        if self._dialer is not None and self._agent_factory is not None:
            return True  # injected (tests)
        if sys.platform != "win32":
            logger.warning("Phone calls disabled: they drive Phone Link, which is Windows only")
            return False
        kwargs, why = self._client_kwargs()
        if kwargs is None:
            logger.warning("Phone calls disabled: %s", why)
            return False
        self.client_kwargs = kwargs
        # find_spec, not an import: google.genai is heavy, and boot should not
        # pay for it when no call is ever made.
        for module in ("google.genai", "soundcard"):
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
            from services.phone_link import PhoneLinkDialer

            self._dialer = PhoneLinkDialer()
        return self._dialer

    def _make_agent(self):
        if self._agent_factory is not None:
            return self._agent_factory()
        from services.gemini_live_call import GeminiLiveAgent

        return GeminiLiveAgent(
            client_kwargs=self.client_kwargs,
            model=self.model,
            voice=self.voice,
            language_code=self.language_code,
            silence_ms=self.silence_ms,
            cable_device=self.cable_output,
        )

    def _mic_route(self):
        if self._mic_route_factory is not None:
            return self._mic_route_factory()
        from services.phone_link import MicRoute

        return MicRoute(self.cable_mic_endpoint)

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
            return "", "", "", PhoneCommandResult(False, f"Which {who}: {names}?")
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
            busy = self._active is not None
        if busy:
            return PhoneCommandResult(False, _MSG_BUSY)

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
            )

        with self._lock:
            if self._active is not None:
                return PhoneCommandResult(False, _MSG_BUSY)
            self._active = {"label": label, "number": resolved, "since": time.monotonic()}
        self._thread = threading.Thread(
            target=self._run_call, args=(brief, resolved, label), name="phone-call", daemon=True
        )
        self._thread.start()
        return PhoneCommandResult(True, f"Calling {label} now. The report comes when the call ends.")

    def status_line(self) -> str:
        with self._lock:
            active, last = self._active, self._last
        if active is not None:
            seconds = int(time.monotonic() - active["since"])
            return f"On the phone with {active['label']}, {seconds // 60}m{seconds % 60:02d}s in."
        if last is not None:
            summary = (last.result.outcome or {}).get("summary") or last.status.replace("_", " ")
            return f"No call right now. The last one, to {last.label}: {summary}"
        return "No call right now, and none earlier this session."

    def pop_report(self) -> CallReport | None:
        try:
            return self._reports.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=15)

    # ------------------------------------------------------------- the call

    def _run_call(self, brief: CallBrief, number: str, label: str) -> None:
        started_at = datetime.now().isoformat(timespec="seconds")
        result = CallResult()
        dial_status: list[str] = []
        try:
            dialer = self._get_dialer()

            def dial() -> bool:
                dial_status.append(dialer.dial(number))
                return dial_status[-1] in _DIALED

            prompt = build_call_prompt(brief, owner=self.owner, callback_number=self.callback_number)
            with self._mic_route():
                agent = self._make_agent()
                result = agent.run(
                    prompt,
                    max_call_s=self.max_call_s,
                    no_answer_s=self.no_answer_s,
                    still_connected=dialer.in_call,
                    stop=self._stop,
                    dial=dial,
                    mute=self._room_speaking,
                )
                if result.ended_by == "dial_failed":
                    result.error = _DIAL_FAILURES.get(dial_status[-1] if dial_status else "", _DIAL_FAILURES[""])
                elif dial_status:
                    # Always try, still inside the mic route: a call whose End
                    # button was never seen (DIALED_UNCONFIRMED) reads None, not
                    # True, and skipping it would leave the line open on the
                    # room mic once the route is undone. After "hung_up" too:
                    # if that reading was ever wrong, this closes the line.
                    # NO_CALL is only reassuring once the call was seen on
                    # screen, which "hung_up" (seen, then gone) implies.
                    hung = dialer.hang_up()
                    unseen = (
                        dial_status[-1] == "DIALED_UNCONFIRMED"
                        and result.ended_by != "hung_up"
                        and hung != "ENDED"
                    )
                    if hung == "ENDED" and result.ended_by == "hung_up":
                        logger.warning("Call read as hung up was still live; ended it now")
                    if unseen or hung not in ("ENDED", "NO_CALL"):
                        result.error = result.error or (
                            "I couldn't confirm the call hung up. Check the phone, it may still be connected."
                        )
        except Exception as exc:
            logger.exception("Phone call to %s failed", label)
            result.ended_by = result.ended_by or "error"
            result.error = str(exc)[:300]
        if result.outcome is None and result.lines:
            result.outcome = self._read_outcome(brief, result)
        report = CallReport(label=label, number=number, brief=brief, result=result, started_at=started_at)
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
        record = {
            "ts": report.started_at,
            "to": report.label,
            "number": report.number,
            "kind": report.brief.normalized_kind(),
            "goal": report.brief.goal,
            "status": report.status,
            "ended_by": report.result.ended_by,
            "duration_s": report.result.duration_s,
            "outcome": report.result.outcome,
            "error": report.result.error,
        }
        if self.include_transcripts:
            record["transcript"] = [{"who": l.who, "text": l.text.strip()} for l in report.result.lines]
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Could not write call log: %s", exc)
