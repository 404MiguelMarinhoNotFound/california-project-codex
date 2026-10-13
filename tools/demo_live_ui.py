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

TOKEN = "demo"
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
        self.turn += 1
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
def start(port: int):
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
    bus = EventBus()
    live = LiveUI(config, bus)
    if not live.start():
        sys.exit(f"Could not listen on 127.0.0.1:{port}")
    return bus, live


def run_steps(bus, steps, wait, on_snap=None, on_stop=None):
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
        browser.close()
    live.stop()
    if failures:
        print("FAILED")
        for f in failures:
            print("  " + f)
        return 1
    print(f"OK: screenshots in {SHOTS}")
    return 0


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
    args = parser.parse_args()
    if args.check:
        return check(args.port, include_text=not args.no_text)

    bus, live = start(args.port)
    print(f"Live UI demo: {live.url}   (Ctrl-C to stop)")
    try:
        while True:
            steps = Script(Voice(include_text=not args.no_text)).build()
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
