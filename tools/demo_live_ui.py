"""
Drive the live UI with scripted turns, without California, a mic or a speaker.

Starts a real EventBus + LiveUI on 127.0.0.1 with the token `demo` and plays
three turns into it, using the real `core/audio_levels.envelope()` on WAVs found
under sounds/ (bootup and activation clips) or on synthetic speech when none
are there (a fresh clone has none; they are generated audio):

  1. "put on Fallout"            ends happy
  2. "turn on the attic lights"  a failed light, ends sheepish
  3. a long reply cut off by "California"  ends with a stopped clip_end

    uv run python tools/demo_live_ui.py                 # open the printed URL
    uv run python tools/demo_live_ui.py --loop --no-text
    uv run --no-project --with playwright python tools/demo_live_ui.py --check

`--check` opens the page in headless Chromium at 390x844 and 1280x900, in both
colour schemes, screenshots each state and mood into debug/live_ui/ (gitignored),
and fails on a console error, a horizontal scroll, or a halo that keeps moving
more than 300ms after she was cut off. Playwright is imported only there, so the
plain demo needs nothing beyond the project's own dependencies. The check runs
from outside the project environment (`--no-project`) so uv.lock is untouched;
it then needs numpy and pyyaml, which `--with` adds if missing.
"""

import argparse
import glob
import math
import os
import sys
import time

import numpy as np

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from core.audio_levels import envelope  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.live_ui import LiveUI  # noqa: E402
from core.turn_log import _short_target  # noqa: E402


def _min_gap(sats) -> float:
    """The smallest centre-to-centre distance between visible satellites, px."""
    pts = [(s["x"], s["y"]) for s in sats if s["alpha"] > 0.3]
    best = 1e9
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            best = min(best, math.dist(pts[i], pts[j]))
    return best

TOKEN = "demo"
# Turn ids are unique across every script played into one bus, like a real run.
_TURN_IDS = iter(range(1, 1_000_000))
HZ = 25
DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
SHOTS = os.path.join(ROOT, "debug", "live_ui")


# ------------------------------------------------------------------ audio
def _synthetic_speech(text: str, sr: int = 24000) -> np.ndarray:
    """A voiced, syllable-shaped signal about as long as `text` takes to say."""
    syllables = max(2, int(len(text) / 3.2))
    seconds = syllables * 0.19 + 0.25
    t = np.arange(int(seconds * sr)) / sr
    rng = np.random.default_rng(len(text))
    rate = syllables / seconds
    syll = np.clip(np.sin(2 * math.pi * rate * t + np.sin(t * 1.7) * 2), 0, None) ** 1.4
    phrase = np.sin(np.clip(t / seconds, 0, 1) * math.pi) ** 0.4
    loud = 0.25 + 0.75 * syll * (0.6 + 0.4 * rng.random(len(t)) ** 0.2)
    carrier = np.sin(2 * math.pi * 190 * t) + 0.4 * np.sin(2 * math.pi * 380 * t)
    return (0.25 * loud * phrase * carrier).astype(np.float32)


def _real_clips() -> list[tuple[np.ndarray, int]]:
    try:
        import soundfile as sf
    except Exception:
        return []
    clips = []
    for path in sorted(glob.glob(os.path.join(ROOT, "sounds", "**", "*.wav"), recursive=True))[:12]:
        try:
            data, sr = sf.read(path, dtype="float32")
            clips.append((data, sr))
        except Exception:
            continue
    return clips


class Voice:
    """Clip payloads: a real WAV when there is one, synthetic speech otherwise."""

    def __init__(self, include_text: bool):
        self.include_text = include_text
        self.real = _real_clips()
        self._ids = iter(range(1, 1_000_000))
        self._next_real = 0

    def clip(self, text: str, kind: str = "reply") -> tuple[dict, float]:
        if self.real:
            data, sr = self.real[self._next_real % len(self.real)]
            self._next_real += 1
        else:
            data, sr = _synthetic_speech(text), 24000
        ms = int(round(len(data) / sr * 1000))
        payload = {"clip": next(self._ids), "kind": kind, "hz": HZ, "ms": ms, "env": envelope(data, sr, HZ)}
        if text and self.include_text:
            payload["text"] = text
        return payload, ms / 1000.0


# ------------------------------------------------------------------ script
class Script:
    """
    The scripted turns as a flat list of steps. Each step is (wait_s, kind, arg):
    `publish` (event kind, data), `snap` (a screenshot label) or `mark_stop`
    (the instant a stopped clip_end went out, for the settle assertion).
    """

    def __init__(self, voice: Voice):
        self.v = voice
        self.steps: list[tuple[float, str, object]] = []
        self.turn = 0

    def at(self, wait_s, kind, arg=None):
        self.steps.append((wait_s, kind, arg))

    def pub(self, event, data, wait_s=0.0):
        self.at(wait_s, "publish", (event, data))

    def idle_scores(self, seconds, base=0.02):
        for i in range(int(seconds * 10)):
            self.pub("wake", {"score": round(base + 0.01 * math.sin(i), 4), "phase": "idle", "threshold": 0.81}, 0.1)
            if i % 2 == 0:
                self.pub("mic", {"level": 0.05, "phase": "idle"})

    def mic(self, seconds, phase="recording", level=0.55):
        for i in range(int(seconds * 15)):
            lv = max(0.0, level * (0.6 + 0.4 * math.sin(i * 0.9)))
            self.pub("mic", {"level": round(lv, 2), "phase": phase}, 1 / 15)

    def say(self, text, kind="reply", stop_after=None):
        payload, seconds = self.v.clip(text, kind)
        self.pub("clip_start", payload)
        if kind == "reply" and self.v.include_text:
            self.pub("sentence", {"text": text})
        if stop_after is not None:
            self.at(stop_after, "snap", "speaking")
            self.pub("clip_end", {"clip": payload["clip"], "stopped": True})
            self.at(0.0, "mark_stop")
            return
        self.pub("clip_end", {"clip": payload["clip"], "stopped": False}, seconds + 0.15)

    def wake(self, said, chained=False):
        self.turn = next(_TURN_IDS)
        if not chained:
            self.pub("wake", {"score": 0.93, "phase": "idle", "threshold": 0.81})
            self.pub("wake_hit", {"score": 0.93, "phase": "idle"})
        self.pub("turn_start", {"turn": self.turn, "chained": chained, "ts": ""})
        if not chained:
            self.say("Sup.", kind="ack")
        self.pub("state", {"state": "listening"})
        self.at(0.4, "snap", "listening")
        self.mic(1.6)
        self.pub("turn_mark", {"turn": self.turn, "name": "speech_end", "ms": 2300})
        self.pub("state", {"state": "thinking"})
        self.pub("turn_set", {"turn": self.turn, "fields": {"transcript": said} if self.v.include_text else {}}, 0.6)
        self.pub("turn_mark", {"turn": self.turn, "name": "stt_done", "ms": 2900})

    def end(self, outcome, ms):
        self.pub("turn_mark", {"turn": self.turn, "name": "reply_done", "ms": ms})
        self.pub("turn_end", {"turn": self.turn, "record": {"outcome": outcome, "total_ms": ms, "marks": {
            "speech_end": 2300, "stt_done": 2900, "llm_first_token": 3600, "first_sentence": 3900,
            "first_audio": 4300, "reply_done": ms}}})
        self.pub("state", {"state": "idle"})

    def tool(self, name, action, result, ok, seconds=1.2):
        self.pub("turn_tool_start", {"turn": self.turn, "name": name, "action": action, "start_ms": 3700})
        self.at(seconds * 0.5, "snap", "thinking")
        self.pub("turn_tool", {"turn": self.turn, "name": name, "action": action, "start_ms": 3700,
                               "ms": int(seconds * 1000), "result": result, "ok": ok}, seconds * 0.5)

    # A tool call in two halves, so others can run in between (a web search
    # overlapping a local tool) and a test can drop the end on purpose.
    _start_ms = 3700

    def tool_start(self, name, action, target=None, start_ms=None, wait_s=0.0):
        """Publish a start, the way TurnTimer.tool_started would (label trimmed). Returns the key."""
        if start_ms is None:
            self._start_ms += 137
            start_ms = self._start_ms
        data = {"turn": self.turn, "name": name, "action": action, "start_ms": start_ms}
        label = _short_target(target) if self.v.include_text else None
        if label:
            data["target"] = label
        self.pub("turn_tool_start", data, wait_s)
        return (name, action, start_ms, target)

    def tool_label(self, call, target, wait_s=0.0):
        name, action, start_ms, _ = call
        label = _short_target(target) if self.v.include_text else None
        if label:
            self.pub("turn_tool_start", {"turn": self.turn, "name": name, "action": action,
                                         "start_ms": start_ms, "target": label}, wait_s)
        return (name, action, start_ms, target)

    def tool_end(self, call, result, ok=None, awaiting=False, ms=None, wait_s=0.0, tool_input=None):
        name, action, start_ms, target = call
        entry = {"turn": self.turn, "name": name, "action": action, "start_ms": start_ms,
                 "ms": int(ms if ms is not None else wait_s * 1000), "result": result}
        if ok is True or ok is False:
            entry["ok"] = ok
        elif awaiting:
            entry["awaiting"] = True
        label = _short_target(target) if self.v.include_text else None
        if label:
            entry["target"] = label
        if tool_input and self.v.include_text:
            entry["input"] = tool_input
        self.pub("turn_tool", entry, wait_s)

    def call_tool(self, name, action, target, result, ok=None, awaiting=False, seconds=1.0, ms=None, snap=None,
                  tool_input=None):
        call = self.tool_start(name, action, target)
        if snap:
            self.at(seconds * 0.6, "snap", snap)
            self.tool_end(call, result, ok, awaiting, ms if ms is not None else int(seconds * 1000), seconds * 0.4,
                          tool_input=tool_input)
        else:
            self.tool_end(call, result, ok, awaiting, ms if ms is not None else int(seconds * 1000), seconds,
                          tool_input=tool_input)
        return call

    def probe(self, label, js, predicate, message, wait_s=0.0):
        """Evaluate `js` in the page here; fail the check with `message` when predicate(value) is false."""
        self.at(wait_s, "probe", (label, js, predicate, message))

    def reply(self, text, mood=None):
        if mood:
            self.pub("mood", {"mood": mood, "reason": "demo"})
        self.pub("state", {"state": "speaking"})
        self.say(text)

    # ------------------------------------------------------------ the tools script
    def build_tools(self):
        """Every tool she can pick, and the edge cases. See check_tools()."""
        sats = "window.__halo ? window.__halo.sats : []"
        self.pub("state", {"state": "idle"})
        self.idle_scores(1.0)

        # 1. Dark room -> Fallout: a 33s deep wake (compressed), then Stremio.
        self.wake("put on fallout")
        self.pub("state", {"state": "speaking"})
        self.say("Waking the TV, give me half a minute.")
        self.pub("state", {"state": "thinking"})
        wake = self.tool_start("control_tv", "turn_on")
        self.at(2.5, "snap", "tv-turn-on-running")
        self.probe("turn_on running", sats, lambda v: len(v) == 1 and v[0]["st"] == "run" and "/ ~33" in v[0]["note"],
                   "turn_on should be one running satellite timed against ~33s")
        self.tool_end(wake, "The TV and box are on.", ok=True, ms=33200, wait_s=2.0)
        play = self.tool_start("control_tv", "stremio_play", "Fallout S2E9", wait_s=0.3)
        self.at(1.5, "snap", "tv-stremio-running")
        self.tool_end(play, "Fallout is playing.", ok=True, ms=19800, wait_s=1.0)
        self.at(0.25, "snap", "tv-stremio-docking")
        self.reply("Fallout's on. Season two, episode nine.", mood="happy")
        self.end("reply", 41000)
        self.probe("docked", sats, lambda v: len(v) == 0, "docked satellites should be gone", wait_s=1.5)

        # 2. YouTube search, volume, and a status read (no mood) in one turn.
        self.wake("search youtube for lofi and set the volume to thirty")
        self.call_tool("control_tv", "youtube_search", "lofi beats to study to", "Playing it.", ok=True, seconds=2.2,
                       ms=4800, snap="tv-youtube-running")
        self.call_tool("control_tv", "volume_set", "volume 30", "TV volume 18 to 30", ok=True, seconds=0.35)
        self.call_tool("control_tv", "get_status", None, "TV on, YouTube playing.", seconds=0.9, snap="tv-status-running")
        self.at(0.1, "snap", "rail-three-tools")
        self.reply("Lofi's on, volume thirty.", mood="happy")
        self.end("reply", 12000)

        # 3. Lights: one fails, then a status read.
        self.wake("turn on the attic lights")
        self.call_tool("control_lights", "light_on", "attic", "I couldn't reach your lights just now.", ok=False,
                       seconds=2.4, ms=6200)
        self.pub("mood", {"mood": "sheepish", "reason": "control_lights.light_on failed"})
        self.at(0.6, "snap", "lights-failed")
        self.probe("fail state", sats, lambda v: any(x["st"] == "fail" for x in v), "a failed light should show the fail state")
        self.call_tool("control_lights", "light_status", "attic", "Last I set it: off.", seconds=0.5)
        self.reply("The attic strip isn't answering. Probably out of Bluetooth range again.")
        self.end("reply", 9000)
        self.probe("fail gone", sats, lambda v: len(v) == 0, "a failed satellite should sink and go", wait_s=2.5)

        # 4. Vacuum: the status guard, then the clean.
        self.wake("vacuum the kitchen and the hall")
        self.call_tool("control_vacuum", "vacuum_status", None, "Docked, 92%.", seconds=1.6, ms=2100)
        self.call_tool("control_vacuum", "vacuum_clean_rooms", "kitchen, hall", "Cleaning kitchen and hall.", ok=True,
                       seconds=2.0, ms=3100, snap="vacuum-cleaning")
        self.reply("Sir Sucks-a-Lot is on it.", mood="happy")
        self.end("reply", 9000)

        # 5. WhatsApp: a read-back parks, his yes sends it.
        self.wake("tell marta i'm running late")
        self.call_tool("control_whatsapp", "whatsapp_send", "to Marta", "I've got Marta Zuka. Say send it.",
                       awaiting=True, seconds=0.25, ms=60)
        self.reply("Marta Zuka, that you're running late. Send it?")
        self.end("reply", 6000)
        self.at(0.6, "snap", "whatsapp-waiting")
        self.probe("ask parked", sats, lambda v: len(v) == 1 and v[0]["st"] == "ask" and "waiting" in v[0]["note"],
                   "a read-back should park as 'waiting for you' after the turn")
        self.idle_scores(1.0)
        self.wake("yes send it")
        self.call_tool("control_whatsapp", "whatsapp_send", "to Marta", "Sent to Marta Zuka.", ok=True, seconds=1.8,
                       ms=1900, snap="whatsapp-sending",
                       tool_input={"to": "Marta", "message": "Chego daqui a dez minutos, guarda-me um pastel de nata",
                                   "confirm": "True"})
        self.probe("ask retired", sats, lambda v: not any(x["st"] == "ask" for x in v),
                   "the parked read-back should go once he answered")
        self.reply("Sent.", mood="happy")
        self.end("reply", 5000)

        # 6. WhatsApp reads: unread (list only), then one chat.
        self.wake("anything on whatsapp")
        self.call_tool("control_whatsapp", "whatsapp_unread", None, "Unread from Rui.", seconds=1.4, ms=2600)
        self.call_tool("control_whatsapp", "whatsapp_read", "Rui's chat", "The last 3 messages...", seconds=1.2, ms=1900,
                       snap="whatsapp-read")
        self.reply("Rui says the match moved to Sunday.")
        self.end("reply", 9000)

        # 7. A phone call: read back, go, then it orbits while she is idle.
        self.wake("call taberna da praia for a table")
        self.call_tool("control_phone", "phone_call", "Taberna da Praia", "Ready to call. Read it back.",
                       awaiting=True, seconds=0.2, ms=40)
        self.reply("Taberna da Praia, 912 345 678, a table for two at eight. Go?")
        self.end("reply", 7000)
        self.wake("go")
        self.call_tool("control_phone", "phone_call", "Taberna da Praia", "Calling Taberna da Praia now.",
                       ok=True, seconds=0.3, ms=120)
        self.reply("Calling them now.", mood="happy")
        self.end("reply", 4000)
        self.pub("call_state", {"state": "starting", "label": "Taberna da Praia", "kind": "book_table",
                                "started_at": "2026-10-13T20:00:00"})
        self.pub("call_state", {"state": "dialing", "label": "Taberna da Praia"}, 0.8)
        for i, (who, text) in enumerate([("them", "Taberna da Praia, boa noite."),
                                         ("her", "Boa noite! Sou a assistente do Miguel."),
                                         ("them", "Diga."), ("her", "Queria uma mesa para dois às oito.")]):
            self.pub("call_line", {"index": i, "who": who, "text": text, "at": 2 + i * 2}, 0.9)
        self.idle_scores(0.5)
        self.at(0.0, "snap", "phone-call-idle")
        self.probe("call orbits", sats, lambda v: len(v) == 1 and v[0]["st"] == "live",
                   "the call should stay in orbit after the turn, while she is idle")
        self.pub("call_state", {"state": "ended", "label": "Taberna da Praia", "ended_by": "end_call", "error": ""}, 1.0)
        self.probe("call docked", sats, lambda v: len(v) == 0, "an ended call should dock and go", wait_s=2.0)

        # 8. Find my phone.
        self.wake("where is my phone")
        self.call_tool("control_phone", "phone_find", "your phone", "It's ringing.", ok=True, seconds=1.3, ms=1500)
        self.reply("Ringing it now.", mood="happy")
        self.end("reply", 5000)

        # 9. Web search: learns its query after it starts; then search + a local
        #    tool in one response, the search answered after the tool.
        self.wake("did benfica win last night")
        web = self.tool_start("web_search", "search")
        self.tool_label(web, "benfica result last night", wait_s=0.4)
        self.at(1.0, "snap", "web-search-running")
        self.probe("web sweeps", sats, lambda v: len(v) == 1 and v[0]["dev"] == "web" and "/ ~" not in v[0]["note"]
                   and v[0]["label"] == "benfica result last night", "a web search should sweep with its query as the label")
        self.tool_end(web, "3 results", ok=True, ms=2400, wait_s=1.0)
        self.reply("They did. Two nil.")
        self.end("reply", 8000)
        self.wake("open stremio and find me something to watch")
        web = self.tool_start("web_search", "search", "best new series october")
        app = self.tool_start("control_tv", "launch_app", "stremio", wait_s=0.3)
        self.at(0.8, "snap", "web-plus-local")
        self.probe("two at once", sats, lambda v: len(v) == 2, "a search and a local tool should both show")
        self.tool_end(app, "Opened Stremio.", ok=True, ms=2900, wait_s=0.6)
        self.tool_end(web, "5 results", ok=True, ms=3400, wait_s=1.2)
        self.reply("Stremio's open. Try Severance.", mood="happy")
        self.end("reply", 9000)

        # 10. Crowding: a call in orbit, a parked read-back, and three running.
        self.wake("call mum")
        self.call_tool("control_phone", "phone_call", "Mum", "Calling Mum now.", ok=True, seconds=0.2, ms=90)
        self.pub("call_state", {"state": "dialing", "label": "Mum"}, 0.2)
        self.end("reply", 3000)
        self.wake("lights, tv, vacuum and message the group")
        self.call_tool("control_whatsapp", "whatsapp_send", "🌊🏄 Surf crew de Carcavelos 🏄🌊", "NOT SENT YET.",
                       awaiting=True, seconds=0.2, ms=50)
        a = self.tool_start("control_lights", "light_color", "living room, warm white")
        b = self.tool_start("control_tv", "turn_on", None, wait_s=0.15)
        c = self.tool_start("control_vacuum", "vacuum_clean_all", "everywhere", wait_s=0.15)
        d = self.tool_start("web_search", "search", "weather carcavelos tomorrow", wait_s=0.15)
        self.at(1.2, "snap", "crowd-6")
        self.at(0.0, "snap", "rail-crowd")
        self.probe("crowd", sats, lambda v: 5 <= len(v) <= 6, "five or six satellites should share the ring")
        self.probe("no overlap", sats, lambda v: _min_gap(v) >= 40,
                   "crowded satellites must not sit on each other (min gap px)")
        for call, ok in ((a, True), (b, False), (c, True), (d, True)):
            self.tool_end(call, "ok", ok=ok, ms=1500, wait_s=0.35)
        self.end("reply", 9000)
        self.pub("call_state", {"state": "ended", "label": "Mum", "ended_by": "hung_up", "error": ""}, 1.0)

        # 11. Labels that do not fit: long, emoji, right-to-left.
        self.wake("labels")
        for target in ("Fallout: The Complete Collection Director's Cut Extended Edition Remastered 4K HDR",
                       "📺🔥🎬 Série nova 🎬🔥📺", "مرحبا بالعالم من كاركافيلوس", "שלום עולם, Carcavelos"):
            self.tool_start("control_tv", "stremio_play", target, wait_s=0.05)
        self.at(0.9, "snap", "labels")
        self.probe("labels inside", "window.__labelBounds ? window.__labelBounds() : null",
                   lambda v: v is not None and v["out"] == 0, "every label must stay inside the stage")
        self.end("reply", 3000)

        # 12. A tool whose end never arrives, and one cut off by a barge-in.
        self.wake("never ends")
        self.tool_start("control_tv", "turn_on")
        self.at(0.8, "snap", "orphan-running")
        self.end("reply", 3000)
        self.probe("orphan lost", sats, lambda v: all(x["st"] == "lost" for x in v), "an unanswered tool should show as lost at turn end",
                   wait_s=0.2)
        self.probe("orphan gone", sats, lambda v: len(v) == 0, "a lost satellite should fade out", wait_s=1.2)
        self.wake("play fallout")
        self.tool_start("control_tv", "stremio_play", "Fallout")
        self.pub("state", {"state": "speaking"})
        self.say("Putting Fallout on, it might take a moment to find a good source for you.", stop_after=0.8)
        self.pub("wake_hit", {"score": 0.62, "phase": "reply"})
        self.at(0.3, "snap", "barge-in-mid-tool")
        self.end("barged_in", 4000)
        self.probe("barge lost", sats, lambda v: len(v) == 0, "a barged-in tool must not stay up", wait_s=1.5)

        # 13. Rapid turns back to back.
        for i in range(12):
            self.wake(f"rapid {i}", chained=True)
            self.call_tool("control_lights", "light_on" if i % 2 else "light_off", "attic", "ok", ok=True,
                           seconds=0.12, ms=300)
            self.end("reply", 2000)
        self.probe("rapid bounded", sats, lambda v: len(v) <= 6, "rapid turns must not pile satellites up")
        self.probe("rapid drained", sats, lambda v: len(v) == 0, "rapid turns should all dock and go", wait_s=2.5)
        self.at(0.0, "snap", "end")
        return self.steps

    def build(self):
        self.pub("state", {"state": "idle"})
        self.idle_scores(1.5)
        self.at(0.0, "snap", "idle")

        # 1. "put on Fallout" ends happy.
        self.wake("put on fallout")
        self.tool("control_tv", "stremio_play", "Fallout is playing on Stremio.", True, 1.6)
        self.pub("mood", {"mood": "happy", "reason": "control_tv.stremio_play worked"})
        self.pub("state", {"state": "speaking"})
        self.pub("turn_mark", {"turn": self.turn, "name": "first_audio", "ms": 4300})
        self.say("Fallout is up on the TV, Master Miguel.")
        self.at(0.0, "snap", "happy")
        self.say("Season two, episode nine. Picking up where you left off.")
        self.end("reply", 9800)
        self.idle_scores(1.2)

        # 2. A failed light ends sheepish.
        self.wake("turn on the attic lights")
        self.tool("control_lights", "light_on", "I couldn't reach your lights just now.", False, 1.4)
        self.pub("mood", {"mood": "sheepish", "reason": "control_lights.light_on failed"})
        self.pub("state", {"state": "speaking"})
        self.say("The attic strip isn't answering. Probably out of Bluetooth range again.")
        self.at(0.0, "snap", "sheepish")
        self.end("reply", 8600)
        self.idle_scores(1.0)

        # 3. A long reply, cut off by the wake word.
        self.wake("tell me about the bagpipes")
        self.pub("state", {"state": "speaking"})
        self.say("The bagpipes are older than Scotland itself, and honestly, the neighbours "
                 "have been complaining about them for about as long.", stop_after=1.6)
        self.pub("wake_hit", {"score": 0.62, "phase": "reply"})
        self.at(0.35, "snap", "barged_in")
        self.end("barged_in", 7200)
        self.idle_scores(1.0)
        self.at(0.0, "snap", "idle_after")
        return self.steps


# ------------------------------------------------------------------ server
def start(port: int, include_text: bool = True, bus=None):
    os.environ["CALIFORNIA_UI_TOKEN"] = TOKEN
    try:
        import yaml

        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
    except Exception:
        config = {}
    ui = dict(config.get("ui") or {})
    ui.update({"enabled": True, "host": "127.0.0.1", "port": port})
    config["ui"] = ui
    config["logging"] = {**(config.get("logging") or {}), "include_transcripts": include_text}
    bus = bus or EventBus()
    live = LiveUI(config, bus)
    if not live.start():
        sys.exit(f"Could not listen on 127.0.0.1:{port}")
    return bus, live


def run_steps(bus, steps, wait, on_snap=None, on_stop=None, on_probe=None):
    for wait_s, kind, arg in steps:
        if wait_s:
            wait(wait_s)
        if kind == "publish":
            event, data = arg
            bus.publish(event, data)
        elif kind == "snap" and on_snap:
            on_snap(arg)
        elif kind == "mark_stop" and on_stop:
            on_stop()
        elif kind == "probe" and on_probe:
            on_probe(*arg)


# ------------------------------------------------------------------ check
def check(port: int, include_text: bool) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("--check needs Playwright: uv run --no-project --with playwright python tools/demo_live_ui.py --check")
        return 2

    bus, live = start(port)
    os.makedirs(SHOTS, exist_ok=True)
    url = f"http://127.0.0.1:{live._server.server_address[1]}/{TOKEN}/?debug"
    failures: list[str] = []
    executable = os.environ.get("CHROMIUM_PATH") or (DEFAULT_CHROMIUM if os.path.exists(DEFAULT_CHROMIUM) else None)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=executable,
            args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
        )
        for width, height in ((390, 844), (1280, 900)):
            for scheme in ("dark", "light"):
                name = f"{width}x{height}-{scheme}"
                ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
                page = ctx.new_page()
                errors: list[str] = []
                page.on("console", lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.goto(url)
                page.wait_for_function("window.__halo !== undefined", timeout=10000)
                page.wait_for_timeout(600)
                if page.evaluate("document.body.classList.contains('has-halo')") is not True:
                    failures.append(f"{name}: the halo did not draw (WebGL?)")

                def snap(label, page=page, name=name):
                    page.screenshot(path=os.path.join(SHOTS, f"{name}-{label}.png"))
                    scroll = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    if scroll > 0:
                        failures.append(f"{name}/{label}: horizontal scroll of {scroll}px")

                stop_levels = {}

                def stopped(page=page, stop_levels=stop_levels):
                    stop_levels["before"] = page.evaluate("window.__halo.voice")
                    page.wait_for_timeout(300)
                    stop_levels["after"] = page.evaluate("window.__halo.voice")

                steps = Script(Voice(include_text)).build()
                run_steps(bus, steps, lambda s, page=page: page.wait_for_timeout(int(s * 1000)), snap, stopped)

                if stop_levels.get("before", 0) < 0.2:
                    failures.append(f"{name}: the halo was not moving before the barge-in "
                                    f"(voice {stop_levels.get('before')})")
                if stop_levels.get("after", 1) >= 0.05:
                    failures.append(f"{name}: voice still {stop_levels.get('after'):.3f} 300ms after a stopped clip_end")
                mood = page.evaluate("window.__halo.mood")
                if errors:
                    failures.append(f"{name}: console errors: {errors[:3]}")
                print(f"{name}: voice before stop {stop_levels.get('before', 0):.2f}, "
                      f"300ms after {stop_levels.get('after', 1):.3f}, mood at end {mood}, "
                      f"{len(errors)} console errors")
                ctx.close()
        failures += _check_ambient_and_fallback(browser, url)
        failures += _check_dusk(browser, url)
        browser.close()
    live.stop()
    if failures:
        print("FAILED")
        for f in failures:
            print("  " + f)
        return 1
    print(f"OK: screenshots in {SHOTS}")
    return 0


def _check_dusk(browser, url) -> list[str]:
    """
    The Dusk redesign's promises: the fonts load, the strip pins once the halo
    scrolls away and lets go in ambient, every control is a 44px target, and a
    turn row (and a call row, when there is one) opens on a tap. Opened on the
    session the earlier contexts played, so the feed has rows to click.
    """
    failures: list[str] = []
    for width, height, scheme in ((390, 844, "dark"), (390, 844, "light"), (1280, 900, "dark")):
        name = f"dusk-{width}-{scheme}"
        ctx, page, errors = _open(browser, url, width, height, scheme)
        _wait_for(page, "!document.body.classList.contains('loading')")
        page.evaluate("document.fonts.ready")
        if page.evaluate("document.fonts.check('16px Geist') && document.fonts.check('16px \"Geist Mono\"')") is not True:
            failures.append(f"{name}: Geist did not load")

        page.evaluate("window.scrollTo(0, document.getElementById('halo').offsetHeight + 240)")
        page.wait_for_timeout(500)
        if page.evaluate("document.body.classList.contains('pinned')") is not True:
            failures.append(f"{name}: the strip did not pin after scrolling past the halo")
        elif float(page.evaluate("getComputedStyle(document.getElementById('pin')).opacity")) < 0.9:
            failures.append(f"{name}: pinned, but the strip is not showing")
        page.screenshot(path=os.path.join(SHOTS, f"{name}-pinned.png"))
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        if page.evaluate("document.body.classList.contains('pinned')"):
            failures.append(f"{name}: still pinned with the halo back in view")

        page.evaluate("window.scrollTo(0, document.getElementById('halo').offsetHeight + 240)")
        page.wait_for_timeout(400)
        page.evaluate("document.getElementById('halo-full').click()")
        page.wait_for_timeout(300)
        if page.evaluate("document.body.classList.contains('pinned')"):
            failures.append(f"{name}: still pinned in ambient mode")
        page.evaluate("document.getElementById('halo-full').click()")
        page.wait_for_timeout(300)

        small = page.evaluate("""[...document.querySelectorAll('button')].filter((b) => {
            const r = b.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.height < 44;
          }).map((b) => (b.id || b.className || b.textContent).slice(0, 30))""")
        if small:
            failures.append(f"{name}: buttons under 44px tall: {small[:5]}")

        if page.evaluate("document.querySelectorAll('#turns .turn > button').length") == 0:
            failures.append(f"{name}: no turn rows to open")
        else:
            page.click("#turns .turn > button")
            page.wait_for_timeout(200)
            if page.evaluate("document.querySelector('#turns .turn > button').getAttribute('aria-expanded')") != "true" \
                    or page.evaluate("document.querySelector('#turns .turn .detail').hidden"):
                failures.append(f"{name}: a turn row did not open on a tap")
        if page.evaluate("document.querySelectorAll('#calls-list .subhead').length"):
            page.click("#calls-list .subhead")
            page.wait_for_timeout(400)
            if page.evaluate("document.querySelector('#calls-list .sub-body').hidden"):
                failures.append(f"{name}: a call row did not open on a tap")
        page.screenshot(path=os.path.join(SHOTS, f"{name}-feed.png"), full_page=True)
        if errors:
            failures.append(f"{name}: console errors: {errors[:3]}")
        print(f"{name}: pinned strip, targets, rows checked")
        ctx.close()
    return failures


def _launch(pw):
    executable = os.environ.get("CHROMIUM_PATH") or (DEFAULT_CHROMIUM if os.path.exists(DEFAULT_CHROMIUM) else None)
    return pw.chromium.launch(
        executable_path=executable,
        args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    )


def _open(browser, url, width, height, scheme="dark", reduced=False, block_three=False):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                              reduced_motion="reduce" if reduced else "no-preference")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    if block_three:
        page.route("**/vendor/three.min.js", lambda route: route.abort())
    page.goto(url)
    if block_three:
        page.wait_for_timeout(800)
    else:
        _wait_for(page, "window.__halo !== undefined")
        page.wait_for_timeout(500)
    return ctx, page, errors


def _wait_for(page, js, timeout_s=10.0):
    """wait_for_function without its eval: the page's CSP has no 'unsafe-eval'."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.evaluate(js):
            return
        page.wait_for_timeout(100)
    raise TimeoutError(js)


def check_tools(port: int, only: str | None = None) -> int:
    """
    Every tool path and the edge cases, on the real page, with probes.

    One bus and one server throughout; each context gets the tools script from
    a fresh connection, so every run is also a page opened onto a session with
    history in its replay ring.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("--check needs Playwright: uv run --no-project --with playwright python tools/demo_live_ui.py --check")
        return 2
    os.makedirs(SHOTS, exist_ok=True)
    failures: list[str] = []
    bus, live = start(port)
    url = f"http://127.0.0.1:{live._server.server_address[1]}/{TOKEN}/?debug"

    def play(page, name, script, include_text=True):
        def snap(label):
            page.screenshot(path=os.path.join(SHOTS, f"tools-{name}-{label}.png"), full_page=label.startswith("rail"))
            scroll = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            if scroll > 0:
                failures.append(f"{name}/{label}: horizontal scroll of {scroll}px")

        def probe(label, js, predicate, message):
            value = page.evaluate(js)
            try:
                good = predicate(value)
            except Exception as exc:  # noqa: BLE001 - a probe that cannot read is a failure
                good, value = False, f"{value!r} ({exc})"
            if not good:
                failures.append(f"{name}/{label}: {message}; got {value!r}"[:400])

        steps = script(Script(Voice(include_text)))
        run_steps(bus, steps, lambda sec: page.wait_for_timeout(int(sec * 1000)), snap, None, probe)

    with sync_playwright() as pw:
        browser = _launch(pw)
        runs = [("390-dark", 390, 844, "dark"), ("1280-dark", 1280, 900, "dark"), ("1280-light", 1280, 900, "light")]
        for name, w, h, scheme in runs:
            if only and only not in name:
                continue
            ctx, page, errors = _open(browser, url, w, h, scheme)
            play(page, name, lambda sc: sc.build_tools())
            leftover = page.evaluate("({sats: window.__halo.satCount, dom: document.querySelectorAll('#halo-sats .sat').length,"
                                     " tethers: window.__halo.tethers,"
                                     " entries: window.__tools.entries})")
            if any(leftover.values()):
                failures.append(f"{name}: left behind after the script {leftover}")
            if errors:
                failures.append(f"{name}: console errors {errors[:3]}")
            print(f"tools {name}: leftovers {leftover}, {len(errors)} console errors")
            ctx.close()

        if not only or only == "edge":
            failures += _check_tool_edges(browser, bus, live, url, play)
        browser.close()
    live.stop()
    if failures:
        print("FAILED")
        for f in failures:
            print("  " + f)
        return 1
    print(f"OK: screenshots in {SHOTS}")
    return 0


def _short_turn(sc, tools, *, end=True, text="x"):
    """A turn with the given (name, action, target, ok, seconds) tools."""
    sc.wake(text, chained=True)
    for name, action, target, ok, seconds in tools:
        sc.call_tool(name, action, target, "ok", ok=ok, seconds=seconds, ms=int(seconds * 1000))
    if end:
        sc.end("reply", 3000)
    return sc.steps


def _check_tool_edges(browser, bus, live, url, play) -> list[str]:
    failures: list[str] = []
    sats_js = "window.__halo ? window.__halo.sats : []"

    # Reduced motion: satellites still appear, show their state and go.
    ctx, page, errors = _open(browser, url, 390, 844, reduced=True)

    def reduced_script(sc):
        sc.wake("reduced")
        call = sc.tool_start("control_tv", "turn_on")
        sc.at(1.2, "snap", "running")
        sc.probe("reduced running", sats_js, lambda v: len(v) == 1 and v[0]["alpha"] > 0.9, "a satellite shows at once with reduced motion")
        sc.tool_end(call, "on", ok=True, ms=5000, wait_s=0.2)
        sc.at(0.6, "snap", "docked")
        sc.end("reply", 3000)
        sc.probe("reduced gone", sats_js, lambda v: len(v) == 0, "a docked satellite goes with reduced motion too", wait_s=1.5)
        return sc.steps
    play(page, "390-reduced", reduced_script)
    failures += [f"reduced: {e}" for e in errors[:3]]
    ctx.close()

    # include_transcripts false: no label anywhere, even if one were sent.
    live.stop()
    bus2, live2 = start(live.port, include_text=False, bus=bus)
    ctx, page, errors = _open(browser, url, 390, 844)

    def notext_script(sc):
        sc.wake("private")
        call = sc.tool_start("control_whatsapp", "whatsapp_send", "to Marta")
        # A target the server would never send with transcripts off: the page drops it anyway.
        sc.pub("turn_tool_start", {"turn": sc.turn, "name": "control_tv", "action": "stremio_play",
                                   "start_ms": 99, "target": "SECRET-LABEL"})
        sc.at(0.8, "snap", "running")
        sc.probe("no labels", "[...document.querySelectorAll('#halo-sats .tag b, #tools small')].map(n => n.textContent).join('|')",
                 lambda v: "Marta" not in v and "SECRET" not in v, "no target text with include_transcripts false")
        sc.tool_end(call, "sent", ok=True, ms=1000, wait_s=0.3)
        sc.end("reply", 3000)
        return sc.steps
    play(page, "390-notext", notext_script, include_text=False)
    failures += [f"notext: {e}" for e in errors[:3]]
    ctx.close()
    live2.stop()
    bus, live = start(live.port, bus=bus)

    # No WebGL (three.js blocked): the rail alone carries the tools, no errors.
    ctx, page, errors = _open(browser, url, 390, 844, block_three=True)

    def nogl_script(sc):
        sc.wake("no gl")
        call = sc.tool_start("control_tv", "stremio_play", "Fallout S2E9")
        sc.at(0.8, "snap", "running")
        sc.probe("rail row", "document.querySelectorAll('#tools li').length", lambda v: v == 1, "the rail shows the call without WebGL")
        sc.tool_end(call, "playing", ok=True, ms=20000, wait_s=0.2)
        sc.at(0.3, "snap", "done")
        sc.probe("rail done", "document.querySelector('#tools li').dataset.st", lambda v: v == "ok", "the rail row ends ok")
        sc.end("reply", 3000)
        return sc.steps
    play(page, "390-nogl", nogl_script)
    failures += [f"nogl: {e}" for e in errors if "ERR_FAILED" not in e][:3]   # the block itself logs one
    ctx.close()

    # Ambient: the satellites are part of the halo, so they stay.
    ctx, page, errors = _open(browser, url.replace("?debug", "?debug#ambient"), 390, 844)

    def ambient_script(sc):
        sc.wake("ambient")
        sc.tool_start("control_tv", "turn_on")
        sc.tool_start("control_lights", "light_on", "attic", wait_s=0.2)
        sc.at(1.0, "snap", "two")
        sc.probe("ambient sats", "document.querySelectorAll('#halo-sats .sat').length", lambda v: v == 2, "satellites show in ambient")
        sc.end("reply", 3000)
        return sc.steps
    play(page, "390-ambient", ambient_script)
    ctx.close()

    # Opened mid-turn: a running tool from the replay ring comes up already aged,
    # finished ones from earlier turns do not replay their docks.
    sc = Script(Voice(True))
    _short_turn(sc, [("control_lights", "light_on", "attic", True, 0.1)], text="earlier")
    sc.wake("mid-turn")
    running = sc.tool_start("control_tv", "turn_on")
    run_steps(bus, sc.steps, time.sleep)
    time.sleep(3.0)
    ctx, page, errors = _open(browser, url, 1280, 900)
    sats = page.evaluate(sats_js)
    page.screenshot(path=os.path.join(SHOTS, "tools-1280-midturn-open.png"))
    if not (len(sats) == 1 and sats[0]["st"] == "run"):
        failures.append(f"mid-turn open: expected one running satellite, got {sats}")
    else:
        aged = sats[0]["note"].split(" ")[0]
        if not aged.endswith("s") or float(aged[:-1]) < 2.5:
            failures.append(f"mid-turn open: the replayed call should already be ~3s old, shows {sats[0]['note']}")

    # Reconnect mid-tool: drop the server, end the tool while the page is away,
    # bring it back on the same bus; Last-Event-ID resumes and the call ends.
    live.stop()
    sc.steps = []
    sc.tool_end(running, "on", ok=True, ms=8000)
    sc.end("reply", 9000)
    run_steps(bus, sc.steps, time.sleep)
    bus, live = start(live.port, bus=bus)
    page.wait_for_timeout(6000)
    after = page.evaluate(sats_js)
    page.screenshot(path=os.path.join(SHOTS, "tools-1280-after-reconnect.png"))
    if after:
        failures.append(f"reconnect: the call ended while the page was away should not linger, got {after}")
    entries = page.evaluate("window.__tools.entries")
    if entries:
        failures.append(f"reconnect: {entries} tool entries left behind")
    failures += [f"midturn/reconnect: {e}" for e in errors if "events" not in e][:3]

    # Performance: frame time with four running satellites on swiftshader.
    def frame_stats(seconds=3.0):
        return page.evaluate("""(ms) => new Promise((done) => {
            const t = []; let last = performance.now(); const end = last + ms;
            function f(now) { t.push(now - last); last = now; if (now < end) requestAnimationFrame(f); else {
              t.sort((a, b) => a - b);
              done({ n: t.length, mean: t.reduce((a, b) => a + b, 0) / t.length, p95: t[Math.floor(t.length * 0.95)] }); } }
            requestAnimationFrame(f); })""", int(seconds * 1000))
    sc.steps = []
    sc.wake("perf")
    run_steps(bus, sc.steps, time.sleep)
    page.wait_for_timeout(800)
    base = frame_stats()
    sc.steps = []
    for name, action, target in (("control_tv", "turn_on", None), ("control_lights", "light_on", "attic"),
                                 ("control_vacuum", "vacuum_clean_all", "everywhere"), ("web_search", "search", "weather")):
        sc.tool_start(name, action, target)
    run_steps(bus, sc.steps, time.sleep)
    page.wait_for_timeout(1000)
    four = frame_stats()
    print(f"frame time, swiftshader 1280x900: none {base['mean']:.1f}ms mean / {base['p95']:.1f}ms p95, "
          f"four satellites {four['mean']:.1f}ms / {four['p95']:.1f}ms ({four['n']} frames)")
    if four["mean"] > base["mean"] * 1.5 + 4:
        failures.append(f"perf: four satellites cost too much ({base} -> {four})")
    sc.steps = []
    sc.end("reply", 5000)
    run_steps(bus, sc.steps, time.sleep)
    page.wait_for_timeout(1500)

    # A long session, compressed: 180 turns (about 30 minutes of use) with
    # tool calls of every outcome. Nothing may grow.
    def census():
        # The session feed (200 turns) and the log (300 lines) are capped lists
        # that grow by design; everything else must stay flat.
        return page.evaluate("""() => ({ dom: document.getElementsByTagName('*').length
              - document.querySelectorAll('#turns *, #log-lines *, #hits *').length,
            feed: document.getElementById('turns').children.length,
            sats: window.__halo.satCount, entries: window.__tools.entries, rows: window.__tools.rows,
            heap: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1e6) : -1 })""")
    first = census()
    kinds = [("control_lights", "light_on", "attic", True), ("control_tv", "get_status", None, None),
             ("control_whatsapp", "whatsapp_send", "to Marta", None), ("control_vacuum", "vacuum_dock", None, False),
             ("web_search", "search", "news", True)]
    t0 = time.time()
    for i in range(180):
        sc.steps = []
        sc.wake(f"turn {i}", chained=True)
        name, action, target, ok = kinds[i % len(kinds)]
        awaiting = name == "control_whatsapp"
        call = sc.tool_start(name, action, target)
        if i % 17 != 0:   # every 17th call never answers
            sc.tool_end(call, "x", ok=ok, awaiting=awaiting, ms=500)
        sc.end("reply", 2000)
        run_steps(bus, sc.steps, lambda s: None)
        page.wait_for_timeout(40)
        if i == 90:
            mid = census()
    page.wait_for_timeout(3000)
    last = census()
    fps = frame_stats(2.0)
    print(f"long session ({time.time() - t0:.0f}s, 180 turns): start {first}, mid {mid}, end {last}, "
          f"frames {fps['mean']:.1f}ms mean")
    if last["sats"] > 1 or last["entries"] > 1 or last["dom"] > first["dom"] + 40:
        failures.append(f"long session: growth {first} -> {last}")
    page.screenshot(path=os.path.join(SHOTS, "tools-1280-long-session-end.png"))
    ctx.close()
    live.stop()
    return failures


def _check_ambient_and_fallback(browser, url: str) -> list[str]:
    """Ambient mode shows only the halo; without three.js the page falls back to the pill."""
    failures = []
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
    page = ctx.new_page()
    page.goto(url.replace("?debug", "?debug#ambient"))
    page.wait_for_function("window.__halo !== undefined", timeout=10000)
    page.wait_for_timeout(500)
    page.screenshot(path=os.path.join(SHOTS, "390x844-dark-ambient.png"))
    visible = page.evaluate("""() => [...document.body.children]
        .filter((n) => n.tagName !== 'SCRIPT' && getComputedStyle(n).display !== 'none')
        .map((n) => n.id || n.className)""")
    if visible != ["halo"]:
        failures.append(f"ambient: expected only the halo visible, got {visible}")
    page.click("#halo-full")
    if page.evaluate("document.body.classList.contains('ambient')") is not False:
        failures.append("ambient: the button did not leave ambient mode")
    ctx.close()

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/vendor/three.min.js", lambda route: route.abort())
    page.goto(url)
    page.wait_for_timeout(800)
    page.screenshot(path=os.path.join(SHOTS, "390x844-dark-nogl.png"))
    state = page.evaluate("""() => ({
        pill: getComputedStyle(document.getElementById('state')).display,
        line: !document.getElementById('halo-nogl').hidden,
        hasHalo: document.body.classList.contains('has-halo')})""")
    if state["pill"] == "none" or not state["line"] or state["hasHalo"]:
        failures.append(f"fallback: without three.js expected the pill and the line, got {state}")
    if errors:
        failures.append(f"fallback: page errors {errors[:3]}")
    print(f"ambient + fallback: {state}")
    ctx.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8799, help="default 8799, clear of California's 8765")
    parser.add_argument("--loop", action="store_true", help="play the turns again and again")
    parser.add_argument("--no-text", action="store_true", help="as with logging.include_transcripts: false")
    parser.add_argument("--check", action="store_true", help="headless Playwright pass over the page")
    parser.add_argument("--only", choices=("halo", "tools", "edge", "390", "1280"),
                        help="with --check: just the halo pass, the tools pass, its edge cases, or one width")
    parser.add_argument("--tools", action="store_true", help="play the tool script instead of the three turns")
    args = parser.parse_args()
    if args.check:
        code = 0
        if args.only in (None, "halo"):
            code = check(args.port, include_text=not args.no_text)
        if args.only != "halo":
            only = None if args.only in (None, "tools") else args.only
            code = check_tools(args.port + 1, only) or code
        return code

    bus, live = start(args.port)
    print(f"Live UI demo: {live.url}   (Ctrl-C to stop)")
    try:
        while True:
            script = Script(Voice(include_text=not args.no_text))
            steps = script.build_tools() if args.tools else script.build()
            run_steps(bus, steps, time.sleep)
            if not args.loop:
                print("Done. The page stays up; Ctrl-C to stop.")
                while True:
                    time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        live.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
