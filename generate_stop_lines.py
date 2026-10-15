"""
Generate the two lines California says when she stops listening for an answer.

After a reply that asks Master Miguel something, the mic stays open for a short
window so he can answer without the wake word (see "Answering Her Questions
Without The Wake Word" in CLAUDE.md). She says one of these when she gives up:

    timeout  a window in a chain went unanswered
    cap      conversation.max_follow_ups follow-ups in a row

Written under one directory PER VOICE, like the other generated clips, so
switching `tts.provider` / voice never overwrites another voice's set:

    sounds/stop_lines/<provider>_<voice>/timeout.wav, cap.wav, manifest.json

Point `sounds.stop_lines_dir` in config.yaml at the folder you want played.
They are pre-recorded on purpose and never synthesized at runtime; without them
she simply goes quiet.

Run it after editing STOP_LINES, after changing the TTS voice, and once on a
fresh clone: the generated WAVs are deliberately not committed, matching
sounds/bootup/ and sounds/california_activations/.

    uv run python generate_stop_lines.py

Siblings: generate_activation_phrases.py (whose trim_silence this reuses) and
generate_bootup_sounds.py.
"""

import json
import os

import soundfile as sf
import yaml
from dotenv import load_dotenv

OUTPUT_ROOT = "sounds/stop_lines"

STOP_LINES = {
    "timeout": "I'll take that as a no. Say my name if you need me.",
    "cap": "Okay, I'll stop listening now. Say California if you need me.",
}


def main():
    from generate_activation_phrases import trim_silence
    from services.tts import TTSService

    load_dotenv()
    with open("config.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    tts = TTSService(config)

    output_dir = os.path.join(OUTPUT_ROOT, tts.voice_slug())
    os.makedirs(output_dir, exist_ok=True)
    print(f"Voice: {tts.voice_slug()}")

    written = {}
    for name, text in STOP_LINES.items():
        try:
            raw, sample_rate = tts.synthesize(text)
            audio = trim_silence(raw, sample_rate)
            sf.write(os.path.join(output_dir, f"{name}.wav"), audio, sample_rate)
        except Exception as exc:
            print(f"  ✗  {name} failed: {exc}")
            continue
        written[name] = text
        print(f"  ✓  {len(audio) / sample_rate:4.2f}s  {name}.wav  —  \"{text}\"")

    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(written, handle, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(written)} files and manifest.json saved to /{output_dir}/")
    print(f'Point config.yaml at it:  sounds.stop_lines_dir: "{output_dir.replace(os.sep, "/")}"')


if __name__ == "__main__":
    main()
