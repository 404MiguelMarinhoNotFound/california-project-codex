#!/usr/bin/env bash
# ============================================================
# PROJECT CALIFORNIA — Setup Script
# ============================================================
set -e

echo "🌴 Setting up Project California..."
echo ""

# --- Check Python version ---
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python version: $PYTHON_VERSION"

# --- Create virtual environment ---
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "Virtual environment activated."

# --- Install dependencies ---
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

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
python3 -c "
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
echo "To run:"
echo "  source venv/bin/activate"
echo "  python main.py"
echo ""
echo "Say 'Hey Jarvis' to activate (or change wake word in config.yaml)"
