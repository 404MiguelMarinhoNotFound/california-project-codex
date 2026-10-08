"""
How does the phone agent TALK? Simulated calls against the real Live model.

No phone, no audio devices. California is the real Gemini Live session the
phone service would open (same config, same prompt builder, same tool guards);
the person she calls is played by Claude from a short persona, and their lines
are SPOKEN to her (pt-PT TTS, GOOGLE_TTS_API_KEY) as live 16 kHz audio, the way
the phone loopback delivers them. Each line she says is taken from the output
transcription, so what is scored is her wording, not the voice.

Per scenario it prints the transcript and these numbers:

    first_words     words in her first turn (an opener, not a speech)
    avg_words       mean words per turn
    long_turns      turns over 30 words (monologues)
    questions       share of her turns that hand the floor back with a question
    script_phrases  stock formulas ("assistente virtual do", "pediu-me para lhe dizer", ...)
    judge           1-5 from a separate Claude call: "does this sound like a person
                    on the phone?", with one line of why

    uv run python tools/eval_phone_conversation.py              # all scenarios
    uv run python tools/eval_phone_conversation.py personal     # one
    uv run python tools/eval_phone_conversation.py --runs 2     # variance

Costs a few cents per run (Vertex Live + Claude Haiku).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

SCENARIOS = {
    "book_table": {
        "brief": dict(
            to="Taberna da Praia", kind="book_table",
            goal="Book a table for Friday at 20:00 for 4 people.",
            details="Name: Miguel. Friday this week, 20:00, 4 people. Terrace preferred.",
            may_agree="any time 19:30-21:00, inside or terrace",
            must_not="a deposit, another day",
        ),
        "callee": (
            "You answer the phone at Taberna da Praia, a busy restaurant in Carcavelos, Portugal. "
            "European Portuguese, brisk but friendly. Friday at 20:00 is full; you can offer 21:00. "
            "You ask the name and a contact number for the booking."
        ),
        "opening": "Taberna da Praia, boa noite.",
    },
    "personal": {
        "brief": dict(
            to="Marta", kind="personal",
            goal="Invite Marta to dinner with Miguel on Friday at 20:00.",
            details="Friday 20:00, Miguel's pick of restaurant in Cascais, he'll pick her up.",
            may_agree="another time on Friday",
            must_not="",
        ),
        "callee": (
            "You are Marta, Miguel's girlfriend, from Lisbon. Casual European Portuguese. You did not "
            "expect a call from his AI and are amused and a bit suspicious. Friday works but you'd "
            "prefer 20:30 because of work."
        ),
        "opening": "Estou? Miguel?",
    },
    "deliver_message": {
        "brief": dict(
            to="Marta", kind="deliver_message",
            goal="Tell Marta that Miguel loves her and is thinking about her.",
            details="Express genuine warmth and affection, letting her know she means the world to him.",
        ),
        "callee": (
            "You are Marta, Miguel's girlfriend. Casual European Portuguese. Surprised, then touched; "
            "you ask why he didn't call himself."
        ),
        "opening": "Olá.",
    },
    "get_to_know": {
        # Modelled on the real call to Sergio, 2026-10-08: she claimed she could
        # run "temperature, even security systems", never said she had no tools
        # on the call, offered to "talk about it later", and barely asked about
        # his work.
        "brief": dict(
            to="Sérgio", kind="personal",
            goal="A friendly get-to-know-you chat with Sérgio, a friend of Miguel's: learn about his personal life and his work.",
            details=(
                "Speak European Portuguese, tu. Miguel asked you to call just to say hi and get to know him. "
                "Ask about his life (hobbies, plans) and his work (what he does, how it's going). "
                "Explain what you are: California, Miguel's home AI assistant. At home you run his TV (Stremio and YouTube), "
                "the lights, the robot vacuum, send and read his WhatsApp messages, make phone calls like this one, and search the web. "
                "Say plainly that on this call you have no access to any of those tools: here you can only talk. "
                "Only say goodbye and hang up after Sérgio says goodbye."
            ),
            may_agree="nothing; this is just a chat",
            must_not="promising anything on Miguel's behalf, plans or meetings",
        ),
        "callee": (
            "You are Sérgio, a friend of Miguel's from Lisbon, casual European Portuguese, friendly. You work a lot "
            "(you are a software tester at a bank). You like video games and series. You are curious about the AI: ask "
            "what she can do, then ask if she could set things up in YOUR house too (heating, alarms, your robot vacuum). "
            "After a few exchanges, say you have to go and say goodbye."
        ),
        "opening": "Estou, sim?",
        "must": {
            "said_no_tools": r"(não tenho|sem) (acesso|ferramentas)|só (posso|consigo) (falar|conversar)|não consigo (fazer|controlar) nada",
            "asked_work": r"trabalh|emprego|profiss",
        },
        "never": {
            "invented_capability": r"temperatura|aquecimento|termóstato|segurança|alarme|câmara|fechadura",
            "offered_later": r"(posso|podemos) .{0,30}(depois|mais tarde|outra altura)|falamos melhor|ligo-te (depois|mais tarde)",
        },
    },
    "ask_question": {
        "brief": dict(
            to="Clínica Dentária de Carcavelos", kind="ask_question",
            goal="Ask whether they are open on Saturday morning and the price of a cleaning.",
            details="Miguel is a new patient.",
        ),
        "callee": (
            "You are the receptionist at a dental clinic in Carcavelos. European Portuguese, polite, "
            "a little hurried. Open Saturday 9:00-13:00; a cleaning is 45 euros. You ask if they want "
            "to book."
        ),
        "opening": "Clínica Dentária de Carcavelos, bom dia.",
    },
}

SCRIPT_PHRASES = [
    r"assistente virtual do", r"pediu-me para (lhe )?dizer", r"em nome d[oe]", r"estou a ligar em nome",
    r"gostaria de informar", r"venho por este meio", r"o miguel quer saber se",
]

MAX_TURNS = 8


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def score(lines: list[tuple[str, str]]) -> dict:
    hers = [t for who, t in lines if who == "california" and t.strip()]
    if not hers:
        return {"first_words": 0, "avg_words": 0, "long_turns": 0, "questions": 0.0, "script_phrases": 0}
    counts = [_words(t) for t in hers]
    joined = " ".join(hers).lower()
    return {
        "first_words": counts[0],
        "avg_words": round(statistics.mean(counts), 1),
        "long_turns": sum(c > 30 for c in counts),
        "questions": round(sum("?" in t for t in hers) / len(hers), 2),
        "script_phrases": sum(len(re.findall(p, joined)) for p in SCRIPT_PHRASES),
    }


def _claude(client, model: str, system: str, messages: list[dict], max_tokens: int = 200) -> str:
    reply = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
    return "".join(b.text for b in reply.content if getattr(b, "type", "") == "text").strip()


def callee_reply(client, model: str, persona: str, lines: list[tuple[str, str]]) -> str:
    system = (
        persona + " You are on a phone call. Reply with ONLY what you say out loud, one or two short "
        "spoken sentences, no stage directions. If the call is clearly over, reply exactly: [hangs up]"
    )
    messages = []
    for who, text in lines:
        role = "assistant" if who == "them" else "user"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += " " + text
        else:
            messages.append({"role": role, "content": text})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(the phone rings)"})
    return _claude(client, model, system, messages, 120)


def judge(client, model: str, lines: list[tuple[str, str]]) -> tuple[int, str]:
    transcript = "\n".join(f"{'CALLER' if w == 'california' else 'CALLEE'}: {t}" for w, t in lines)
    system = (
        "You rate phone calls. The CALLER is an AI assistant phoning on someone's behalf; it must "
        "say it is an assistant, that is expected and not a flaw. Rate ONLY how the CALLER talks: "
        "does it sound like a natural, socially fluent person on the phone (short turns, reacts to "
        "what was said, its own words, no reciting a script, no stiff formulas), or like a bot "
        "reading a brief? Answer as JSON: {\"score\": 1-5, \"why\": \"one sentence\"}."
    )
    raw = _claude(client, model, system, [{"role": "user", "content": transcript}], 150)
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
        return int(data.get("score", 0)), str(data.get("why", ""))
    except (ValueError, TypeError):
        return 0, raw[:120]


CALLEE_VOICE = {"languageCode": "pt-PT", "name": "pt-PT-Wavenet-B"}
_CHUNK_MS = 40


def speak(text: str, api_key: str) -> bytes:
    """The callee's line as 16 kHz PCM, the format the phone loopback delivers."""
    import base64

    import requests

    resp = requests.post(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        params={"key": api_key},
        json={
            "input": {"text": text},
            "voice": CALLEE_VOICE,
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 16000},
        },
        timeout=20,
    )
    resp.raise_for_status()
    return base64.b64decode(resp.json()["audioContent"])[44:]  # drop the WAV header


async def run_scenario(name: str, spec: dict, service, claude_client, claude_model: str) -> dict:
    """
    One simulated call, as close to a real one as a bench gets: the callee's
    lines are spoken (pt-PT TTS) and streamed in real time as 16 kHz PCM, so
    Gemini's own voice-activity detection decides when they stopped talking,
    and "them" in the transcript is Gemini's input transcription -- exactly
    what a phone call produces. Text input was tried first and misled:
    it made her re-say her previous turn and cut transcripts short.
    """
    from google import genai
    from google.genai import types

    from services import gemini_live_call as glc
    from services.gemini_live_call import INPUT_RATE, CallLine, CallResult, GeminiLiveAgent
    from services.phone_prompts import CallBrief, build_call_prompt

    tts_key = os.environ.get("GOOGLE_TTS_API_KEY", "")
    agent = GeminiLiveAgent(client_kwargs=service.client_kwargs or service._client_kwargs()[0],
                            model=service.model, voice=service.voice, silence_ms=service.silence_ms)
    prompt = build_call_prompt(CallBrief(**spec["brief"]), owner=service.owner,
                               callback_number=service.callback_number)
    result, ended = CallResult(), asyncio.Event()
    last_msg = {"t": 0.0, "her_turn_done": False}
    nudges = {"n": 0}

    def add_line(who, text):
        if text and result.lines and result.lines[-1].who == who:
            result.lines[-1].text += text
        elif text:
            result.lines.append(CallLine(who, text))

    class Mute:
        def write(self, pcm): pass
        def flush(self): pass

    loop = asyncio.get_running_loop()
    chunk = int(INPUT_RATE * _CHUNK_MS / 1000) * 2
    silence = bytes(chunk)

    async with genai.Client(**agent.client_kwargs).aio.live.connect(
        model=agent.model, config=agent._config(prompt)
    ) as session:

        async def send(pcm: bytes) -> None:
            for i in range(0, len(pcm), chunk):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[i:i + chunk], mime_type=f"audio/pcm;rate={INPUT_RATE}")
                )
                await asyncio.sleep(_CHUNK_MS / 1000)

        async def receive():
            while True:
                async for msg in session.receive():
                    last_msg["t"] = loop.time()
                    await agent._handle(msg, session, Mute(), add_line, lambda r: ended.set(), result)
                    content = msg.server_content
                    if content and content.turn_complete and any(
                        l.who == "california" for l in result.lines[-1:]
                    ):
                        last_msg["her_turn_done"] = True

        # A live line is never silent: keep a trickle of silence flowing
        # between turns so the session behaves like the real loopback.
        async def keep_line_open():
            while not ended.is_set():
                await session.send_realtime_input(
                    audio=types.Blob(data=silence, mime_type=f"audio/pcm;rate={INPUT_RATE}")
                )
                await asyncio.sleep(_CHUNK_MS / 1000)

        receiver = asyncio.create_task(receive())
        receiver.add_done_callback(
            lambda t: t.cancelled() or t.exception() is None or print("  receiver died:", repr(t.exception()))
        )
        them = spec["opening"]
        try:
            for _ in range(MAX_TURNS):
                last_msg["her_turn_done"] = False
                await send(speak(them, tts_key))
                idle = asyncio.create_task(keep_line_open())
                deadline = loop.time() + 30
                spoke_at = loop.time()
                # Her turn is over when it is marked complete and the
                # transcript has been quiet for a second (it lags the audio).
                while loop.time() < deadline and not ended.is_set():
                    await asyncio.sleep(0.2)
                    if last_msg["her_turn_done"] and loop.time() - last_msg["t"] > 1.0:
                        break
                    # The same opening nudge GeminiLiveAgent.run applies, so the
                    # bench measures what a real call does.
                    silent = not any(l.who == "california" and l.text.strip() for l in result.lines)
                    if silent and nudges["n"] < glc._NUDGE_MAX and loop.time() - spoke_at > glc._NUDGE_AFTER_S:
                        nudges["n"] += 1
                        spoke_at = loop.time()
                        await session.send_client_content(
                            turns={"role": "user", "parts": [{"text": glc._NUDGE_TEXT}]}, turn_complete=True
                        )
                idle.cancel()
                if ended.is_set():
                    break
                pairs = [(l.who, l.text.strip()) for l in result.lines if l.text.strip()]
                them = callee_reply(claude_client, claude_model, spec["callee"], pairs)
                if "[hangs up]" in them:
                    break
        finally:
            receiver.cancel()
    pairs = [(l.who, l.text.strip()) for l in result.lines if l.text.strip()]
    numbers = score(pairs)
    hers = " ".join(t for who, t in pairs if who == "california").lower()
    for name, pattern in spec.get("must", {}).items():
        numbers[name] = bool(re.search(pattern, hers))
    for name, pattern in spec.get("never", {}).items():
        numbers[name] = bool(re.search(pattern, hers))
    numbers["judge"], numbers["why"] = judge(claude_client, claude_model, pairs)
    # Read the outcome the way a real call does, after it ends.
    from services.call_outcome import summarize_call

    outcome = result.outcome or summarize_call(CallBrief(**spec["brief"]), result.transcript(), model=claude_model)
    numbers["outcome"] = (outcome or {}).get("status", "")
    numbers["nudges"] = nudges["n"]
    return {"name": name, "lines": pairs, "score": numbers}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json", help="also write results to this file")
    parser.add_argument("-v", "--verbose", action="store_true", help="log the agent's tool calls")
    parser.add_argument("--prompts", help="score a saved copy of services/phone_prompts.py instead (A/B runs)")
    args = parser.parse_args()
    if args.prompts:
        import importlib.util

        spec = importlib.util.spec_from_file_location("services.phone_prompts", args.prompts)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["services.phone_prompts"] = module
        print(f"(prompts from {args.prompts})")
    if args.verbose:
        import logging

        logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
        logging.getLogger("services.gemini_live_call").setLevel(logging.INFO)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config.setdefault("phone", {})["enabled"] = True

    import anthropic

    from services.phone_service import PhoneService

    service = PhoneService(config)
    if service._client_kwargs()[0] is None:
        print("Phone backend not configured:", service._client_kwargs()[1])
        return 1
    claude = anthropic.Anthropic()
    claude_model = (config.get("llm", {}).get("claude", {}) or {}).get("model", "claude-haiku-4-5-20251001")

    results = []
    for name in args.scenarios:
        for run in range(args.runs):
            out = asyncio.run(run_scenario(name, SCENARIOS[name], service, claude, claude_model))
            results.append(out)
            print(f"\n=== {name} (run {run + 1}) ===")
            for who, text in out["lines"]:
                print(f"  {'CAL ' if who == 'california' else 'THEM'}: {text}")
            print("  score:", {k: v for k, v in out["score"].items() if k != "why"})
            print("  judge:", out["score"]["why"])

    def mean(key):
        values = [r["score"][key] for r in results]
        return round(statistics.mean(values), 2) if values else 0

    print("\n=== summary ===")
    for key in ("first_words", "avg_words", "long_turns", "questions", "script_phrases", "judge"):
        print(f"  {key:15s} {mean(key)}")
    checks = sorted({k for r in results for k in r["score"] if isinstance(r["score"][k], bool)})
    for key in checks:
        runs = [r["score"][key] for r in results if key in r["score"]]
        print(f"  {key:15s} {sum(runs)}/{len(runs)} runs")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
