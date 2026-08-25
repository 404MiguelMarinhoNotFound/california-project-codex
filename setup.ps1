# ============================================================
# PROJECT CALIFORNIA - Setup Script (uv, Windows)
# ============================================================
# This project is uv-only. Do NOT use pip, venv, or virtualenv.
$ErrorActionPreference = "Stop"

Write-Host "Setting up Project California..."
Write-Host ""

# --- Install uv if missing ---
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    # Make uv visible to this shell without requiring a restart
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "uv version: $(uv --version)"

# --- Interpreter + dependencies ---
# uv reads .python-version, downloads CPython if needed, and builds .venv
# to match uv.lock exactly. No manual venv creation, no pip.
Write-Host "Syncing environment from uv.lock..."
uv sync

# Optional feature sets (see [project.optional-dependencies] in pyproject.toml):
#   uv sync --extra kokoro       # default TTS provider in config.yaml
#   uv sync --extra elevenlabs   # premium TTS
#   uv sync --extra openai       # OpenAI-compatible LLM endpoints
#   uv sync --extra porcupine    # Porcupine wake word
#   uv sync --extra silero       # torch-based VAD (heavy, ~2GB)
# (piper and pi extras are Linux/Pi only and resolve to nothing on Windows)

# --- Setup .env ---
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "Created .env file. Please edit it with your API keys:"
    Write-Host "    - GROQ_API_KEY      (free: https://console.groq.com/keys)"
    Write-Host "    - ANTHROPIC_API_KEY (https://console.anthropic.com/settings/keys)"
    Write-Host ""
}

# --- Create directories ---
New-Item -ItemType Directory -Force -Path models, sounds | Out-Null

# --- Check audio devices ---
Write-Host ""
Write-Host "Checking audio devices..."
uv run python -c @'
import sounddevice as sd
print("Input devices:")
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0:
        print(f'  [{i}] {d["name"]} (inputs: {d["max_input_channels"]})')
print()
print(f'Default input: {sd.query_devices(kind="input")["name"]}')
print(f'Default output: {sd.query_devices(kind="output")["name"]}')
'@

Write-Host ""
Write-Host "Setup complete!"
Write-Host ""
Write-Host "To run (no activation step needed - uv run handles the environment):"
Write-Host "  uv run python main.py"
