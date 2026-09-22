#!/usr/bin/env bash
# Set voicepilot up on a fresh machine: build a venv with a Python that has
# wheels, install deps, point the shebang at it, and put it on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "voicepilot needs a Unix pty (it uses pty/termios/fcntl)."
     echo "On Windows, run it inside WSL."; exit 1 ;;
esac

# 1. pick an interpreter 
# Newest is not safest: compiled deps (ctranslate2) lag behind new releases.
PY=""
for v in 3.12 3.13 3.11 3.10; do
  if command -v "python$v" >/dev/null 2>&1; then PY="$(command -v python$v)"; break; fi
done
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
  [ -z "$PY" ] && { echo "No python3 found. Install Python 3.10-3.13."; exit 1; }
  echo "warning: falling back to $("$PY" -V). If faster-whisper fails to"
  echo "         install, get Python 3.12 and re-run this script."
fi
echo "==> interpreter: $PY ($("$PY" -V 2>&1))"

# 2. build the venv
rm -rf .venv
if command -v uv >/dev/null 2>&1; then
  echo "==> creating .venv with uv"
  uv venv --python "$PY" .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  echo "==> creating .venv with venv/pip"
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install -r requirements.txt
fi

# 3. make the script self-contained
# -i.bak keeps this working on both BSD sed (macOS) and GNU sed (Linux)
sed -i.bak "1s|.*|#!$HERE/.venv/bin/python|" voicepilot.py
rm -f voicepilot.py.bak
chmod +x voicepilot.py

mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/voicepilot.py" "$HOME/.local/bin/voicepilot"
echo "==> installed: $HOME/.local/bin/voicepilot"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "    (add to your shell rc:  export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
esac

# 4. platform bits the pip deps can't provide
if [ "$(uname -s)" = "Linux" ]; then
  command -v espeak-ng >/dev/null 2>&1 || command -v spd-say >/dev/null 2>&1 || {
    echo "==> no speech synthesiser found, install one:"
    echo "    sudo apt install espeak-ng      # or: sudo dnf install espeak-ng"; }
  ./.venv/bin/python -c "import sounddevice" 2>/dev/null || {
    echo "==> PortAudio missing:  sudo apt install libportaudio2"; }
fi

echo
echo "Done. Verify with:  voicepilot --check"
