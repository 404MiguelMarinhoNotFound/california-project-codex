"""
Generate the boot-up one-liners California plays at startup.

Synthesizes every line in BOOTUP_LINES with Kokoro af_bella and writes one WAV
per line into sounds/bootup/. `Orchestrator._play_bootup_sound` picks one of
them at random on each launch, so the assistant does not greet Master Miguel
the same way twice.

Run it after editing BOOTUP_LINES, and once on a fresh clone: the generated
WAVs are deliberately not committed, matching sounds/chime.wav and
sounds/error.wav. This repo carries audio *sources*, not audio output. Without
them the orchestrator logs "No bootup sounds found" and starts silently, which
is harmless but loses the greeting.

Kokoro is an optional extra, so this needs it installed:

    uv sync --extra default          # or: uv sync --extra kokoro
    uv run python generate_bootup_sounds.py

TTS settings are forced to kokoro / af_bella / speed 1.0 regardless of what
config.yaml selects, so the sound set stays consistent even when the live TTS
provider is switched to edge or elevenlabs.

Sibling script: generate_activation_phrases.py, which does the same job for the
post-wake-word acknowledgements.
"""

import os
import sys
import yaml
import soundfile as sf
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from services.tts import TTSService

BOOTUP_LINES = {
    # Cold, clean boot
    "california_live":          "California. Live.",
    "systems_nominal":          "All systems nominal. Let's go.",
    "online_and_sharp":         "Online, sharp, and not in the mood for nonsense.",
    "booted_clean":             "Booted clean. Whenever you're ready, Master Miguel.",

    # Attitude
    "state_your_purpose":       "Systems live. State your purpose.",
    "try_to_keep_up":           "California online. Try to keep up.",
    "dont_waste_my_time":       "I'm up. Let's not waste each other's time.",
    "better_be_worth_it":       "Systems live. This better be worth waking me up for.",
    "oh_its_starting":          "Oh, we're doing this. Okay. I'm ready.",

    # Warm / familiar
    "back_in_business":         "Back in business. What are we doing, Master Miguel?",
    "good_to_go":               "Good to go. I'm listening.",
    "welcome_back":             "Welcome back. California's up and running.",
    "ready_when_you_are":       "Ready when you are.",
    "im_up":                    "I'm up. What do you need?",

    # Cinematic / dramatic
    "initializing":             "Initializing. Neural systems nominal. Awaiting input.",
    "core_online":              "Core intelligence online. All modules green.",
    "full_operational":         "Fully operational, Master Miguel. Let's make it count.",
    "systems_go":               "Systems go. The floor is yours.",
    "standing_by":              "California standing by. Talk to me.",
}


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = load_config()

    # Force kokoro af_bella regardless of current config
    config["tts"]["provider"] = "kokoro"
    config["tts"]["kokoro"]["voice"] = "af_bella"
    config["tts"]["kokoro"]["speed"] = 1.0
    config["tts"]["kokoro"]["lang_code"] = "a"

    tts = TTSService(config)

    out_dir = os.path.join("sounds", "bootup")
    os.makedirs(out_dir, exist_ok=True)

    print(f"Generating {len(BOOTUP_LINES)} boot-up sound bites -> {out_dir}/\n")

    for filename, text in BOOTUP_LINES.items():
        out_path = os.path.join(out_dir, f"{filename}.wav")
        print(f"  [{filename}]  \"{text}\"")
        audio, sr = tts.synthesize(text)
        if audio.size == 0:
            print(f"    WARNING: empty audio for '{filename}', skipping.")
            continue
        sf.write(out_path, audio, sr)
        duration_ms = int(len(audio) / sr * 1000)
        print(f"    -> {out_path}  ({duration_ms}ms)")

    print(f"\nDone. {len(BOOTUP_LINES)} files written to {out_dir}/")


if __name__ == "__main__":
    main()
