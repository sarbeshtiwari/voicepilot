#!/usr/bin/env bash
# Set voicepilot up on a fresh machine: build a venv with a Python that has
# wheels, install deps, and put a launcher on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
VENV="$HERE/.venv"
if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  VENV="$HERE/.venv-wsl"
fi

case "$(uname -s)" in
  Darwin|Linux) ;;
  MINGW*|MSYS*|CYGWIN*)
     # a Unix-ish shell on Windows: the venv layout here is Scripts\, not bin/,
     # so this script would build something that cannot run
     echo "This is a Windows shell. Use the PowerShell installer instead:"
     echo ""
     echo "    powershell -ExecutionPolicy Bypass -File install.ps1"
     echo ""
     echo "That sets up solo mode (voice control of the machine)."
     echo "Wrapping an AI agent needs a Unix pty - install under WSL for that."
     exit 1 ;;
  *) echo "Unsupported platform: $(uname -s)"; exit 1 ;;
esac

# --- 1. pick an interpreter -------------------------------------------------
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

# --- 2. build the venv ------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  echo "==> using $VENV with uv"
  [ -x "$VENV/bin/python" ] || uv venv --python "$PY" "$VENV"
  uv pip install --python "$VENV/bin/python" -r requirements.txt
else
  echo "==> using $VENV with venv/pip"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install -r requirements.txt
fi

# --- 2b. optional Apple Silicon speech engines -----------------------------
# Much better English recognition, but parakeet pulls torch (~2GB), so it is
# opt-in rather than part of the default install.
if [ "${1:-}" = "--mlx" ]; then
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
    echo "==> installing parakeet-mlx + mlx-whisper (large download)"
    if command -v uv >/dev/null 2>&1; then
      uv pip install --python "$VENV/bin/python" parakeet-mlx mlx-whisper
    else
      "$VENV/bin/python" -m pip install parakeet-mlx mlx-whisper
    fi
  else
    echo "==> --mlx needs Apple Silicon; skipping"
  fi
elif [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  echo "==> tip: ./install.sh --mlx adds faster, more accurate speech engines"
fi

# --- 3. launcher (also works when the project path contains spaces) ---------
mkdir -p "$HOME/.local/bin"
# Replace the old symlink before writing, or it would overwrite the source.
rm -f "$HOME/.local/bin/voicepilot"
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV/bin/python" "$HERE/voicepilot.py" > "$HOME/.local/bin/voicepilot"
chmod +x "$HOME/.local/bin/voicepilot"
echo "==> installed: $HOME/.local/bin/voicepilot"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "    (add to your shell rc:  export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
esac

# --- 4. platform bits the pip deps can't provide ---------------------------
if [ "$(uname -s)" = "Linux" ]; then
  command -v espeak-ng >/dev/null 2>&1 || command -v spd-say >/dev/null 2>&1 || {
    echo "==> no speech synthesiser found, install one:"
    echo "    sudo apt install espeak-ng      # or: sudo dnf install espeak-ng"; }
  "$VENV/bin/python" -c "import sounddevice" 2>/dev/null || {
    echo "==> PortAudio missing:  sudo apt install libportaudio2"; }
fi

echo
echo "Done. Verify with:  voicepilot --check"
