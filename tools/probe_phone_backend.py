"""
Check the phone agent's Gemini backend without placing a call.

Connects to Gemini Live with exactly what PhoneService would use (config.yaml
`phone:` + .env credentials), plays the callee's opening line as text, and
reports time to first audio and what she said back. Costs a fraction of a cent.

    uv run python tools/probe_phone_backend.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config.setdefault("phone", {})["enabled"] = True

    from services.gemini_live_call import GeminiLiveAgent
    from services.phone_prompts import CallBrief, build_call_prompt
    from services.phone_service import PhoneService

    service = PhoneService(config)
    kwargs, why = service._client_kwargs()
    if kwargs is None:
        print(f"Not configured: {why}")
        return 1
    shown = {k: ("<set>" if k == "api_key" else v) for k, v in kwargs.items()}
    print(f"backend={service.backend} model={service.model} client={shown}")

    agent = GeminiLiveAgent(client_kwargs=kwargs, model=service.model, voice=service.voice)
    prompt = build_call_prompt(CallBrief(
        to="Taberna da Praia", kind="book_table", goal="mesa sexta-feira às 20h para 4 pessoas",
        details="em nome de Miguel",
    ))

    async def run() -> None:
        from google import genai

        from services.gemini_live_call import CallLine, CallResult

        class CountingSpeaker:
            def __init__(self):
                self.samples, self.first = 0, None

            def write(self, pcm):
                self.first = self.first if self.first is not None else time.time() - sent
                self.samples += len(pcm) // 2

            def flush(self):
                pass

        result, speaker, done = CallResult(), CountingSpeaker(), asyncio.Event()

        def add_line(who, text):
            if text and result.lines and result.lines[-1].who == who:
                result.lines[-1].text += text
            elif text:
                result.lines.append(CallLine(who, text))

        client = genai.Client(**kwargs)
        started = time.time()
        async with client.aio.live.connect(model=service.model, config=agent._config(prompt)) as session:
            print(f"connected in {time.time() - started:.2f}s")
            # The callee's opening, as the loopback would hear it, counted as theirs.
            add_line("them", "Estou? Taberna da Praia, boa noite.")
            sent = time.time()
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": "Estou? Taberna da Praia, boa noite."}]},
                turn_complete=True,
            )

            async def receive():
                while True:
                    async for msg in session.receive():
                        # The same handler a real call uses, early-hang-up guard included.
                        await agent._handle(msg, session, speaker, add_line, lambda r: done.set(), result)
                        content = msg.server_content
                        if content and content.turn_complete and speaker.samples:
                            done.set()

            task = asyncio.create_task(receive())
            try:
                await asyncio.wait_for(done.wait(), 20)
            except asyncio.TimeoutError:
                pass
            task.cancel()
        if speaker.first is not None:
            print(f"first audio after {speaker.first:.2f}s, {speaker.samples / 24000:.1f}s of speech")
        else:
            print("no audio came back")
        print("she said:", " ".join(l.text.strip() for l in result.lines if l.who == "california"))
        if result.outcome:
            print("outcome recorded:", result.outcome)

    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
