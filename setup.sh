#!/usr/bin/env sh
# Den setup for Linux.
set -u
cd "$(dirname "$0")" || exit 1

echo "=== Den setup ==="

PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[X] Python 3.11 or newer was not found."
    echo "    Debian/Ubuntu: sudo apt install python3 python3-venv"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/4] Creating virtual environment..."
    if ! "$PY" -m venv .venv; then
        echo "[X] Could not create the virtual environment."
        echo "    Debian/Ubuntu: sudo apt install python3-venv"
        exit 1
    fi
else
    echo "[1/4] Virtual environment already exists."
fi

echo "[2/4] Installing dependencies (needs internet)..."
# REQUIREMENTS=requirements-lite.txt ./setup.sh   installs without the voice libraries
if ! .venv/bin/python -m pip install --disable-pip-version-check --quiet -r "${REQUIREMENTS:-requirements.txt}"; then
    echo "[X] Dependency install failed. Check your internet connection and run ./setup.sh again."
    exit 1
fi

if [ ! -f ".env" ]; then
    cp .env.example .env
    chmod 600 .env
    echo "[3/4] Created .env - edit it and paste your bot token."
else
    echo "[3/4] .env already exists."
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[!] ffmpeg not found. Music and video will not work until you install it:"
    echo "    Debian/Ubuntu: sudo apt install ffmpeg"
fi

chmod +x start.sh 2>/dev/null

echo "[4/4] Self-test..."
if .venv/bin/python bot.py --check; then
    echo
    echo "Setup finished. Put your token in .env, then run ./start.sh"
else
    echo "[!] The self-test reported a problem. Read the lines above."
fi
