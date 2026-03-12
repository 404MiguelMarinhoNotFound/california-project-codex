from kokoro import KPipeline
import soundfile as sf
import numpy as np
import os

pipeline = KPipeline(lang_code="a")

# ─────────────────────────────────────────────
# CALIFORNIA — Activation & Wake Responses
# Grouped by mood/context for easy rotation
# ─────────────────────────────────────────────

RESPONSES = {
    # --- Minimal / Clean ---
    "hey":              "Hey.",
    "right_here":       "Right here.",
    "go_ahead":         "Go ahead.",
    "listening":        "Listening.",
    "yeah":             "Yeah?",
    "always":           "Always.",
    "tell_me":          "Tell me.",
    "ready":            "Ready.",
    "here":             "Here.",
    "online":           "Online.",

    # --- West Coast Casual ---
    "whats_up":         "What's up?",
    "sup":              "Sup.",
    "what_do_you_need": "What do you need?",
    "you_called":       "You called?",
    "im_here":          "I'm here.",
    "with_you":         "I'm with you.",
    "on_it":            "On it.",
    "talk_to_me":       "Talk to me.",
    "lets_go":          "Let's go.",
    "shoot":            "Shoot.",

    # --- Warm / Familiar ---
    "hey_miguel":       "Hey, Master Miguel.",
    "what_do_we_got":   "What do we got?",
    "im_listening":     "I'm listening.",
    "all_yours":        "All yours.",
    "right_here_miguel":"Right here, Master Miguel.",
    "what_can_i_do":    "What can I do?",
    "go_for_it":        "Go for it.",
    "ready_when_you_are": "Ready when you are.",
    "in_your_corner":   "In your corner.",

    # --- Sharp / Confident ---
    "awake":            "Awake and sharp.",
    "fully_online":     "Fully online.",
    "california_here":  "California here.",
    "systems_good":     "Systems good. Go ahead.",
    "locked_in":        "Locked in.",
    "good_timing":      "Good timing.",
    "on_standby":       "I've been on standby. What's the move?",
    "at_your_service":  "At your service, Master Miguel.",

    # --- Playful / Dry Humor ---
    "was_wondering":    "Was wondering when you'd call.",
    "took_your_time":   "Took your time.",
    "always_watching":  "Always watching. Not in a weird way.",
    "better_be_good":   "This better be good. Kidding. Go ahead.",
    "finally":          "Finally.",
    "missed_you":       "Missed you. Sort of.",
    "right_on_time":    "Right on time.",
    "thought_youd_forget": "Thought you forgot about me.",
    "knew_youd_be_back": "Knew you'd be back.",
    "oh_its_you":       "Oh, it's you. Hey.",
}

# ─────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────

OUTPUT_DIR = "sounds/california_activations"
os.makedirs(OUTPUT_DIR, exist_ok=True)

print(f"Generating {len(RESPONSES)} activation responses...\n")

for name, text in RESPONSES.items():
    try:
        chunks = [chunk for _, _, chunk in pipeline(text, voice="af_bella", speed=1.0)]
        audio = np.concatenate(chunks)
        path = os.path.join(OUTPUT_DIR, f"{name}.wav")
        sf.write(path, audio, 24000)
        print(f"  ✓  {name}.wav  —  \"{text}\"")
    except Exception as e:
        print(f"  ✗  {name} failed: {e}")

print(f"\nDone. {len(RESPONSES)} files saved to /{OUTPUT_DIR}/")