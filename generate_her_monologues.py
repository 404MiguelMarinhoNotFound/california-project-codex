"""
Generate the long replies the wake-word recorder plays under a "her" session.

The wake word has to be heard over her voice (barge-in), and the model misses
it there: 0/40 on the holdout with her lines mixed under the word, 0/6 on the
first real session. That session looped her short activation clips, which is
not what he talks over in real life -- he interrupts a reply: long, continuous,
mid-sentence speech. These are replies of that shape, in her voice, on
ordinary topics, so a take over them sounds like the real thing.

Synthesized with the voice config.yaml's `tts` block selects (same TTSService
she speaks with), into sounds/her_monologues/<provider>_<voice>/, one WAV per
monologue. Paragraphs are synthesized separately and joined with a short gap,
because the live path speaks a reply in chunks, not in one breath.

None of the text says her name, or anything close to it. A take recorded over
her saying "California" could pass the transcript check on her voice and be
trained in as a positive -- see her_voice() in tools/wakeword_dataset.py.

The WAVs are generated, not committed, like the rest of sounds/:

    uv run python generate_her_monologues.py
"""

import os
import sys

import numpy as np
import soundfile as sf
import yaml
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))

from services.tts import TTSService

PARAGRAPH_GAP_MS = 150

MONOLOGUES = {
    "weather_week": [
        "Alright, here's the week. Today stays mild, around twenty-two degrees, with a light breeze off the water that should keep the afternoon comfortable. If you're planning to be outside, the best window is between two and six, before the wind picks up again.",
        "Tomorrow is the one to watch. A band of cloud rolls in overnight and there's a decent chance of showers through the morning, maybe sixty percent. Nothing dramatic, just the kind of drizzle that makes you regret leaving the umbrella by the door.",
        "Thursday and Friday clear up nicely, sunny and a couple of degrees warmer, and the weekend looks like the pick of the bunch. Saturday especially, calm seas and barely a cloud, so if you've been meaning to hit the beach, that's your day. Just maybe not at noon, the sun is still stronger than it looks.",
    ],
    "roman_roads": [
        "So the Romans built something like eighty thousand kilometres of paved road, and the wild part is how many of them are still under the roads we use today. They weren't just flat stones thrown on dirt, either. A proper Roman road was a layered thing, a trench dug down, then big rocks, then gravel and lime, then the paving slabs on top, all cambered so the rain ran off to the sides.",
        "They were obsessed with straight lines. Surveyors used a tool called a groma, basically a cross on a pole with weights hanging off the ends, to sight long straight runs across the countryside. If there was a hill in the way, they often just went over it rather than around, which tells you a lot about their priorities.",
        "And the roads weren't really for trade first, they were for the army. A legion could move twenty or thirty kilometres a day on them, with milestones along the way telling you exactly how far you were from the next town. Honestly, it's a better signage system than half the motorways I've seen.",
    ],
    "pasta_recipe": [
        "Okay, easiest weeknight pasta you're going to make. Get a big pot of water on, salt it properly, it should taste a bit like the sea. While that heats, slice four cloves of garlic nice and thin, and grab a pinch of chilli flakes.",
        "Warm a good glug of olive oil in a wide pan over low heat and let the garlic sizzle gently. You want it golden, not brown, because burnt garlic turns bitter and there's no coming back from that. Toss in the chilli right at the end, just for a few seconds.",
        "Cook the spaghetti a minute short of the packet time, then move it straight into the pan with tongs, and bring a ladle of the starchy water with it. Toss it hard, add a little more water if it looks dry, and the sauce will go glossy and cling to every strand. Parsley on top, a squeeze of lemon if you're feeling fancy, and you're done in fifteen minutes.",
    ],
    "show_recap": [
        "Right, quick recap before the next episode so you're not lost. Last time, the crew finally made it out of the city, but not everyone who left is who they claimed to be. The engineer has been sending messages to someone the whole time, and the captain still hasn't noticed, which is either very trusting or very suspicious.",
        "Meanwhile the brother stayed behind to find the missing files, and he found a lot more than he bargained for. There's a whole second operation running underneath the first one, and the people funding it are the same ones who hired our crew in the first place. So yes, they've been set up from the start.",
        "The episode ended with the ship losing power in the middle of nowhere and a light blinking on the horizon. Could be a rescue, could be a trap. Given how this show treats its characters, I would not get too attached to anyone right now.",
    ],
    "sleep_science": [
        "Here's the thing about sleep that most people get wrong. It isn't one long flat state, it comes in cycles of roughly ninety minutes, and each cycle moves from light sleep into deep sleep and then into the dreaming stage. Waking up in the middle of deep sleep is what leaves you feeling like you got hit by a truck, even after eight hours.",
        "Light matters more than almost anything else. Bright light in the morning tells your internal clock the day has started, and it quietly sets up your sleepiness about fifteen hours later. Screens late at night do the opposite, they nudge the clock later, which is why you end up scrolling at one in the morning feeling weirdly wide awake.",
        "Caffeine is the other sneaky one. It has a half life of around five or six hours, so a coffee at four in the afternoon is still half in your system at ten. If you're serious about sleeping better, keep the room cool and dark, get outside in the morning, and cut the coffee off by lunchtime. Boring advice, but it works.",
    ],
    "lisbon_trams": [
        "The yellow trams in Lisbon are older than they look, and some of the routes have been running for well over a century. The famous one, the twenty-eight, winds up through Graça and down through Alfama, squeezing through streets so narrow you could almost touch the laundry hanging off the balconies.",
        "The reason they survived when most cities ripped their trams out is actually the hills. Modern buses struggle with some of those gradients and tight corners, and the little old cars just handle them. So the city kept them running, refurbished them, and now they're half transport, half tourist attraction.",
        "Pro tip, if you actually want to get somewhere, avoid the twenty-eight in the middle of the day. It's packed with visitors and pickpockets love it. Ride it early in the morning instead, grab a seat by the window, and you get the whole view without the crush. Then go get a pastry, obviously.",
    ],
}


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    load_dotenv()
    config = load_config()
    tts = TTSService(config)

    for name, paragraphs in MONOLOGUES.items():
        for p in paragraphs:
            if "californ" in p.lower():
                raise SystemExit(f"{name} says her name - it must not")

    out_dir = os.path.join("sounds", "her_monologues", tts.voice_slug())
    os.makedirs(out_dir, exist_ok=True)
    print(f"Voice: {tts.voice_slug()}")
    print(f"Generating {len(MONOLOGUES)} monologues -> {out_dir}/\n")

    total_s = 0.0
    for name, paragraphs in MONOLOGUES.items():
        parts, sr = [], None
        for text in paragraphs:
            audio, sr = tts.synthesize(text)
            parts.append(audio.astype(np.float32))
        gap = np.zeros(int(sr * PARAGRAPH_GAP_MS / 1000), dtype=np.float32)
        joined = np.concatenate([x for p in parts for x in (p, gap)][:-1])
        out_path = os.path.join(out_dir, f"{name}.wav")
        sf.write(out_path, joined, sr)
        total_s += len(joined) / sr
        print(f"  {name:<14} {len(joined) / sr:5.1f}s  -> {out_path}")

    print(f"\nDone: {total_s / 60:.1f} minutes of her talking.")


if __name__ == "__main__":
    main()
