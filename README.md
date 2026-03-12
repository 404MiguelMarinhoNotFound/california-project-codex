# 🌴 Project California

A DIY voice assistant powered by Claude, running on a Raspberry Pi (or your laptop).

**Say "Hey Jarvis" → speak your question → hear the answer.**

## Quick Start

```bash
# 1. Clone / navigate to the project
cd california

# 2. Create virtual env & install deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Set up API keys
cp .env.example .env
# Edit .env with your keys:
#   GROQ_API_KEY     — free at https://console.groq.com/keys
#   ANTHROPIC_API_KEY — at https://console.anthropic.com/settings/keys
nano .env

# 4. Test each component individually
python main.py --test-mic       # Check your microphone works + calibrate VAD
python main.py --test-tts       # Check TTS audio output
python main.py --test-stt       # Record + transcribe (tests mic + Groq)
python main.py --test-llm       # Chat via keyboard (tests Claude)
python main.py --test-pipeline  # Full loop: Enter → speak → hear answer (no wake word)

# 5. Run the full assistant
python main.py
```

## How It Works

```
Mic → Wake Word (openWakeWord) → Record (VAD) → STT (Groq Whisper) → LLM (Claude) → TTS (Edge/Piper) → Speaker
         local                     local          cloud ~0.5s       cloud streaming     cloud/local
```

The key: everything from LLM → TTS is **streamed sentence by sentence**, so you hear the
first words ~2 seconds after speaking — not 5-6 seconds if we waited for the full response.

## Configuration

All settings are in `config.yaml`. Key things to tune:

| Setting | What it does | When to change |
|---------|-------------|----------------|
| `wake_word.model` | Wake word trigger | Change to custom model |
| `wake_word.threshold` | Sensitivity (0-1) | Getting false positives → raise it |
| `vad.energy_threshold` | Silence detection | Run `--test-mic` to calibrate |
| `llm.provider` | claude / groq | Want free? Use groq |
| `tts.provider` | edge / piper / elevenlabs | edge works everywhere |

## Test Modes

| Command | Tests | Use when |
|---------|-------|----------|
| `--test-mic` | Microphone levels | Setting up, calibrating VAD threshold |
| `--test-tts` | Speaker + TTS engine | Checking audio output works |
| `--test-stt` | Mic → Groq transcription | Verifying STT quality + API key |
| `--test-llm` | Claude chat (keyboard) | Testing LLM responses + API key |
| `--test-pipeline` | Full loop (no wake word) | End-to-end test, press Enter to talk |

## Project Structure

```
california/
├── main.py                  # Entry point + test modes
├── config.yaml              # All tunables
├── .env                     # API keys (git-ignored)
├── core/
│   ├── orchestrator.py      # State machine (the brain)
│   ├── audio_pipeline.py    # Mic capture + playback
│   ├── wake_word.py         # openWakeWord detector
│   └── vad.py               # Voice activity detection
├── services/
│   ├── stt.py               # Speech-to-text (Groq Whisper)
│   ├── llm.py               # LLM brain (Claude / Groq)
│   ├── tts.py               # Text-to-speech (Edge / Piper / ElevenLabs)
│   └── sentence_chunker.py  # Stream splitter for LLM → TTS
├── hardware/
│   └── led_controller.py    # LED feedback (console on laptop, pixel_ring on Pi)
├── models/                  # Wake word models go here
└── sounds/                  # Auto-generated chime sounds
```

## Estimated Costs

| Component | Cost |
|-----------|------|
| Hardware (Pi 4 + ReSpeaker + speaker) | ~€80 one-time |
| Groq Whisper API | Free |
| Claude Sonnet 4.5 (~50 queries/day) | ~€2-3/month |
| Edge TTS | Free |
| **Total monthly** | **~€2-3** |

## Next Steps

1. **Custom wake word**: Train "Hey California" using openWakeWord's training tools
2. **Better TTS**: Switch to Piper (local) on Pi, or ElevenLabs (paid) for premium voice
3. **Home automation**: Add MQTT/Home Assistant integration
4. **Offline fallback**: Add local Whisper + small LLM for when internet is down
