#!/usr/bin/env bash
# ============================================================
# PROJECT CALIFORNIA — Setup Script (uv)
# ============================================================
# This project is uv-only. Do NOT use pip, venv, or virtualenv.
set -e

echo "🌴 Setting up Project California..."
echo ""

# --- Install uv if missing ---
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Make uv visible to this shell without requiring a restart
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "uv version: $(uv --version)"

# --- Interpreter + dependencies ---
# uv reads .python-version, downloads CPython if needed, and builds .venv
# to match uv.lock exactly. No manual venv creation, no pip.
echo "Syncing environment from uv.lock..."
uv sync

# Optional feature sets (see [project.optional-dependencies] in pyproject.toml):
#   uv sync --extra kokoro       # default TTS provider in config.yaml
#   uv sync --extra piper        # local TTS, Linux/Pi only
#   uv sync --extra elevenlabs   # premium TTS
#   uv sync --extra openai       # OpenAI-compatible LLM endpoints
#   uv sync --extra porcupine    # Porcupine wake word
#   uv sync --extra silero       # torch-based VAD (heavy, ~2GB)
#   uv sync --extra pi           # ReSpeaker LED ring, Linux/Pi only

# --- Setup .env ---
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  Created .env file. Please edit it with your API keys:"
    echo "    - GROQ_API_KEY     (free: https://console.groq.com/keys)"
    echo "    - ANTHROPIC_API_KEY (https://console.anthropic.com/settings/keys)"
    echo ""
    echo "    nano .env"
    echo ""
fi

# --- Create directories ---
mkdir -p models sounds

# --- Check audio devices ---
echo ""
echo "Checking audio devices..."
uv run python -c "
import sounddevice as sd
print('Input devices:')
for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels'] > 0:
        print(f'  [{i}] {d[\"name\"]} (inputs: {d[\"max_input_channels\"]})')
print()
print(f'Default input: {sd.query_devices(kind=\"input\")[\"name\"]}')
print(f'Default output: {sd.query_devices(kind=\"output\")[\"name\"]}')
"

echo ""
echo "✅ Setup complete!"
echo ""
echo "To run (no activation step needed — uv run handles the environment):"
echo "  uv run python main.py"
echo ""
echo "Say 'Hey Jarvis' to activate (or change wake word in config.yaml)"
