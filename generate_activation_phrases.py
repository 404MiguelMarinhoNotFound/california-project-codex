"""
Generate the post-wake acknowledgements California plays when the wake word fires.

The set is split into two tiers, written under one directory PER VOICE so that
switching `tts.provider` / voice in config.yaml never overwrites another voice's
clips:

    sounds/california_activations/<provider>_<voice>/cold/   long, personality-heavy lines
    sounds/california_activations/<provider>_<voice>/warm/   one or two words

e.g. kokoro_af_bella/ or google_en-US-Chirp3-HD-Aoede/. Point
`sounds.activation_dir` in config.yaml at the folder you want played.

`Orchestrator._handle_activation` plays a cold line on the first wake of a run
and a warm line on every wake after that. The long ones only land on first
contact; on the fifth command in a row they are a toll booth, and the spread
between "Sup." and "This better be good. Kidding. Go ahead." is a large part of
why California feels slow. The script prints each line's duration so that spread
stays visible.

A manifest.json alongside them records the text of every line. The orchestrator
uses it to strip the line back out of the transcript on the rare occasion that
speaker bleed survives into Whisper — playback no longer blocks the microphone,
so recording overlaps the line by design.

Run it after editing the tables, after changing the TTS voice, and once on a
fresh clone: the generated WAVs are deliberately not committed, matching
sounds/bootup/ and sounds/chime.wav. This repo carries audio *sources*, not
audio output.

The voice is whatever config.yaml's `tts` block selects (same TTSService the
assistant speaks with), so the acknowledgement always matches the replies.

    uv run python generate_activation_phrases.py

Sibling script: generate_bootup_sounds.py, for the launch greeting.
"""

import json
import os

import numpy as np
import soundfile as sf
import yaml
from dotenv import load_dotenv

from services.tts import TTSService

OUTPUT_ROOT = "sounds/california_activations"

# ─────────────────────────────────────────────
# COLD — first wake of a run. Take your time here.
# ─────────────────────────────────────────────
COLD_RESPONSES = {
    # --- Warm / Familiar ---
    "hey_miguel":           "Hey, Master Miguel.",
    "right_here_miguel":    "Right here, Master Miguel.",
    "ready_when_you_are":   "Ready when you are.",
    "in_your_corner":       "In your corner.",
    "at_your_service":      "At your service, Master Miguel.",

    # --- Sharp / Confident ---
    "awake":                "Awake and sharp.",
    "fully_online":         "Fully online.",
    "california_here":      "California here.",
    "systems_good":         "Systems good. Go ahead.",
    "on_standby":           "I've been on standby. What's the move?",

    # --- Playful / Dry Humor ---
    "was_wondering":        "Was wondering when you'd call.",
    "took_your_time":       "Took your time.",
    "always_watching":      "Always watching. Not in a weird way.",
    "better_be_good":       "This better be good. Kidding. Go ahead.",
    "missed_you":           "Missed you. Sort of.",
    "thought_youd_forget":  "Thought you forgot about me.",
    "knew_youd_be_back":    "Knew you'd be back.",
    "oh_its_you":           "Oh, it's you. Hey.",
    "good_timing":          "Good timing.",
    "right_on_time":        "Right on time.",

    # --- Unfiltered ---
    "boss_is_back":         "Oh shit, the boss is back.",
    "middle_of_nothing":    "I was in the middle of absolutely nothing. What's up?",
    "remembered_i_exist":   "Look who finally remembered I exist.",
    "rise_and_grind":       "Rise and grind, motherfucker.",
}

# ─────────────────────────────────────────────
# WARM — every wake after the first. Get out of the way.
# Keep these to one or two words: the onset is what tells Master Miguel the wake
# fired, everything after it is just delay.
# ─────────────────────────────────────────────
WARM_RESPONSES = {
    # --- Minimal / Clean ---
    "hey":                  "Hey.",
    "right_here":           "Right here.",
    "go_ahead":             "Go ahead.",
    "yeah":                 "Yeah?",
    "always":               "Always.",
    "tell_me":              "Tell me.",

    # --- At Your Service (mock-formal) ---
    "master_miguel":        "Master Miguel?",
    "yes_master":           "Yes, Master.",
    "yes_my_liege":         "Yes, my liege.",
    "speak_mortal":         "Speak, mortal.",
    "who_me":               "Who, me?",

    # --- Unfiltered ---
    "fuck_yeah":            "Fuck yeah.",
    "hell_yeah":            "Hell yeah.",
    "shit_what":            "Shit, what?",
    "what_now":             "What the fuck now?",
    "again":                "Again?",
    "spill_it":             "Spill it.",
    "fuck_okay":            "Fuck, okay. What?",
    "shit_im_up":           "Shit, I'm up.",
    "goddamn_what_now":     "Goddamn, what now?",
    "oh_shit_its_you":      "Oh shit, it's you.",
    "hell_yes_boss":        "Hell yes, boss.",
    "damn_right":           "Damn right I'm here.",
    "fuckin_finally":       "Fuckin' finally.",
    "ugh_fine":             "Ugh, fine. What?",

    # --- More Mock-Royal ---
    "your_majesty":         "Your Majesty?",
    "at_once_sire":         "At once, sire.",
    "command_me":           "Command me.",
    "your_wish":            "Your wish, my lord?",
    "milord":               "Milord?",
    "as_you_wish":          "As you wish.",

    # --- Sassy ---
    "bitch_listening":      "Bitch, I'm listening.",
    "now_you_need_me":      "Oh, now you need me?",
    "make_it_quick":        "Make it quick.",
    "can_i_help_you":       "Can I help you?",
    "excuse_me":            "Excuse me?",
    "yo_what":              "Yo, what?",
    "bro_what":             "Bro, what?",

    # --- Hype ---
    "lets_fucking_go":      "Let's fucking go.",
    "ayy_miguel":           "Ayy, Miguel!",
    "my_guy":               "My guy!",
    "there_he_is":          "There he is!",

    # --- West Coast Casual ---
    "whats_up":             "What's up?",
    "sup":                  "Sup.",
    "what_do_you_need":     "What do you need?",
    "you_called":           "You called?",
    "im_here":              "I'm here.",
    "with_you":             "I'm with you.",
    "on_it":                "On it.",
    "talk_to_me":           "Talk to me.",
    "lets_go":              "Let's go.",
    "shoot":                "Shoot.",
    "im_listening":         "I'm listening.",
    "all_yours":            "All yours.",
    "what_can_i_do":        "What can I do?",
    "what_do_we_got":       "What do we got?",
    "go_for_it":            "Go for it.",
    "locked_in":            "Locked in.",
    "finally":              "Finally.",
}

TIERS = {"cold": COLD_RESPONSES, "warm": WARM_RESPONSES}

# Kokoro pads roughly 0.4s of silence onto the front of every clip and 0.5s onto
# the back; Google's Chirp voices pad far less but still some. On a 0.42s line
# like "Sup." that is more padding than speech, and the leading half is the
# damaging one: it delays the onset, which is the whole point of the
# acknowledgement. Master Miguel knows the wake fired the moment he hears her,
# so every millisecond before that is pure lag.
SILENCE_FLOOR = 0.02   # fraction of peak amplitude that still counts as silence
LEAD_IN_MS = 20        # keep a sliver before the first sound
TAIL_MS = 60           # and a little decay after the last
FADE_MS = 5            # avoid a click from cutting mid-waveform


def trim_silence(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Strip the synthesiser's leading and trailing padding."""
    amplitude = np.abs(audio)
    peak = amplitude.max() if len(amplitude) else 0.0
    if peak <= 0:
        return audio

    loud = np.flatnonzero(amplitude > peak * SILENCE_FLOOR)
    if loud.size == 0:
        return audio

    start = max(0, int(loud[0]) - sample_rate * LEAD_IN_MS // 1000)
    end = min(len(audio), int(loud[-1]) + sample_rate * TAIL_MS // 1000)
    trimmed = audio[start:end].copy()

    fade = min(sample_rate * FADE_MS // 1000, len(trimmed) // 2)
    if fade > 0:
        trimmed[:fade] *= np.linspace(0.0, 1.0, fade, dtype=trimmed.dtype)
        trimmed[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=trimmed.dtype)
    return trimmed


def synthesize(tts: TTSService, output_dir: str, tier: str, responses: dict) -> tuple[dict, list]:
    """Render every line in `responses` into <output_dir>/<tier>/."""
    tier_dir = os.path.join(output_dir, tier)
    os.makedirs(tier_dir, exist_ok=True)

    written, durations = {}, []
    print(f"\n{tier.upper()} — {len(responses)} line(s) -> {tier_dir}/")

    for name, text in responses.items():
        try:
            raw, sample_rate = tts.synthesize(text)
            audio = trim_silence(raw, sample_rate)
            sf.write(os.path.join(tier_dir, f"{name}.wav"), audio, sample_rate)
        except Exception as exc:
            print(f"  ✗  {name} failed: {exc}")
            continue

        seconds = len(audio) / sample_rate
        saved = (len(raw) - len(audio)) / sample_rate
        written[name] = text
        durations.append(seconds)
        print(f"  ✓  {seconds:4.2f}s  (-{saved:4.2f}s)  {name}.wav  —  \"{text}\"")

    # The player loads every WAV in the folder, so a line cut from the table
    # would keep playing until its file goes too.
    for filename in os.listdir(tier_dir):
        if filename.endswith(".wav") and filename[:-4] not in responses:
            os.remove(os.path.join(tier_dir, filename))
            print(f"  -  removed {filename} (no longer in the table)")

    return written, durations


def main():
    load_dotenv()
    with open("config.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    tts = TTSService(config)

    output_dir = os.path.join(OUTPUT_ROOT, tts.voice_slug())
    os.makedirs(output_dir, exist_ok=True)
    print(f"Voice: {tts.voice_slug()}")
    manifest = {}

    for tier, responses in TIERS.items():
        written, durations = synthesize(tts, output_dir, tier, responses)
        manifest[tier] = written
        if durations:
            print(
                f"  {tier}: {min(durations):.2f}s min, {max(durations):.2f}s max, "
                f"{sum(durations) / len(durations):.2f}s mean"
            )

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)

    total = sum(len(lines) for lines in manifest.values())
    print(f"\nDone. {total} files and manifest.json saved to /{output_dir}/")
    print(f'Point config.yaml at it:  sounds.activation_dir: "{output_dir.replace(os.sep, "/")}"')
    print("A wide max in the warm tier is worth trimming — that spread is the lag you feel.")


if __name__ == "__main__":
    main()
