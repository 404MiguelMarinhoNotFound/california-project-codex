"""
Generate the boot-up one-liners California plays at startup.

Synthesizes every line in BOOTUP_LINES with the voice config.yaml's `tts`
block selects and writes one WAV per line into sounds/bootup/<provider>_<voice>/
(e.g. kokoro_af_bella/ or google_en-US-Chirp3-HD-Aoede/), so switching voices
never overwrites another voice's set. `Orchestrator._play_bootup_sound` picks
one at random from the folder `sounds.bootup_dir` names on each launch, so the
assistant does not greet Master Miguel the same way twice.

Run it after editing BOOTUP_LINES, and once on a fresh clone: the generated
WAVs are deliberately not committed, matching sounds/chime.wav and
sounds/error.wav. This repo carries audio *sources*, not audio output. Without
them the orchestrator logs "No bootup sounds found" and starts silently, which
is harmless but loses the greeting.

The voice is whatever config.yaml selects (same TTSService the assistant speaks
with), so the greeting always matches the replies. Run it after changing voice:

    uv run python generate_bootup_sounds.py

Sibling script: generate_activation_phrases.py, which does the same job for the
post-wake-word acknowledgements.
"""

import os
import sys
import yaml
import soundfile as sf
from dotenv import load_dotenv

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
    load_dotenv()
    config = load_config()
    tts = TTSService(config)

    out_dir = os.path.join("sounds", "bootup", tts.voice_slug())
    os.makedirs(out_dir, exist_ok=True)

    print(f"Voice: {tts.voice_slug()}")
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
    print(f'Point config.yaml at it:  sounds.bootup_dir: "{out_dir.replace(os.sep, "/")}"')


if __name__ == "__main__":
    main()
