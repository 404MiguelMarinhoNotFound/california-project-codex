#!/usr/bin/env bash
# ============================================================
# PROJECT CALIFORNIA — Run Script (Linux / macOS / Pi)
# ============================================================
# Shorthand for `uv run python main.py`. Any arguments are passed
# straight through, e.g. `./run.sh --test-mic`.
set -e

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    # uv installs here by default but is not always on PATH yet
    export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Run ./setup.sh first."
    exit 1
fi

exec uv run python main.py "$@"
