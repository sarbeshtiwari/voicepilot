#!/usr/bin/env python3
"""
voicepilot - talk to any terminal AI agent by voice.

Wraps an interactive CLI agent (claude, codex, kimi, kiro, aider, gemini, ...)
inside a pty. You keep using the agent exactly as normal. In the background
voicepilot watches the agent's output stream; when the output goes quiet
(= the model stopped thinking and the task is done) it:

  1. speaks  -> "Task complete. What's next?"
  2. listens -> records your voice, stops on silence
  3. types   -> transcribes it and sends it to the agent as your next prompt

Repeat forever, hands free.

    ./voicepilot.py claude
    ./voicepilot.py codex --model gpt-5
    ./voicepilot.py --check              # verify mic / TTS / STT setup

Keys while running:
    Ctrl-]      talk now (push-to-talk) / cancel current listen
    Ctrl-\\      toggle automatic voice mode on/off
    any key     cancels a listen in progress (fall back to typing)
"""

from __future__ import annotations

import argparse
import codecs
import getpass
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque

try:                       # agent-wrapping mode needs a real pty
    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty
    HAVE_PTY = True
except ImportError:        # native Windows
    HAVE_PTY = False

# ---------------------------------------------------------------- constants --

HOTKEY_TALK = b"\x1d"      # Ctrl-]
HOTKEY_TOGGLE = b"\x1c"    # Ctrl-backslash

DIM = "\x1b[2m"
GRN = "\x1b[32m"
YEL = "\x1b[33m"
RED = "\x1b[31m"
RST = "\x1b[0m"

# strips CSI / OSC / single-char escapes and control bytes
ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[@-Z\\-_]"
    rb"|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)

# shown by agents *while* they are working - never announce over these
BUSY_MARKERS = (
    "esc to interrupt",
    "ctrl+c to interrupt",
    "ctrl-c to interrupt",
    "press esc to stop",
    "interrupt)",
)

# An agent showing its input prompt is a far stronger "it finished" signal
# than silence alone, and it is what lets this work with agents whose idle
# screen keeps repainting. Union of the shapes real CLIs draw.
PROMPT_PATTERNS = (
    re.compile(r"^\s*[│┃|]\s*[>❯›]"),          # boxed input (claude, codex, crush)
    re.compile(r"^\s*>>>\s*$"),               # python / ollama repl
    re.compile(r"^\s*[>❯›#\$]\s*$"),           # bare prompt (aider, shells)
    re.compile(r"\?\s*for\s+shortcuts"),
    re.compile(r"^\s*\(.*\)\s*[>❯]\s*$"),     # "(main) >"
)

# Per-agent tuning, picked from the command name. Unknown agents get the
# defaults, which are deliberately conservative rather than Claude-specific.
AGENT_PROFILES = {
    "claude":   {"idle": 2.5},
    "codex":    {"idle": 2.5, "submit_delay": 1.0},
    "crush":    {"idle": 2.5},
    "opencode": {"idle": 2.5},
    "kimi":     {"idle": 2.5},
    "kiro":     {"idle": 2.5},
    "goose":    {"idle": 2.5},
    "gemini":   {"idle": 2.5},
    "cursor-agent": {"idle": 2.5},
    "aider":    {"idle": 2.0, "submit_key": "\r"},
    "ollama":   {"idle": 1.5},
    "llm":      {"idle": 1.5},
    "python":   {"idle": 1.0},
    "python3":  {"idle": 1.0},
    "node":     {"idle": 1.0},
    "sqlite3":  {"idle": 1.0},
}


def agent_profile(argv: list) -> dict:
    """Tuning for the command being wrapped, by basename (and subcommand)."""
    if not argv:
        return {}
    base = os.path.basename(argv[0]).lower()
    base = re.sub(r"\.(exe|cmd|bat|py)$", "", base)
    if base in AGENT_PROFILES:
        return AGENT_PROFILES[base]
    for token in argv[1:3]:                      # e.g. "ollama run llama3"
        if token.lower() in AGENT_PROFILES:
            return AGENT_PROFILES[token.lower()]
    return {}


# the agent is blocked on a yes/no or numbered choice
APPROVAL_RE = re.compile(
    r"(do you want to|allow this|grant .*permission|proceed\?|"
    r"\by/n\b|\[y/n\]|\(y/n\)|❯\s*1\.|^\s*1\.\s*yes)",
    re.I | re.M,
)

YES_WORDS = {"yes", "yep", "yeah", "yup", "sure", "ok", "okay", "approve",
             "allow", "go ahead", "do it", "confirm", "one"}
NO_WORDS = {"no", "nope", "deny", "reject", "cancel that", "dont", "don't",
            "stop that", "escape"}
NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5"}

REPEAT_WORDS = {"repeat", "again", "repeat that", "say that again",
                "read that again", "what did it say", "what did you say",
                "come again", "pardon", "sorry"}
MORE_WORDS = {"read more", "more", "continue", "go on", "keep reading",
              "read the rest", "the rest", "finish it"}

QUIT_WORDS = {"stop listening", "voice off", "stop voice", "mute",
              "quiet mode", "disable voice"}
SKIP_WORDS = {"cancel", "never mind", "nevermind", "nothing", "skip",
              "forget it", "ignore that"}


# lines that are terminal furniture, not something worth reading aloud
CHROME_PATTERNS = (
    re.compile(r"^[\s\u2500-\u257f\u2580-\u259f·•]*$"),   # box rules only
    re.compile(r"^\s*[│┃|]"),                                 # input box interior
    re.compile(r"^\s*[>❯›]\s"),                              # echo of your prompt
    re.compile(r"^\s*[⎿⏐↳└├]"),                               # tool-result gutters
    re.compile(r"\?\s*for\s+shortcuts", re.I),
    re.compile(r"(esc|ctrl[+-]c) to (interrupt|stop|exit)", re.I),
    re.compile(r"^\s*[⏵⏴▶◀]{1,2}\s"),                        # mode indicators
    re.compile(r"^\s*[✻✽✢✳∗*]\s.*\b\d+s\b"),                # "✻ Working for 3s"
    re.compile(r"^\s*(shift\+tab|ctrl\+\w+|alt\+\w+)", re.I),
    re.compile(r"context left|auto-?compact", re.I),
    re.compile(r"^\s*tokens?:\s|^\s*cost:\s*\$", re.I),      # aider footers
    re.compile(r"^\s*\d+(\.\d+)?[km]? tokens?\b", re.I),
    re.compile(r"^\s*(use|type) /\w+ (to|for)\b", re.I),      # hint bars
    re.compile(r"^\s*[>❯›]{1,3}\s*$"),                        # bare prompt line
)
BULLET_RE = re.compile(r"^\s*[●○◍◆▪▸•]\s+")


class Screen:
    """Reassembles the agent's output into lines and isolates its last reply.

    Deliberately approximate - it models \n and \r but not cursor addressing.
    It is only ever used to decide what to read out loud, never to render.
    """

    def __init__(self, maxlines: int = 1500):
        self.lines: deque = deque(maxlen=maxlines)
        self.cur = ""
        self.seen = 0          # total completed lines, ever
        self.mark_at = 0
        self.marked = False
        self.echo = ""
        self._pending_cr = False

    def feed(self, text: str) -> None:
        text = ANSI_TXT_RE.sub("", text)
        # a pty sends CRLF for every newline (ONLCR); treating that CR as an
        # overwrite would blank each line just before it is committed. A chunk
        # can also split the pair, hence the carry.
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            self._pending_cr = True
        text = text.replace("\r\n", "\n")
        for ch in text:
            if ch == "\n":
                self.lines.append(self.cur)
                self.cur = ""
                self.seen += 1
            elif ch == "\r":
                self.cur = ""      # the agent is overwriting this line
            else:
                self.cur += ch

    def tail_lines(self, n: int = 6) -> list:
        out = list(self.lines)[-n:]
        if self.cur:
            out.append(self.cur)
        return out

    def at_prompt(self) -> bool:
        """Is the agent visibly showing its input prompt?"""
        return any(p.search(line) for line in self.tail_lines(6)
                   for p in PROMPT_PATTERNS)

    def mark(self, echo: str = "") -> None:
        """Remember where the next reply starts: just after your prompt.

        `echo` is the text we just typed; a pty echoes it straight back and it
        would otherwise be read aloud as if the agent had said it.
        """
        self.mark_at = self.seen
        self.marked = True
        self.echo = echo.strip()

    def _since_mark(self) -> list:
        back = self.seen - self.mark_at
        if back <= 0:
            out = []
        elif back <= len(self.lines):
            out = list(self.lines)[-back:]
        else:
            out = list(self.lines)
        return out + ([self.cur] if self.cur.strip() else [])

    def reply(self) -> str:
        """The agent's answer since your last prompt, minus the TUI chrome."""
        out: list = []
        recent: deque = deque(maxlen=60)    # repaints repeat whole lines
        dropped: list = []
        for raw in self._since_mark():
            line = raw.rstrip()
            if not line.strip():
                continue
            if any(p.search(line) for p in CHROME_PATTERNS):
                dropped.append(("chrome", line))
                continue
            if self.echo and line.strip() == self.echo:
                dropped.append(("your own prompt", line))
                continue
            line = BULLET_RE.sub("", line).strip()
            if not line:
                continue
            if line in recent:
                dropped.append(("repaint duplicate", line))
                continue
            recent.append(line)
            out.append(line)
        if TRACE:
            TRACE("extract", f"{len(out)} line(s) kept, {len(dropped)} dropped")
            for why, line in dropped[:6]:
                TRACE("  drop", f"{why:18} | {line[:56]}")
            for line in out[:6]:
                TRACE("  keep", f"{'':18} | {line[:56]}")
        return "\n".join(out)


def speakable(text: str, limit: int) -> tuple:
    """Reduce a reply to something worth hearing, plus the leftover."""
    text = re.sub(r"```.*?```", " ... code block ... ", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)       # list markers
    text = re.sub(r"[*_#]{1,3}", "", text)                      # markdown
    text = re.sub(r"\s*\n\s*", ". ", text)                     # lines -> sentences
    text = re.sub(r"(?:\.\s*){2,}", ". ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text, ""
    cut = text.rfind(". ", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut + 1].strip(), text[cut + 1:].strip()


__version__ = "1.0"

CONFIG_PATH = os.path.expanduser("~/.config/voicepilot/config.json")

# settings worth remembering between runs; the flags that set them are listed
# so an explicit flag always beats the saved value
CONFIG_KEYS = {
    "name": ("--name",), "user": ("--user",), "wake": ("--wake", "--no-wake"),
    "voice": ("--voice",), "rate": ("--rate",), "tts": ("--tts",),
    "stt": ("--stt",), "stt_model": ("--stt-model",),
    "language": ("--language",), "idle": ("--idle",),
    "reply_chars": ("--reply-chars",), "greeting": ("--greeting",),
    "ready_announce": ("--ready-announce",), "announce": ("--announce",),
    "followup": ("--followup",),
}


def load_config() -> dict:
    try:
        import json
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(args) -> str:
    import json
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    data = {k: getattr(args, k) for k in CONFIG_KEYS if getattr(args, k, None) is not None}
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return CONFIG_PATH


def _flag_given(flags: tuple) -> bool:
    return any(a == f or a.startswith(f + "=") for a in sys.argv[1:] for f in flags)


def daypart() -> str:
    h = time.localtime().tm_hour
    return "morning" if h < 12 else "afternoon" if h < 18 else "evening"


def personalize(text: str, name: str, user: str) -> str:
    """Fill {name}/{user}/{daypart} in, and tidy up if a name is unset.

    Placeholders belong at the end of a clause - "ready, {user}." - so that an
    empty name leaves a stray comma this can clean away rather than a gap.
    """
    try:
        out = text.format(name=name, user=user, daypart=daypart())
    except (KeyError, IndexError):
        return text
    out = re.sub(r",\s*([.?!,])", r"\1", out)
    out = re.sub(r"\s+([.?!,])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip()


CYA = "\x1b[36m"
MAG = "\x1b[35m"


class Trace:
    """Live commentary on the decisions voicepilot is making.

    Writes to stderr so it can be redirected away from the agent's TUI:
        voicepilot --trace claude 2> trace.log
    """

    def __init__(self, enabled: bool = False):
        self.on = enabled
        self.t0 = time.time()

    def __bool__(self) -> bool:
        return self.on

    def __call__(self, tag: str, msg: str) -> None:
        if not self.on:
            return
        try:
            os.write(2, f"\r\n{CYA}{time.time() - self.t0:7.2f}s "
                        f"{MAG}{tag:<9}{RST} {msg}\r\n".encode())
        except OSError:
            pass


TRACE = Trace(False)


def enable_windows_ansi() -> None:
    """Legacy Windows consoles print escape codes literally; opt into VT."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleOutputCP(65001)
        for std in (-11, -12):                     # stdout, stderr
            handle = kernel.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass                                       # cosmetic only


def status(msg: str) -> None:
    """Print a voicepilot status line without wrecking the child's TUI."""
    try:
        os.write(1, f"\r\n{DIM}[voice]{RST} {msg}\r\n".encode())
    except OSError:
        pass


def write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            n = os.write(fd, data)
        except (BlockingIOError, InterruptedError):
            time.sleep(0.005)
            continue
        data = data[n:]


ANSI_TXT_RE = re.compile(ANSI_RE.pattern.decode("ascii"))


def visible(chunk: bytes) -> str:
    return ANSI_RE.sub(b"", chunk).decode("utf-8", "ignore")


# -------------------------------------------------------------------- speech --

class Speaker:
    """Text to speech. Uses whatever the OS already has before any pip deps."""

    def __init__(self, backend: str = "auto", voice: str | None = None,
                 rate: int | None = None, enabled: bool = True):
        self.voice = voice
        self.rate = rate
        self.enabled = enabled
        self.backend = self._detect() if backend == "auto" else backend
        self._proc: subprocess.Popen | None = None
        self._engine = None

    @staticmethod
    def _detect() -> str:
        if sys.platform == "darwin" and shutil.which("say"):
            return "say"
        for cmd in ("espeak-ng", "espeak", "spd-say"):
            if shutil.which(cmd):
                return cmd
        if sys.platform.startswith("win"):
            return "powershell"
        try:
            import pyttsx3  # noqa: F401
            return "pyttsx3"
        except Exception:
            return "none"

    @property
    def available(self) -> bool:
        return self.enabled and self.backend != "none"

    def say(self, text: str) -> None:
        """Speak `text`, blocking until done."""
        if not self.available or not text:
            return
        b = self.backend
        try:
            if b == "say":
                cmd = ["say"]
                if self.voice:
                    cmd += ["-v", self.voice]
                if self.rate:
                    cmd += ["-r", str(self.rate)]
                cmd.append(text)
                self._run(cmd)
            elif b in ("espeak-ng", "espeak"):
                cmd = [b, "-s", str(self.rate or 175)]
                if self.voice:
                    cmd += ["-v", self.voice]
                cmd.append(text)
                self._run(cmd)
            elif b == "spd-say":
                self._run(["spd-say", "-w", text])
            elif b == "powershell":
                ps = ("Add-Type -AssemblyName System.Speech;"
                      "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                      f"$s.Speak('{text.replace(chr(39), chr(39) * 2)}')")
                self._run(["powershell", "-NoProfile", "-Command", ps])
            elif b == "pyttsx3":
                import pyttsx3
                if self._engine is None:
                    self._engine = pyttsx3.init()
                    if self.rate:
                        self._engine.setProperty("rate", self.rate)
                    if self.voice:
                        self._engine.setProperty("voice", self.voice)
                self._engine.say(text)
                self._engine.runAndWait()
        except Exception as e:  # never let TTS kill the session
            status(f"{YEL}tts failed ({e}){RST}")

    def _run(self, cmd: list[str]) -> None:
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self._proc.wait()
        self._proc = None

    def stop(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------- microphone --

class Recorder:
    """Records from the default mic and stops automatically on silence."""

    MIN_THRESHOLD = 300.0   # int16 RMS floor, keeps room tone from triggering

    def __init__(self, samplerate: int = 16000, device=None,
                 threshold: float | None = None):
        self.samplerate = samplerate
        # argparse supplies strings; sounddevice treats '1' as a device NAME.
        self.device = int(device) if isinstance(device, str) and device.isdecimal() else device
        self.threshold = threshold

    def input_samplerate(self) -> int:
        """Use the mic's native rate when its driver rejects 16 kHz."""
        import sounddevice as sd
        try:
            sd.check_input_settings(device=self.device, channels=1,
                                    dtype="int16", samplerate=self.samplerate)
            return self.samplerate
        except sd.PortAudioError:
            rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
            sd.check_input_settings(device=self.device, channels=1,
                                    dtype="int16", samplerate=rate)
            return rate

    @staticmethod
    def available() -> bool:
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            return True
        except Exception:
            return False

    def record(self, *, max_wait: float = 8.0, silence: float = 1.2,
               max_len: float = 60.0, min_len: float = 0.4,
               should_cancel=None, on_speech_start=None) -> str | None:
        """Wait for speech, record it, return a path to a wav file (or None)."""
        import numpy as np
        import sounddevice as sd

        samplerate = self.input_samplerate()
        frame_ms = 30
        blocksize = int(samplerate * frame_ms / 1000)
        frame_s = blocksize / samplerate

        q: queue.Queue[bytes] = queue.Queue()

        def cb(indata, frames, time_info, st):
            q.put(bytes(indata))

        preroll: deque[bytes] = deque(maxlen=10)   # keeps the first syllable
        frames: list[bytes] = []
        noise: list[float] = []
        started = False
        silence_run = 0.0
        total = 0.0
        t0 = time.time()
        last_frame = time.monotonic()
        thresh = self.threshold or self.MIN_THRESHOLD

        with sd.RawInputStream(samplerate=samplerate, blocksize=blocksize,
                               dtype="int16", channels=1, device=self.device,
                               callback=cb):
            while True:
                if should_cancel and should_cancel():
                    return None
                try:
                    buf = q.get(timeout=0.25)
                except queue.Empty:
                    if time.monotonic() - last_frame > 5.0:
                        raise RuntimeError("microphone stopped delivering audio; check the input device and permissions")
                    if not started and time.time() - t0 > max_wait:
                        return None
                    continue

                last_frame = time.monotonic()
                arr = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0

                if not started:
                    preroll.append(buf)
                    if len(noise) < 12:
                        noise.append(rms)
                        continue
                    if self.threshold is None:
                        floor = sum(noise) / len(noise)
                        thresh = max(self.MIN_THRESHOLD, floor * 3.5)
                    if rms > thresh:
                        started = True
                        frames.extend(preroll)
                        total = len(preroll) * frame_s
                        TRACE("mic", f"speech starts: level {rms:.0f} > "
                                     f"threshold {thresh:.0f}")
                        if on_speech_start:
                            on_speech_start()
                    elif time.time() - t0 > max_wait:
                        return None
                else:
                    frames.append(buf)
                    total += frame_s
                    silence_run = 0.0 if rms > thresh * 0.65 else silence_run + frame_s
                    if total >= min_len and silence_run >= silence:
                        TRACE("mic", f"{silence_run:.1f}s of silence ends it: "
                                     f"{total:.1f}s captured")
                        break
                    if total >= max_len:
                        TRACE("mic", f"hit the {max_len}s cap")
                        break

        if not frames:
            return None
        path = tempfile.mktemp(prefix="voicepilot-", suffix=".wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(samplerate)
            w.writeframes(b"".join(frames))
        return path


# ------------------------------------------------------------------- whisper --

class Transcriber:
    """Speech to text. Local faster-whisper first, cloud/Google as fallbacks."""

    # bigger is more accurate and slower; these are the sane rungs
    LADDER = ("tiny.en", "base.en", "small.en", "medium.en", "large-v3")
    MLX_DEFAULT = "mlx-community/whisper-large-v3-turbo"
    PARAKEET_DEFAULT = "mlx-community/parakeet-tdt-0.6b-v2"

    def __init__(self, backend: str = "auto", model: str | None = None,
                 language: str | None = "en", beam: int = 5,
                 bias: str | None = None):
        self.model_name = model
        self.language = language
        self.beam = beam
        # a hint of the expected vocabulary; only safe for a closed command
        # set, since it biases the decoder toward these words
        self.bias = bias
        self.backend = self._detect() if backend == "auto" else backend
        self._model = None

    @staticmethod
    def available_backends() -> list:
        found = []
        for mod, name in (("faster_whisper", "faster-whisper"),
                          ("parakeet_mlx", "parakeet-mlx"),
                          ("mlx_whisper", "mlx-whisper"),
                          ("whisper", "whisper")):
            try:
                __import__(mod)
                found.append(name)
            except Exception:
                pass
        if os.environ.get("OPENAI_API_KEY"):
            try:
                __import__("openai")
                found.append("openai")
            except Exception:
                pass
        return found

    @staticmethod
    def _detect() -> str:
        try:
            import faster_whisper  # noqa: F401
            return "faster-whisper"
        except Exception:
            pass
        if os.environ.get("OPENAI_API_KEY"):
            try:
                import openai  # noqa: F401
                return "openai"
            except Exception:
                pass
        try:
            import speech_recognition  # noqa: F401
            return "google"
        except Exception:
            pass
        try:
            import whisper  # noqa: F401
            return "whisper"
        except Exception:
            return "none"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def warm(self) -> None:
        """Load the local model up front so the first prompt isn't slow."""
        if self.backend == "faster-whisper":
            self._load_fw()
        elif self.backend == "whisper":
            self._load_ow()
        elif self.backend == "parakeet-mlx":
            self._load_pk()
        elif self.backend == "mlx-whisper":
            pass          # loads and caches on first transcribe

    def _load_fw(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name or "small.en",
                                       device="auto", compute_type="int8")
        return self._model

    def _load_ow(self):
        if self._model is None:
            import whisper
            self._model = whisper.load_model(self.model_name or "small.en")
        return self._model

    def _load_pk(self):
        if self._model is None:
            from parakeet_mlx import from_pretrained
            self._model = from_pretrained(self.model_name or self.PARAKEET_DEFAULT)
        return self._model

    def transcribe(self, wav_path: str) -> str:
        t0 = time.time()
        text = self._transcribe(wav_path)
        if TRACE:
            TRACE("recognise", f"{self.backend}"
                               f"{' ' + self.model_name if self.model_name else ''}"
                               f" took {time.time() - t0:.1f}s"
                               f"{' (biased)' if self.bias else ''}")
            TRACE("  text", repr(text))
        return text

    def _transcribe(self, wav_path: str) -> str:
        b = self.backend
        if b == "faster-whisper":
            segs, _ = self._load_fw().transcribe(
                wav_path, language=self.language, vad_filter=True,
                beam_size=self.beam, initial_prompt=self.bias,
                condition_on_previous_text=False)
            return "".join(s.text for s in segs).strip()
        if b == "parakeet-mlx":
            return self._load_pk().transcribe(wav_path).text.strip()
        if b == "mlx-whisper":
            import mlx_whisper
            r = mlx_whisper.transcribe(
                wav_path, path_or_hf_repo=self.model_name or self.MLX_DEFAULT,
                language=self.language, initial_prompt=self.bias)
            return str(r.get("text", "")).strip()
        if b == "whisper":
            r = self._load_ow().transcribe(wav_path, language=self.language,
                                           fp16=False)
            return str(r.get("text", "")).strip()
        if b == "openai":
            from openai import OpenAI
            client = OpenAI()
            with open(wav_path, "rb") as f:
                kw = {"model": self.model_name or "whisper-1", "file": f}
                if self.language:
                    kw["language"] = self.language
                r = client.audio.transcriptions.create(**kw)
            return (r.text or "").strip()
        if b == "google":
            import speech_recognition as sr
            rec = sr.Recognizer()
            with sr.AudioFile(wav_path) as src:
                audio = rec.record(src)
            return rec.recognize_google(audio, language=self.language or "en-US").strip()
        return ""


# ------------------------------------------------------------------- session --

class Session:
    def __init__(self, argv: list[str], args):
        self.argv = argv
        self.a = args

        self.speaker = Speaker(args.tts, args.voice, args.rate,
                               enabled=not args.no_speak)
        self.recorder = Recorder(threshold=args.mic_threshold,
                                 device=args.mic_device)
        self.stt = Transcriber(args.stt, args.stt_model, args.language,
                               beam=args.beam)

        prof = agent_profile(argv)
        from_config = getattr(args, "_config_keys", set())
        for key, value in prof.items():
            if not _flag_given(CONFIG_KEYS.get(key, (f"--{key.replace('_', '-')}",))) \
                    and key not in from_config:
                setattr(self.a, key, value)
        self.profile = prof

        self.master_fd = -1
        self.windows_terminal = None
        self.pid = -1
        self.alive = True
        self.exit_status: int | None = None

        self.lock = threading.Lock()          # guards writes to the pty
        self.voice_thread: threading.Thread | None = None
        self.cancel = threading.Event()

        self.auto_voice = not args.manual
        self.last_output = time.time()
        self.announced = False                # already prompted for this idle
        self.user_typed = False               # user is composing, stay out of it
        self.frames: deque = deque(maxlen=400)  # (ts, text) of recent output
        self.screen = Screen()
        self._output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        if re.sub(r"\.(exe|cmd|bat|ps1)$", "", os.path.basename(argv[0]).lower()) == "codex":
            try:
                from codex_screen import CodexScreen
                size = shutil.get_terminal_size((100, 30))
                self.screen = CodexScreen(size.columns, size.lines)
            except ImportError:
                pass
        self.reply_head = ""      # what we just read out
        self.reply_tail = ""      # what "read more" would continue with
        self.started_at = time.time()
        self.turns = 0
        self.winch = False

    # -- helpers ----------------------------------------------------------

    @property
    def voice_busy(self) -> bool:
        return self.voice_thread is not None and self.voice_thread.is_alive()

    def recent_text(self, window: float = 1.5) -> str:
        """Text from the agent's final burst of output - its last drawn screen.

        A TUI repaints in one burst, so everything within `window` of the last
        write approximates what is on screen now. Matching the whole scrollback
        instead would keep hitting stale text the agent has already erased.
        """
        if getattr(self.screen, "uses_ansi", False):
            return "\n".join(self.screen.tail_lines(30))[-4000:]
        if not self.frames:
            return ""
        cutoff = self.frames[-1][0] - window
        return "".join(t for ts, t in self.frames if ts >= cutoff)[-4000:]

    def send(self, text: str, submit: bool = True) -> None:
        """Type `text` into the child agent as if the user had typed it."""
        TRACE("inject", f"typing {text!r}"
                        f"{' + enter' if submit else ''} into the agent")
        with self.lock:
            self.screen.mark(text)
            data = text.encode()
            if getattr(self.screen, "uses_ansi", False):
                # Codex batches rapid keystrokes as a paste. Explicit boundaries
                # keep Enter from being swallowed by that paste-detection window.
                data = b"\x1b[200~" + data + b"\x1b[201~"
            self._write(data)
            if submit:
                time.sleep(self.a.submit_delay)
                self._write(self.a.submit_key.encode())
                if getattr(self.screen, "uses_ansi", False):
                    time.sleep(0.8)
                    # A busy ConPTY can defer the paste event. Retry Enter only
                    # if the exact dictated text still occupies the composer;
                    # never press it on a permission dialog or a running task.
                    if self.screen.input_text() == " ".join(text.split()):
                        self._write(self.a.submit_key.encode())
            # The old response can still be visible until the agent redraws.
            # Give the new request a full idle interval before considering it done.
            self.last_output = time.time()
            self.announced = False

    def send_raw(self, data: bytes) -> None:
        with self.lock:
            self._write(data)

    def _write(self, data: bytes) -> None:
        if self.windows_terminal is not None:
            self.windows_terminal.write(data)
        else:
            write_all(self.master_fd, data)

    # -- the voice turn ---------------------------------------------------

    def start_voice_turn(self, reason: str = "idle") -> None:
        if self.voice_busy:
            return
        self.announced = True
        self.cancel.clear()
        self.voice_thread = threading.Thread(target=self._voice_turn,
                                             args=(reason,), daemon=True)
        self.voice_thread.start()

    def _prepare_reply(self, reason: str) -> None:
        """Pull what the agent just said, ready to be read out."""
        self.reply_head = self.reply_tail = ""
        if not self.a.read_reply or reason == "manual" or not self.screen.marked:
            return
        full = self.screen.reply()
        if full:
            self.reply_head, self.reply_tail = speakable(full, self.a.reply_chars)

    def _tmpl(self, text: str) -> str:
        return personalize(text, self.a.name, self.a.user)

    def _announce_line(self, reason: str, approval: bool) -> str:
        if reason == "manual":
            return self._tmpl(self.a.listen_prompt)
        if approval:
            head = self._tmpl(self.a.approve_announce)
            return f"{head} {self.reply_head}" if self.reply_head else head
        if self.reply_head:
            # a reply ending in a question is already the prompt to answer,
            # so don't talk over it with "what's next?"
            if self.reply_head.rstrip().endswith("?"):
                return self.reply_head
            return f"{self.reply_head} {self._tmpl(self.a.followup)}"
        if self.turns == 0:
            return self._tmpl(self.a.ready_announce)
        return self._tmpl(self.a.announce)

    def _voice_turn(self, reason: str) -> None:
        try:
            approval = bool(APPROVAL_RE.search(self.recent_text()[-800:]))
            self._prepare_reply(reason)
            line = self._announce_line(reason, approval)

            shown = line if len(line) <= 300 else line[:300] + "..."
            status(f"{GRN}{shown}{RST}")
            self.speaker.say(line)
            if self.cancel.is_set():
                return

            while True:                      # "repeat"/"read more" stay in the turn
                text = self._listen_with_retries()
                if self.cancel.is_set():
                    status(f"{DIM}cancelled - type instead{RST}")
                    return
                if not text:
                    status(f"{DIM}nothing heard - going quiet until the agent "
                           f"replies (Ctrl-] to talk){RST}")
                    return

                status(f'{YEL}heard:{RST} "{text}"')
                if self.a.log:
                    with open(self.a.log, "a") as f:
                        f.write(f"{time.strftime('%F %T')}\t{text}\n")

                if self._dispatch(text, approval):
                    return
        except Exception as e:
            status(f"{RED}voice turn failed: {e}{RST}")

    def _listen_with_retries(self) -> str:
        for attempt in range(self.a.retries + 1):
            text = self._listen_once()
            if text or self.cancel.is_set():
                return text
            if attempt < self.a.retries:
                self.speaker.say("I didn't catch that. Try again.")
        return ""

    def _listen_once(self) -> str:
        if not self.recorder.available():
            status(f"{RED}mic unavailable - pip install sounddevice numpy{RST}")
            return ""
        status(f"{DIM}listening... (speak, then pause){RST}")
        wav = self.recorder.record(
            max_wait=self.a.listen_wait,
            silence=self.a.silence,
            max_len=self.a.max_utterance,
            should_cancel=self.cancel.is_set,
            on_speech_start=lambda: status(f"{DIM}recording...{RST}"),
        )
        if not wav:
            return ""
        try:
            if not self.stt.available:
                status(f"{RED}no speech-to-text backend installed{RST}")
                return ""
            status(f"{DIM}transcribing ({self.stt.backend})...{RST}")
            return self.stt.transcribe(wav)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass

    def _dispatch(self, text: str, approval: bool) -> bool:
        """Act on what was heard. False means keep listening in this turn."""
        norm = re.sub(r"[^a-z0-9' ]", "", text.lower()).strip()

        if norm in REPEAT_WORDS:
            self.speaker.say(self.reply_head or "There was nothing to repeat.")
            return False
        if norm in MORE_WORDS:
            if self.reply_tail:
                self.reply_head, self.reply_tail = speakable(
                    self.reply_tail, self.a.reply_chars)
                status(f"{GRN}{self.reply_head[:300]}{RST}")
                self.speaker.say(self.reply_head)
            else:
                self.speaker.say("That was all of it.")
            return False

        if norm in QUIT_WORDS:
            self.auto_voice = False
            status(f"{YEL}auto voice off - Ctrl-\\ to re-enable{RST}")
            self.speaker.say("Voice mode off.")
            return True
        if norm in SKIP_WORDS:
            status(f"{DIM}skipped{RST}")
            return True
        if norm in ("interrupt", "stop it", "abort", "escape"):
            self.send_raw(b"\x1b")
            status(f"{DIM}sent escape{RST}")
            return True
        if norm in ("exit", "quit the agent", "close the agent"):
            self.send("/exit")
            return True

        if approval:
            if norm in NUM_WORDS:
                self.send(NUM_WORDS[norm]); self.turns += 1; return True
            if norm in YES_WORDS:
                self.send("1"); self.turns += 1; return True
            if norm in NO_WORDS:
                self.send_raw(b"\x1b"); self.turns += 1; return True

        # "literally ..." / "say ..." escapes the command vocabulary
        for prefix in ("literally ", "just say ", "type "):
            if norm.startswith(prefix):
                text = text[len(prefix):]
                break

        text = " ".join(text.split())
        if self.a.confirm:
            self.speaker.say(f"Send: {text}?")
            ok = re.sub(r"[^a-z ]", "", (self._listen_once() or "").lower()).strip()
            if ok not in YES_WORDS:
                status(f"{DIM}not sent{RST}")
                return True

        self.send(text)
        self.turns += 1
        status(f"{GRN}sent{RST}")
        return True

    # -- pty plumbing -----------------------------------------------------

    def _sync_winsize(self) -> None:
        try:
            s = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, s)
            if hasattr(self.screen, "resize"):
                import struct
                rows, columns, _, _ = struct.unpack("HHHH", s)
                if rows and columns:
                    self.screen.resize(columns, rows)
        except OSError:
            pass

    def _handle_stdin(self, data: bytes) -> None:
        if HOTKEY_TOGGLE in data:
            data = data.replace(HOTKEY_TOGGLE, b"")
            self.auto_voice = not self.auto_voice
            status(f"auto voice {GRN + 'ON' + RST if self.auto_voice else YEL + 'OFF' + RST}")
        if HOTKEY_TALK in data:
            data = data.replace(HOTKEY_TALK, b"")
            if self.voice_busy:
                self.cancel.set()
                self.speaker.stop()
                status(f"{DIM}listen cancelled{RST}")
            else:
                self.start_voice_turn("manual")
        if not data:
            return
        # any real keystroke means the human took over: stop listening,
        # and don't nag them with an announcement while they compose
        if self.voice_busy:
            self.cancel.set()
            self.speaker.stop()
        self.user_typed = True
        if b"\r" in data or b"\n" in data:
            self.screen.mark()
        self.send_raw(data)

    def _handle_output(self, chunk: bytes) -> None:
        write_all(1, chunk)
        text = visible(chunk)
        # pure cursor moves / redraws don't count as the agent "working"
        self.screen.feed(self._output_decoder.decode(chunk)
                         if getattr(self.screen, "uses_ansi", False) else text)
        if text.strip():
            self.last_output = time.time()
            self.frames.append((self.last_output, text))
            self.announced = False
            self.user_typed = False

    def _should_announce(self) -> bool:
        if not self.auto_voice or self.announced or self.voice_busy:
            return False
        if self.user_typed:
            return False
        now = time.time()
        if now - self.started_at < self.a.startup_grace:
            return False
        if getattr(self.screen, "uses_ansi", False):
            approval = bool(APPROVAL_RE.search(self.recent_text()))
            if not approval:
                if not self.screen.at_prompt():
                    return False
                if self.screen.marked and not self.screen.reply():
                    return False
        low = self.recent_text()[-800:].lower()
        quiet = now - self.last_output
        if self.screen.at_prompt():
            # Its input prompt is on screen, so it is waiting on us - this
            # outranks the interrupt hint, which lingers in the same burst of
            # output the agent used to erase it. Checking busy first made it
            # sit out the full busy timeout after every single task.
            need, why = self.a.prompt_idle, "input prompt visible"
        elif any(m in low for m in BUSY_MARKERS):
            # no prompt, but still showing an interrupt hint: probably mid-task
            # and silent inside a long tool call, so hold off much longer
            need, why = self.a.busy_idle, "interrupt hint, no prompt"
        else:
            need = self.a.idle
            why = "output quiet"
        ready = quiet >= need
        if TRACE and ready:
            TRACE("idle", f"{why}: quiet {quiet:.1f}s >= {need}s -> speak")
        return ready

    def _drain(self) -> None:
        """Flush whatever the agent wrote just before it exited."""
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
                if not r:
                    return
                chunk = os.read(self.master_fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            write_all(1, chunk)

    # -- main loop --------------------------------------------------------

    def _run_windows(self) -> int:
        from windows_terminal import WindowsTerminal, WindowsInput
        size = shutil.get_terminal_size((100, 30))
        try:
            terminal = WindowsTerminal(self.argv, size.columns, size.lines)
        except Exception as e:
            status(f"could not start agent: {e}")
            return 127
        self.windows_terminal = terminal
        self.pid = terminal.pty.pid
        status(f"{GRN}{self.a.name} on{RST} - {' '.join(self.argv)} "
               "[Windows ConPTY] Ctrl-] talk, Ctrl-\\ toggle")
        ended_at = None
        try:
            with WindowsInput() as keyboard:
                while self.alive:
                    try:
                        chunk = terminal.read()
                    except EOFError:
                        chunk = ""
                    if chunk:
                        self._handle_output(chunk.encode("utf-8"))
                    if not terminal.isalive():
                        # ConPTY may deliver its final output after child exit.
                        if ended_at is None or chunk:
                            ended_at = time.monotonic()
                        if time.monotonic() - ended_at > 0.3:
                            break
                    else:
                        data = keyboard.read()
                        if data:
                            self._handle_stdin(data)
                        new_size = shutil.get_terminal_size((100, 30))
                        if new_size != size:
                            terminal.resize(new_size.columns, new_size.lines)
                            if hasattr(self.screen, "resize"):
                                self.screen.resize(new_size.columns, new_size.lines)
                            size = new_size
                        if self._should_announce():
                            self.start_voice_turn("idle")
                    time.sleep(0.02)
        finally:
            self.alive = False
            self.cancel.set()
            self.speaker.stop()
            self.exit_status = terminal.exit_code()
            terminal.close()
            self.windows_terminal = None
            status(f"session ended - {self.turns} spoken prompt(s) sent")
        return self.exit_status if self.exit_status is not None else 0

    def run(self) -> int:
        if not sys.stdin.isatty():
            print("voicepilot needs an interactive terminal.", file=sys.stderr)
            return 2

        if self.stt.available and self.a.warm:
            status(f"{DIM}loading speech model ({self.stt.backend})...{RST}")
            try:
                self.stt.warm()
            except Exception as e:
                status(f"{YEL}model load failed: {e}{RST}")

        if os.name == "nt":
            return self._run_windows()

        old = termios.tcgetattr(0)
        self.pid, self.master_fd = pty.fork()
        if self.pid == 0:
            os.environ["VOICEPILOT"] = "1"
            try:
                os.execvp(self.argv[0], self.argv)
            except FileNotFoundError:
                sys.stderr.write(f"voicepilot: {self.argv[0]}: not found\n")
            os._exit(127)

        self._sync_winsize()
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "winch", True))

        try:
            tty.setraw(0)
            tuned = " tuned" if self.profile else ""
            status(f"{GRN}{self.a.name} on{RST} - {' '.join(self.argv)}  "
                   f"{DIM}[tts:{self.speaker.backend} stt:{self.stt.backend} "
                   f"idle:{self.a.idle}s{tuned}] Ctrl-] talk, Ctrl-\\ toggle{RST}")
            while self.alive:
                if self.winch:
                    self.winch = False
                    self._sync_winsize()
                try:
                    r, _, _ = select.select([0, self.master_fd], [], [], 0.2)
                except (InterruptedError, OSError):
                    continue

                if 0 in r:
                    try:
                        data = os.read(0, 8192)
                    except OSError:
                        data = b""
                    if data:
                        self._handle_stdin(data)

                if self.master_fd in r:
                    try:
                        chunk = os.read(self.master_fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        self.alive = False
                    else:
                        self._handle_output(chunk)

                if self._should_announce():
                    self.start_voice_turn("idle")

                pid, st = os.waitpid(self.pid, os.WNOHANG)
                if pid == self.pid:
                    self.exit_status = st
                    self._drain()
                    self.alive = False
        except KeyboardInterrupt:
            self.send_raw(b"\x03")
        finally:
            self.cancel.set()
            self.speaker.stop()
            termios.tcsetattr(0, termios.TCSAFLUSH, old)
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            if self.exit_status is None:
                try:
                    _, self.exit_status = os.waitpid(self.pid, 0)
                except (ChildProcessError, OSError):
                    pass
            code = (os.waitstatus_to_exitcode(self.exit_status)
                    if self.exit_status is not None else 0)
            print(f"\n{DIM}[voice] session ended - {self.turns} spoken "
                  f"prompt(s) sent{RST}")
        return code


# ------------------------------------------------------------------ desktop --

# what people actually say -> what the OS calls it
APP_ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "code": "Visual Studio Code", "system settings": "System Settings",
    "system preferences": "System Settings", "settings": "System Settings",
    "activity monitor": "Activity Monitor", "app store": "App Store",
    "quicktime": "QuickTime Player", "preview": "Preview",
    "notes": "Notes", "mail": "Mail", "messages": "Messages",
    "calendar": "Calendar", "reminders": "Reminders", "finder": "Finder",
    "terminal": "Terminal", "iterm": "iTerm", "safari": "Safari",
    "spotify": "Spotify", "slack": "Slack", "discord": "Discord",
    "zoom": "zoom.us", "firefox": "Firefox", "notion": "Notion",
    "explorer": "explorer", "file explorer": "explorer",
    "notepad": "notepad", "task manager": "taskmgr", "edge": "msedge",
}

# overrides where the launcher name differs from the macOS application name
APP_ALIASES_OS = {
    "win": {
        "chrome": "chrome", "google chrome": "chrome", "firefox": "firefox",
        "vs code": "code", "vscode": "code", "code": "code",
        "terminal": "wt", "settings": "ms-settings:",
        "system settings": "ms-settings:", "system preferences": "ms-settings:",
        "spotify": "spotify", "slack": "slack", "discord": "discord",
        "notion": "notion", "zoom": "zoom", "edge": "msedge",
        "mail": "outlookmail:", "calendar": "outlookcal:", "notes": "notepad",
        "finder": "explorer", "preview": "photos:", "app store": "ms-windows-store:",
    },
    "linux": {
        "chrome": "google-chrome", "google chrome": "google-chrome",
        "firefox": "firefox", "vs code": "code", "vscode": "code", "code": "code",
        "terminal": "x-terminal-emulator", "finder": "nautilus",
        "files": "nautilus", "settings": "gnome-control-center",
        "system settings": "gnome-control-center", "spotify": "spotify",
        "slack": "slack", "discord": "discord", "calendar": "gnome-calendar",
    },
}

# Windows virtual-key codes, for the chords SendKeys cannot express
VK = {"win": 0x5B, "alt": 0x12, "ctrl": 0x11, "shift": 0x10, "tab": 0x09,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "f4": 0x73, "f11": 0x7A, "d": 0x44}

MAC_KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "left": 123, "right": 124,
    "down": 125, "up": 126, "home": 115, "end": 119,
    "page up": 116, "page down": 121,
}
WIN_VKEYS = {
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "escape": 0x1B, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
    "page up": 0x21, "page down": 0x22,
}


class DesktopError(Exception):
    pass


class Desktop:
    """Drives the machine itself - apps, cursor, keyboard, volume.

    Native APIs only, so solo mode adds no dependencies: CoreGraphics via
    ctypes plus `osascript` on macOS, user32 on Windows, xdotool on Linux.
    """

    def __init__(self):
        if sys.platform == "darwin":
            self.os = "mac"
        elif sys.platform.startswith("win"):
            self.os = "win"
        else:
            self.os = "linux"
        self._cg = None
        self._u32 = None

    # -- platform handles -------------------------------------------------

    @property
    def cg(self):
        """CoreGraphics, loaded lazily so non-mac platforms never touch it."""
        if self._cg is None:
            import ctypes
            from ctypes import util as cutil

            class CGPoint(ctypes.Structure):
                _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

            lib = ctypes.CDLL(cutil.find_library("ApplicationServices"))
            lib.CGWarpMouseCursorPosition.argtypes = [CGPoint]
            lib.CGEventCreate.restype = ctypes.c_void_p
            lib.CGEventCreate.argtypes = [ctypes.c_void_p]
            lib.CGEventGetLocation.restype = CGPoint
            lib.CGEventGetLocation.argtypes = [ctypes.c_void_p]
            lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
            lib.CGEventCreateMouseEvent.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
            lib.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
            lib.CGEventCreateScrollWheelEvent.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
            lib.CGEventSetIntegerValueField.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
            lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            lib.CFRelease.argtypes = [ctypes.c_void_p]
            lib.CGMainDisplayID.restype = ctypes.c_uint32
            lib.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
            lib.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]

            class CGSize(ctypes.Structure):
                _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]

            class CGRect(ctypes.Structure):
                _fields_ = [("origin", CGPoint), ("size", CGSize)]

            lib.CGDisplayBounds.restype = CGRect
            lib.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            lib.CGGetActiveDisplayList.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32)]
            self._cg = (lib, CGPoint)
        return self._cg

    @property
    def u32(self):
        if self._u32 is None:
            import ctypes
            self._u32 = ctypes.windll.user32
        return self._u32

    def _sh(self, cmd: list) -> None:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise DesktopError((r.stderr or r.stdout).strip().split("\n")[-1])

    def _osa(self, script: str) -> None:
        self._sh(["osascript", "-e", script])

    @staticmethod
    def _osa_str(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def _app(self, name: str) -> str:
        """Resolve a spoken app name to what this OS actually launches."""
        key = name.lower().strip()
        return (APP_ALIASES_OS.get(self.os, {}).get(key)
                or APP_ALIASES.get(key)
                or name.strip())

    def _win_chord(self, *keys: str) -> None:
        """Hold modifiers, tap the key, release. SendKeys cannot do Win+ or
        Alt+Tab, so those have to go through the raw key event API."""
        codes = [VK[k] for k in keys]
        for c in codes:
            self.u32.keybd_event(c, 0, 0, 0)
        for c in reversed(codes):
            self.u32.keybd_event(c, 0, 2, 0)

    def _xdo(self, *args: str) -> None:
        if not shutil.which("xdotool"):
            raise DesktopError("xdotool is not installed (sudo apt install xdotool)")
        self._sh(["xdotool", *args])

    # -- apps -------------------------------------------------------------

    def open_app(self, name: str) -> str:
        app = self._app(name)
        if self.os == "mac":
            self._sh(["open", "-a", app])
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command",
                      f"Start-Process '{app}'"])
        else:
            exe = shutil.which(app) or shutil.which(app.lower())
            if not exe:
                raise DesktopError(f"no such command: {app}")
            subprocess.Popen([exe], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opening {app}"

    def quit_app(self, name: str) -> str:
        app = self._app(name)
        if self.os == "mac":
            self._osa(f'quit app "{self._osa_str(app)}"')
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command",
                      f"Stop-Process -Name '{app}' -ErrorAction Stop"])
        else:
            self._sh(["pkill", "-f", app])
        return f"Closing {app}"

    def web_search(self, query: str) -> str:
        import urllib.parse
        url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
        if self.os == "mac":
            self._sh(["open", url])
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command", f"Start-Process '{url}'"])
        else:
            self._sh(["xdg-open", url])
        return f"Searching for {query}"

    # -- pointer ----------------------------------------------------------

    def screen_size(self) -> tuple:
        if self.os == "mac":
            lib, _ = self.cg
            d = lib.CGMainDisplayID()
            return int(lib.CGDisplayPixelsWide(d)), int(lib.CGDisplayPixelsHigh(d))
        if self.os == "win":
            return int(self.u32.GetSystemMetrics(0)), int(self.u32.GetSystemMetrics(1))
        out = subprocess.run(["xdotool", "getdisplaygeometry"],
                             capture_output=True, text=True).stdout.split()
        return (int(out[0]), int(out[1])) if len(out) == 2 else (1920, 1080)

    def screen_bounds(self) -> tuple:
        """Union of every display as (min_x, min_y, max_x, max_y).

        Not the same as the main display: a second monitor placed above or to
        the left of it lives at negative coordinates, and clamping to the main
        display alone would make it unreachable.
        """
        if self.os == "mac":
            import ctypes
            lib, _ = self.cg
            ids = (ctypes.c_uint32 * 16)()
            count = ctypes.c_uint32(0)
            lib.CGGetActiveDisplayList(16, ids, ctypes.byref(count))
            boxes = [lib.CGDisplayBounds(ids[i]) for i in range(count.value)]
            if not boxes:
                w, h = self.screen_size()
                return 0, 0, w, h
            return (min(b.origin.x for b in boxes),
                    min(b.origin.y for b in boxes),
                    max(b.origin.x + b.size.w for b in boxes),
                    max(b.origin.y + b.size.h for b in boxes))
        if self.os == "win":
            g = self.u32.GetSystemMetrics
            x, y = g(76), g(77)                    # SM_X/YVIRTUALSCREEN
            return x, y, x + g(78), y + g(79)
        w, h = self.screen_size()
        return 0, 0, w, h

    def cursor_pos(self) -> tuple:
        if self.os == "mac":
            lib, _ = self.cg
            ev = lib.CGEventCreate(None)
            pt = lib.CGEventGetLocation(ev)
            lib.CFRelease(ev)
            return int(pt.x), int(pt.y)
        if self.os == "win":
            import ctypes

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = POINT()
            self.u32.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        out = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                             capture_output=True, text=True).stdout
        d = dict(l.split("=") for l in out.strip().splitlines() if "=" in l)
        return int(d.get("X", 0)), int(d.get("Y", 0))

    def move_to(self, x: int, y: int) -> str:
        x0, y0, x1, y1 = self.screen_bounds()
        x = int(max(x0, min(int(x), x1 - 1)))
        y = int(max(y0, min(int(y), y1 - 1)))
        if self.os == "mac":
            lib, CGPoint = self.cg
            lib.CGWarpMouseCursorPosition(CGPoint(x, y))
        elif self.os == "win":
            self.u32.SetCursorPos(x, y)
        else:
            self._xdo("mousemove", str(x), str(y))
        return f"Cursor at {x}, {y}"

    def move_by(self, direction: str, amount: int = 100) -> str:
        x, y = self.cursor_pos()
        dx, dy = {"left": (-amount, 0), "right": (amount, 0),
                  "up": (0, -amount), "down": (0, amount)}[direction]
        return self.move_to(x + dx, y + dy)

    def center(self) -> str:
        w, h = self.screen_size()
        return self.move_to(w // 2, h // 2)

    def click(self, button: str = "left", count: int = 1) -> str:
        if self.os == "mac":
            lib, CGPoint = self.cg
            x, y = self.cursor_pos()
            down, up, btn = (1, 2, 0) if button == "left" else (3, 4, 1)
            for i in range(count):
                for kind in (down, up):
                    ev = lib.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), btn)
                    # click state is what turns two clicks into a double-click
                    lib.CGEventSetIntegerValueField(ev, 1, i + 1)
                    lib.CGEventPost(0, ev)
                    lib.CFRelease(ev)
                time.sleep(0.04)
        elif self.os == "win":
            flags = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
            for _ in range(count):
                for f in flags:
                    self.u32.mouse_event(f, 0, 0, 0, 0)
                time.sleep(0.04)
        else:
            self._xdo("click", "--repeat", str(count),
                      "1" if button == "left" else "3")
        what = {1: "Click", 2: "Double click"}.get(count, "Click")
        return what if button == "left" else f"Right {what.lower()}"

    def scroll(self, direction: str, amount: int = 5) -> str:
        steps = int(amount) * (1 if direction == "up" else -1)
        if self.os == "mac":
            lib, _ = self.cg
            ev = lib.CGEventCreateScrollWheelEvent(None, 1, 1, steps)
            lib.CGEventPost(0, ev)
            lib.CFRelease(ev)
        elif self.os == "win":
            self.u32.mouse_event(0x0800, 0, 0, steps * 120, 0)
        else:
            self._xdo("click", "--repeat", str(abs(steps)),
                      "4" if direction == "up" else "5")
        return f"Scrolled {direction}"

    # -- keyboard ---------------------------------------------------------

    def type_text(self, text: str) -> str:
        if self.os == "mac":
            self._osa('tell application "System Events" to keystroke "%s"'
                      % self._osa_str(text))
        elif self.os == "win":
            esc = re.sub(r"([+^%~(){}\[\]])", r"{\1}", text).replace("'", "''")
            self._sh(["powershell", "-NoProfile", "-Command",
                      "Add-Type -AssemblyName System.Windows.Forms;"
                      f"[System.Windows.Forms.SendKeys]::SendWait('{esc}')"])
        else:
            self._xdo("type", "--clearmodifiers", text)
        return f"Typed {text}"

    def press(self, key: str, modifiers: tuple = ()) -> str:
        key = key.lower().strip()
        if self.os == "mac":
            mods = " down, ".join(m for m in modifiers)
            using = f" using {{{mods} down}}" if modifiers else ""
            if key in MAC_KEYCODES:
                body = f"key code {MAC_KEYCODES[key]}{using}"
            else:
                body = f'keystroke "{self._osa_str(key)}"{using}'
            self._osa(f'tell application "System Events" to {body}')
        elif self.os == "win":
            names = {"command": "^", "control": "^", "option": "%",
                     "alt": "%", "shift": "+"}
            special = {"return": "{ENTER}", "enter": "{ENTER}", "tab": "{TAB}",
                       "escape": "{ESC}", "space": " ", "delete": "{DEL}",
                       "backspace": "{BS}", "up": "{UP}", "down": "{DOWN}",
                       "left": "{LEFT}", "right": "{RIGHT}"}
            seq = "".join(names.get(m, "") for m in modifiers) + \
                  special.get(key, key)
            self._sh(["powershell", "-NoProfile", "-Command",
                      "Add-Type -AssemblyName System.Windows.Forms;"
                      f"[System.Windows.Forms.SendKeys]::SendWait('{seq}')"])
        else:
            names = {"command": "super", "control": "ctrl", "option": "alt"}
            combo = "+".join([names.get(m, m) for m in modifiers] +
                             [{"return": "Return", "escape": "Escape"}.get(key, key)])
            self._xdo("key", "--clearmodifiers", combo)
        return f"Pressed {' '.join(modifiers)} {key}".strip()

    def shortcut(self, key: str) -> str:
        """A platform-correct 'the usual modifier' combo, e.g. copy/paste."""
        return self.press(key, ("command",) if self.os == "mac" else ("control",))

    # -- system -----------------------------------------------------------

    def volume(self, action: str) -> str:
        if self.os == "mac":
            if action == "mute":
                self._osa("set volume with output muted")
                return "Muted"
            if action == "unmute":
                self._osa("set volume without output muted")
                return "Unmuted"
            step = 15 if action == "up" else -15
            self._osa("set volume output volume "
                      f"(output volume of (get volume settings) + {step})")
            return f"Volume {action}"
        vk = {"up": 0xAF, "down": 0xAE, "mute": 0xAD, "unmute": 0xAD}[action]
        if self.os == "win":
            for _ in range(1 if action in ("mute", "unmute") else 5):
                self.u32.keybd_event(vk, 0, 0, 0)
                self.u32.keybd_event(vk, 0, 2, 0)
            return f"Volume {action}"
        self._sh(["amixer", "-q", "sset", "Master",
                  "5%+" if action == "up" else
                  "5%-" if action == "down" else "toggle"])
        return f"Volume {action}"

    def screenshot(self) -> str:
        name = time.strftime("voicepilot-%Y%m%d-%H%M%S.png")
        path = os.path.join(os.path.expanduser("~/Desktop"), name)
        if self.os == "mac":
            self._sh(["screencapture", "-x", path])
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command",
                      "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                      "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                      "$i=New-Object Drawing.Bitmap $b.Width,$b.Height;"
                      "[Drawing.Graphics]::FromImage($i)"
                      ".CopyFromScreen($b.Location,[Drawing.Point]::Empty,$b.Size);"
                      f"$i.Save('{path}')"])
        else:
            for tool in (["gnome-screenshot", "-f", path], ["scrot", path],
                         ["spectacle", "-b", "-n", "-o", path],
                         ["import", "-window", "root", path]):
                if shutil.which(tool[0]):
                    self._sh(tool)
                    break
            else:
                raise DesktopError("no screenshot tool found "
                                   "(install gnome-screenshot or scrot)")
        return "Screenshot saved to the desktop"

    # -- windows ----------------------------------------------------------

    def focus_app(self, name: str) -> str:
        app = self._app(name)
        if self.os == "mac":
            self._osa(f'tell application "{self._osa_str(app)}" to activate')
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command",
                      "(New-Object -ComObject WScript.Shell)"
                      f".AppActivate('{app}')"])
        else:
            self._sh(["wmctrl", "-a", app])
        return f"Switched to {app}"

    def window(self, action: str) -> str:
        if self.os == "mac":
            moves = {
                "minimize": ("m", ("command",)),
                "close": ("w", ("command",)),
                "fullscreen": ("f", ("control", "command")),
                "switch": ("tab", ("command",)),
                "next window": ("`", ("command",)),
            }
            if action == "maximize":       # green-button zoom has no shortcut
                self._osa('tell application "System Events" to tell '
                          '(first process whose frontmost is true) to '
                          'set value of attribute "AXFullScreen" of front window to true')
                return "Maximized"
            key, mods = moves[action]
            self.press(key, mods)
        elif self.os == "win":
            chords = {"minimize": ("win", "down"), "maximize": ("win", "up"),
                      "fullscreen": ("f11",), "close": ("alt", "f4"),
                      "switch": ("alt", "tab"), "next window": ("alt", "tab")}
            self._win_chord(*chords[action])
        else:
            keys = {"minimize": "super+h", "maximize": "super+Up",
                    "fullscreen": "F11", "close": "alt+F4",
                    "switch": "alt+Tab", "next window": "alt+grave"}
            self._xdo("key", "--clearmodifiers", keys[action])
        return action.capitalize()

    # -- media / display --------------------------------------------------

    def media(self, action: str) -> str:
        """play-pause / next / previous."""
        if self.os == "mac":
            # macOS exposes no generic media key over AppleScript, so drive
            # whichever player is actually running
            verb = {"play": "playpause", "next": "next track",
                    "previous": "previous track"}[action]
            for app in ("Spotify", "Music"):
                probe = subprocess.run(
                    ["osascript", "-e",
                     f'tell application "System Events" to (name of processes) '
                     f'contains "{app}"'], capture_output=True, text=True)
                if probe.stdout.strip() == "true":
                    self._osa(f'tell application "{app}" to {verb}')
                    return f"{action.capitalize()} in {app}"
            raise DesktopError("no music player is running")
        vk = {"play": 0xB3, "next": 0xB0, "previous": 0xB1}[action]
        if self.os == "win":
            self.u32.keybd_event(vk, 0, 0, 0)
            self.u32.keybd_event(vk, 0, 2, 0)
        else:
            self._sh(["playerctl", {"play": "play-pause", "next": "next",
                                    "previous": "previous"}[action]])
        return action.capitalize()

    def brightness(self, direction: str) -> str:
        if self.os == "mac":
            # F14/F15 are the brightness keys; not wired on every machine
            self._osa("tell application \"System Events\" to key code "
                      + ("144" if direction == "up" else "145"))
        elif self.os == "win":
            raise DesktopError("brightness control is not available on Windows here")
        else:
            self._sh(["brightnessctl", "set",
                      "10%+" if direction == "up" else "10%-"])
        return f"Brightness {direction}"

    def sleep_display(self) -> str:
        if self.os == "mac":
            self._sh(["pmset", "displaysleepnow"])
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command",
                      "(Add-Type '[DllImport(\"user32.dll\")]public static extern "
                      "int SendMessage(int hWnd,int hMsg,int wParam,int lParam);' "
                      "-Name W -PassThru)::SendMessage(-1,0x0112,0xF170,2)"])
        else:
            self._sh(["xset", "dpms", "force", "off"])
        return "Turning the display off"

    # -- clipboard / files ------------------------------------------------

    def clipboard_get(self) -> str:
        if self.os == "mac":
            r = subprocess.run(["pbpaste"], capture_output=True, text=True)
        elif self.os == "win":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-Clipboard"], capture_output=True, text=True)
        else:
            r = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                               capture_output=True, text=True)
        return r.stdout.strip()

    def clipboard_set(self, text: str) -> str:
        if self.os == "mac":
            cmd = ["pbcopy"]
        elif self.os == "win":
            cmd = ["clip"]
        else:
            cmd = ["xclip", "-selection", "clipboard"]
        subprocess.run(cmd, input=text, text=True)
        return "Copied to the clipboard"

    def open_path(self, where: str) -> str:
        spoken = where.lower().strip().rstrip(".")
        folders = {"downloads": "~/Downloads", "documents": "~/Documents",
                   "desktop": "~/Desktop", "home": "~", "pictures": "~/Pictures",
                   "music": "~/Music", "movies": "~/Movies",
                   "applications": "/Applications", "trash": "~/.Trash"}
        target = os.path.expanduser(folders.get(spoken, where))
        if not os.path.exists(target):
            raise DesktopError(f"there is no {where} folder")
        if self.os == "mac":
            self._sh(["open", target])
        elif self.os == "win":
            self._sh(["explorer", target])
        else:
            self._sh(["xdg-open", target])
        return f"Opening {spoken}"

    def open_url(self, url: str) -> str:
        url = url.strip().replace(" dot ", ".").replace(" ", "")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if self.os == "mac":
            self._sh(["open", url])
        elif self.os == "win":
            self._sh(["powershell", "-NoProfile", "-Command", f"Start-Process '{url}'"])
        else:
            self._sh(["xdg-open", url])
        return f"Opening {url}"

    # -- drag -------------------------------------------------------------

    def drag_to(self, x: int, y: int) -> str:
        sx, sy = self.cursor_pos()
        if self.os == "mac":
            lib, CGPoint = self.cg
            for kind, point in ((1, CGPoint(sx, sy)),      # left down
                                (6, CGPoint(x, y)),        # left dragged
                                (2, CGPoint(x, y))):       # left up
                ev = lib.CGEventCreateMouseEvent(None, kind, point, 0)
                lib.CGEventPost(0, ev)
                lib.CFRelease(ev)
                time.sleep(0.05)
        elif self.os == "win":
            self.u32.mouse_event(0x0002, 0, 0, 0, 0)
            self.move_to(x, y)
            self.u32.mouse_event(0x0004, 0, 0, 0, 0)
        else:
            self._xdo("mousedown", "1")
            self.move_to(x, y)
            self._xdo("mouseup", "1")
        return f"Dragged to {x}, {y}"

    def power(self, action: str) -> str:
        if self.os == "mac":
            verb = {"shutdown": "shut down", "restart": "restart",
                    "logout": "log out"}[action]
            self._osa(f'tell application "System Events" to {verb}')
        elif self.os == "win":
            flag = {"shutdown": "/s", "restart": "/r", "logout": "/l"}[action]
            self._sh(["shutdown", flag, "/t", "0"])
        else:
            cmd = {"shutdown": ["systemctl", "poweroff"],
                   "restart": ["systemctl", "reboot"],
                   "logout": ["loginctl", "terminate-user", getpass.getuser()]}[action]
            self._sh(cmd)
        return f"{action.capitalize()} now"

    def accessibility_ok(self):
        """True/False on macOS, None where the question doesn't apply.

        Moving the cursor needs no permission, but posting clicks and
        keystrokes does - so this can pass the mic check and still fail.
        """
        if self.os != "mac":
            return None
        r = subprocess.run(
            ["osascript", "-e",
             "tell application \"System Events\" to return UI elements enabled"],
            capture_output=True, text=True)
        return r.stdout.strip() == "true" if r.returncode == 0 else False

    def lock(self) -> str:
        if self.os == "mac":
            self._osa('tell application "System Events" to keystroke "q" '
                      'using {control down, command down}')
        elif self.os == "win":
            self._sh(["rundll32.exe", "user32.dll,LockWorkStation"])
        else:
            self._sh(["loginctl", "lock-session"])
        return "Locking the screen"


# --------------------------------------------------------------- solo mode --

NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
           "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100,
           "a hundred": 100, "two hundred": 200, "five hundred": 500}

MODIFIERS = {"command": "command", "cmd": "command", "super": "command",
             "windows": "command", "control": "control", "ctrl": "control",
             "option": "option", "alt": "option", "shift": "shift"}

SHORTCUTS = {"copy": "c", "paste": "v", "cut": "x", "undo": "z", "save": "s",
             "select all": "a", "new tab": "t", "close tab": "w", "find": "f",
             "new window": "n", "refresh": "r", "reload": "r"}


def _amount(raw: str, default: int) -> int:
    """Pull a count out of a noisy phrase.

    Transcription drops filler in ("by about 200", "two hundred pixels") and
    sometimes inserts a word outright, so this digs for the number rather than
    demanding the whole group be one.
    """
    if not raw:
        return default
    raw = raw.strip().lower()
    digits = re.findall(r"\d+", raw)
    if digits:
        return int(digits[-1])
    words = raw.split()
    for size in range(len(words), 0, -1):          # "two hundred" beats "two"
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i:i + size])
            if phrase in NUMBERS:
                return NUMBERS[phrase]
    return default


def _parse_keys(spec: str) -> tuple:
    """'command shift z' -> ('z', ('command', 'shift'))"""
    mods, key = [], []
    for word in spec.replace("+", " ").split():
        if word in MODIFIERS:
            mods.append(MODIFIERS[word])
        else:
            key.append(word)
    return " ".join(key), tuple(mods)


def command_vocabulary(wake: str = "") -> str:
    """A decoder hint listing the words solo mode actually expects.

    Only appropriate for a closed command set - in agent mode you dictate
    arbitrary prose, and biasing toward this list would corrupt it.
    """
    verbs = ("open, launch, quit, switch to, click, double click, right click, "
             "scroll up, scroll down, move the cursor, left, right, up, down, "
             "center, type, press, command, shift, control, option, copy, "
             "paste, undo, select all, close tab, screenshot, clipboard, "
             "brightness, volume, mute, minimize, maximize, full screen, play, "
             "next track, drag, downloads, documents, lock the screen, "
             "start dictating, wake up, go to sleep")
    apps = ", ".join(sorted({v for v in APP_ALIASES.values()})[:12])
    head = f"{wake.capitalize()}. " if wake else ""
    return f"{head}Commands: {verbs}. Apps: {apps}."


class SoloSession:
    """voicepilot with no agent attached: plain voice control of the machine."""

    HELP = ("Say things like: open Safari. switch to Chrome. open downloads. "
            "go to github dot com. move the cursor left 200. click. scroll "
            "down. type hello there. press command s. copy. read the "
            "clipboard. minimize. full screen. play. next track. volume up. "
            "brightness down. take a screenshot. start dictating. lock the "
            "screen. go to sleep. or, quit voicepilot.")

    def __init__(self, args):
        self.a = args
        self.speaker = Speaker(args.tts, args.voice, args.rate,
                               enabled=not args.no_speak)
        self.recorder = Recorder(threshold=args.mic_threshold,
                                 device=args.mic_device)
        self.desk = Desktop()
        # None means "not specified" -> use the assistant's name;
        # "" means --no-wake was passed on purpose, so leave it off
        wake = args.wake if args.wake is not None else args.name
        self.wake = re.sub(r"[^a-z ]", "", wake.lower()).strip()
        self.stt = Transcriber(
            args.stt, args.stt_model, args.language, beam=args.beam,
            bias=None if args.no_bias else command_vocabulary(self.wake))
        self.awake = True
        self.running = True
        self.dictating = False
        self.pending = None        # (description, action) awaiting a spoken yes
        self.rules = self._build_rules()

    # -- vocabulary -------------------------------------------------------

    def _build_rules(self) -> list:
        d = self.desk
        self.mod = ("command",) if d.os == "mac" else ("control",)

        # order matters: the specific phrases must win over the greedy ones
        return [
            (r"^(?:go to sleep|sleep|stop listening|pause|be quiet)$", self._sleep),
            (r"^(?:wake up|start listening|resume|listen up)$", self._wake_up),
            (r"^(?:quit|exit|stop|close) voicepilot$|^goodbye$|^shut down voicepilot$",
             self._quit),
            (r"^(?:help|what can you do|what commands)", lambda m: self.HELP),

            (r"^(?:take (?:a |the )?)?screenshot$|^capture (?:the )?screen$",
             lambda m: d.screenshot()),
            (r"^lock (?:the )?(?:screen|computer|mac|pc)$", lambda m: d.lock()),
            (r"^(?:volume|sound) (up|down)$|^turn (?:the )?(?:volume|sound) (up|down)$",
             lambda m: d.volume(m.group(1) or m.group(2))),
            (r"^(mute|unmute)$", lambda m: d.volume(m.group(1))),

            (r"^(copy|paste|cut|undo|save|select all|new tab|close tab|find|"
             r"new window|refresh|reload)$",
             lambda m: self._shortcut(m.group(1))),
            (r"^redo$", lambda m: self._shortcut("undo", shift=True, say="Redo")),

            (r"^(?:move )?(?:the )?(?:mouse|cursor|pointer) to (\d+)[ ,and]+(\d+)$",
             lambda m: d.move_to(int(m.group(1)), int(m.group(2)))),
            (r"^(?:move )?(?:the )?(?:mouse|cursor|pointer) (left|right|up|down)"
             r"(?: by)?(?: ([\w ]+))?$",
             lambda m: d.move_by(m.group(1), _amount(m.group(2), 100))),
            (r"^(?:center|centre)(?: the)? (?:mouse|cursor|pointer|screen)$",
             lambda m: d.center()),
            (r"^where(?:'s| is)(?: the)? (?:mouse|cursor|pointer)$",
             lambda m: "The cursor is at %d, %d" % d.cursor_pos()),

            (r"^double(?: |-)?click$", lambda m: d.click("left", 2)),
            (r"^right(?: |-)?click$", lambda m: d.click("right")),
            (r"^(?:left )?click(?: (?:the )?mouse)?$", lambda m: d.click("left")),

            (r"^scroll (up|down)(?: by)?(?: ([\w ]+))?$",
             lambda m: d.scroll(m.group(1), _amount(m.group(2), 5))),

            (r"^type (.+)$", lambda m: d.type_text(m.group(1))),
            (r"^(?:press|hit|tap) (.+)$",
             lambda m: d.press(*_parse_keys(m.group(1)))),

            (r"^(?:switch to|focus|go to app|bring up) (.+)$",
             lambda m: d.focus_app(m.group(1))),
            (r"^minimi[sz]e(?: the)?(?: window)?$", lambda m: d.window("minimize")),
            (r"^maximi[sz]e(?: the)?(?: window)?$", lambda m: d.window("maximize")),
            (r"^(?:go )?full ?screen$", lambda m: d.window("fullscreen")),
            (r"^close(?: the)? window$", lambda m: d.window("close")),
            (r"^(?:switch|next)(?: the)? window$|^switch apps?$",
             lambda m: d.window("switch")),

            (r"^(?:play|pause|play pause|resume music)$", lambda m: d.media("play")),
            (r"^(?:next|skip)(?: the)? (?:track|song)$", lambda m: d.media("next")),
            (r"^(?:previous|last|go back a)(?: the)? (?:track|song)$",
             lambda m: d.media("previous")),
            (r"^brightness (up|down)$|^(?:turn )?(?:the )?brightness (up|down)$",
             lambda m: d.brightness(m.group(1) or m.group(2))),
            (r"^(?:turn off|sleep)(?: the)? (?:display|screen|monitor)$",
             lambda m: d.sleep_display()),

            (r"^(?:read|what(?:'s| is) (?:on|in))(?: the)? clipboard$",
             lambda m: self._read_clipboard()),
            (r"^(?:open|show)(?: the)? (downloads|documents|desktop|home|"
             r"pictures|music|movies|applications|trash)(?: folder)?$",
             lambda m: d.open_path(m.group(1))),
            (r"^(?:go to|open|visit) ([\w-]+(?: dot |\.)[\w.]+(?:/\S*)?)$",
             lambda m: d.open_url(m.group(1))),

            (r"^drag(?: the mouse)?(?: to)? (\d+)[ ,and]+(\d+)$",
             lambda m: d.drag_to(int(m.group(1)), int(m.group(2)))),

            (r"^(?:start|begin) (?:dictating|dictation|typing)$|^dictation mode$",
             self._start_dictation),
            (r"^(?:shut ?down|power off)(?: the)?(?: computer|machine|mac|pc)?$",
             lambda m: self._confirm("Shut down the computer",
                                     lambda: d.power("shutdown"))),
            (r"^(?:restart|reboot)(?: the)?(?: computer|machine|mac|pc)?$",
             lambda m: self._confirm("Restart the computer",
                                     lambda: d.power("restart"))),
            (r"^log ?out$|^sign out$",
             lambda m: self._confirm("Log out", lambda: d.power("logout"))),

            (r"^(?:google|search for|search|look up) (.+)$",
             lambda m: d.web_search(m.group(1))),
            (r"^(?:open|launch|start|run) (?:the )?(?:app )?(.+)$",
             lambda m: d.open_app(m.group(1))),
            (r"^(?:quit|close|kill) (?:the )?(?:app )?(.+)$",
             lambda m: d.quit_app(m.group(1))),

            (r"^what(?:'s| is)? the time$|^what time is it$",
             lambda m: "It is " + time.strftime("%-I:%M %p")),
        ]

    def _read_clipboard(self) -> str:
        text = self.desk.clipboard_get()
        if not text:
            return "The clipboard is empty."
        head, rest = speakable(text, self.a.reply_chars)
        return f"The clipboard says: {head}" + (" And there is more." if rest else "")

    def _start_dictation(self, m) -> str:
        self.dictating = True
        return "Dictation on. Everything you say gets typed. Say stop dictating to finish."

    DICTATION_OFF = ("stop dictating", "stop dictation", "end dictation",
                     "stop typing", "that's all", "thats all")

    def _dictate(self, heard: str) -> None:
        norm = re.sub(r"[^a-z' ]", "", heard.lower()).strip()
        if norm in self.DICTATION_OFF:
            self.dictating = False
            status(f"{YEL}dictation off{RST}")
            self.speaker.say("Dictation off.")
            return
        try:
            if norm in ("new line", "newline"):
                self.desk.press("return")
            elif norm == "new paragraph":
                self.desk.press("return")
                self.desk.press("return")
            else:
                status(f'{DIM}typing:{RST} "{heard}"')
                self.desk.type_text(heard)
        except DesktopError as e:
            status(f"{RED}{e}{RST}")
            self.dictating = False
            self.speaker.say("I can't type. Dictation off.")

    def _resolve_pending(self, command: str) -> None:
        description, action = self.pending
        self.pending = None
        if re.match(r"^(yes|yeah|yep|confirm|do it|go ahead|affirmative)$", command):
            try:
                reply = action()
            except DesktopError as e:
                reply = f"That failed. {e}"
        else:
            reply = f"Cancelled. Not doing it."
        status(f"{GRN}{reply}{RST}")
        self.speaker.say(reply)

    def _confirm(self, description: str, action) -> str:
        """Park a destructive action until it is confirmed out loud."""
        self.pending = (description, action)
        return f"{description}? Say yes to confirm."

    def _shortcut(self, name: str, shift: bool = False,
                  say: str | None = None) -> str:
        mods = self.mod + (("shift",) if shift else ())
        self.desk.press(SHORTCUTS[name], mods)
        return say or name.capitalize()

    # -- stateful commands ------------------------------------------------

    def _sleep(self, m):
        self.awake = False
        return f"Sleeping. Say {self.wake} wake up." if self.wake else "Sleeping."

    def _wake_up(self, m):
        self.awake = True
        return "Listening."

    def _quit(self, m):
        self.running = False
        return personalize("Goodbye, {user}.", self.a.name, self.a.user)

    # -- loop -------------------------------------------------------------

    def _addressed(self, text: str) -> str | None:
        """Strip the wake word. None means this speech wasn't for us."""
        norm = re.sub(r"[^a-z0-9' ]", " ", text.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        if not self.wake:
            return norm
        for prefix in (f"hey {self.wake}", f"ok {self.wake}",
                       f"okay {self.wake}", self.wake):
            if norm.startswith(prefix):
                return norm[len(prefix):].strip(" ,.")
        return None

    def _execute(self, command: str) -> None:
        if not command:
            return
        for pattern, handler in self.rules:
            m = re.match(pattern, command)
            if not m:
                continue
            TRACE("match", f"{command!r} -> /{pattern[:48]}/")
            try:
                reply = handler(m)
            except DesktopError as e:
                msg = str(e)
                if re.search(r"assistive|accessibility|1719|not allowed", msg, re.I):
                    reply = ("I need accessibility permission. Enable your "
                             "terminal under Privacy and Security, Accessibility.")
                    status(f"{RED}{msg}{RST}")
                else:
                    reply = f"That failed. {msg}"
                    status(f"{RED}{msg}{RST}")
            except Exception as e:
                reply = "That didn't work."
                status(f"{RED}{e}{RST}")
            status(f"{GRN}{reply}{RST}")
            self.speaker.say(reply)
            return
        TRACE("match", f"no rule matches {command!r} "
                       f"({len(self.rules)} tried)")
        status(f'{YEL}no command matches{RST} "{command}"')
        self.speaker.say("I didn't understand that. Say help for a list.")

    def run(self) -> int:
        if not self.recorder.available():
            print("Solo mode needs a microphone: pip install sounddevice numpy",
                  file=sys.stderr)
            return 2
        if not self.stt.available:
            print("Solo mode needs speech-to-text: pip install faster-whisper",
                  file=sys.stderr)
            return 2

        print(f"{DIM}loading speech model ({self.stt.backend})...{RST}")
        self.stt.warm()
        hello = personalize(self.a.greeting, self.a.name, self.a.user)
        if self.wake:
            hello += f" Say {self.wake}, then a command."
        print(f"{GRN}[solo]{RST} {hello}  {DIM}Ctrl-C to quit{RST}")
        self.speaker.say(hello)

        try:
            while self.running:
                wav = self.recorder.record(max_wait=self.a.solo_wait,
                                           silence=self.a.silence,
                                           max_len=self.a.max_utterance)
                if not wav:
                    continue
                try:
                    heard = self.stt.transcribe(wav)
                finally:
                    os.unlink(wav)
                if not heard:
                    continue

                # dictation takes the raw transcript, punctuation and all,
                # and needs no wake word or it would be unusable
                if self.dictating:
                    self._dictate(heard)
                    continue

                command = self._addressed(heard)
                if command is None:
                    status(f'{DIM}not addressed to me: "{heard}"{RST}')
                    continue
                status(f'{YEL}heard:{RST} "{heard}"')
                if self.a.log:
                    with open(self.a.log, "a") as f:
                        f.write(f"{time.strftime('%F %T')}\tsolo\t{heard}\n")

                if self.pending:
                    self._resolve_pending(command)
                    continue

                if not self.awake:
                    # asleep: nothing but the wake command gets through
                    if re.match(r"^(wake up|start listening|resume|listen up)$",
                                command):
                        self._execute(command)
                    continue
                self._execute(command)
        except KeyboardInterrupt:
            pass
        print(f"\n{DIM}[solo] voice control stopped{RST}")
        return 0


# --------------------------------------------------------------- self check --

def run_compare(args) -> int:
    """Record one phrase and show what every installed engine makes of it.

    Synthetic audio can't rank these - every engine transcribes clean
    text-to-speech near perfectly. Only your own voice, microphone and room
    tell you which one is actually better for you.
    """
    if not Recorder.available():
        print("Needs a microphone: pip install sounddevice numpy", file=sys.stderr)
        return 2
    backends = Transcriber.available_backends()
    if not backends:
        print("No speech engines installed: pip install faster-whisper",
              file=sys.stderr)
        return 2

    trials = []
    for b in backends:
        if b == "faster-whisper":
            trials += [(f"faster-whisper {m}", b, m)
                       for m in ("base.en", "small.en", "medium.en")]
        else:
            trials.append((b, b, None))

    print("Engines to try: " + ", ".join(label for label, _, _ in trials))
    print(f"{DIM}Models download on first use (medium.en is ~770MB), so the "
          f"first run of this can be slow.{RST}\n")
    rec = Recorder(threshold=args.mic_threshold, device=args.mic_device)
    print("Say a command that usually gets misheard - speak, then pause.")
    wav = rec.record(max_wait=20.0, silence=args.silence,
                     max_len=args.max_utterance)
    if not wav:
        print("Heard nothing. Try --mic-threshold 400.")
        return 1

    bias = None if args.no_bias else command_vocabulary(args.wake or "")
    print(f"\n{'engine':30} {'time':>6}  transcript")
    print("-" * 76)
    for label, backend, model in trials:
        try:
            t = Transcriber(backend, model, args.language, beam=args.beam,
                            bias=bias)
            t.warm()
            t0 = time.time()
            text = t.transcribe(wav)
            print(f"{label:30} {time.time() - t0:5.1f}s  {text!r}")
        except Exception as e:
            print(f"{label:30}    --   failed: {type(e).__name__}: {e}")
    os.unlink(wav)
    print("\nPick whichever got it right and save it, for example:")
    print("  voicepilot --stt parakeet-mlx --save")
    print("  voicepilot --stt faster-whisper --stt-model medium.en --save")
    return 0


def run_check(args) -> int:
    ok = True
    print("voicepilot setup check\n" + "-" * 22)
    who = args.user or "(not set - use --user YOURNAME --save)"
    print(f"assistant name : {args.name}   wake word: {args.wake or '(none)'}")
    print(f"your name      : {who}")
    print(f"config         : {CONFIG_PATH}"
          f"{'' if os.path.exists(CONFIG_PATH) else '  (not written yet)'}")
    print()

    sp = Speaker(args.tts, args.voice, args.rate, enabled=not args.no_speak)
    print(f"text-to-speech : {sp.backend}")
    if sp.available:
        sp.say("Voice pilot check. Speak after the prompt.")
    elif not args.no_speak:
        ok = False
        print("   -> install espeak-ng, or: pip install pyttsx3")

    mic = Recorder.available()
    mic_ready = False
    print(f"microphone     : {'sounddevice ok' if mic else 'MISSING'}")
    if not mic:
        ok = False
        print("   -> pip install sounddevice numpy")
    else:
        # macOS hands out all-zero samples instead of an error when the
        # terminal lacks mic permission, so measure before blaming the user
        try:
            import numpy as np
            import sounddevice as sd
            recorder = Recorder(device=args.mic_device)
            rate = recorder.input_samplerate()
            device_label = 'default' if recorder.device is None else recorder.device
            print(f"   -> input device {device_label}, {rate} Hz", flush=True)
            d = sd.rec(rate, samplerate=rate, channels=1,
                       dtype="int16", device=recorder.device)
            deadline = time.monotonic() + 5.0
            try:
                while sd.get_stream().active:
                    if time.monotonic() > deadline:
                        raise RuntimeError("microphone delivered no complete recording within 5 seconds")
                    time.sleep(0.05)
            finally:
                sd.stop()
            rms = float(np.sqrt(np.mean(d.astype(np.float32) ** 2)))
            if rms < 1.0:
                ok = False
                print(f"   -> capturing digital silence (rms {rms:.1f}): "
                      "check mute, input device, and microphone permissions")
                if sys.platform == "darwin":
                    print("      System Settings > Privacy & Security > "
                          "Microphone > enable your terminal app, then restart it")
            else:
                mic_ready = True
                print(f"   -> room tone rms {rms:.0f}, speech must exceed "
                      f"{max(Recorder.MIN_THRESHOLD, rms * 3.5):.0f}")
        except Exception as e:
            ok = False
            print(f"   -> could not open the mic: {e}")
        if not mic_ready:
            print("   -> list inputs with: python -m sounddevice; select one with --mic-device INDEX")
            if sys.platform.startswith("win"):
                print("   -> Windows Settings > Privacy & security > Microphone > allow desktop apps")
            elif os.environ.get("WSL_DISTRO_NAME"):
                print("   -> WSL audio needs WSLg/PulseAudio and Windows microphone permission.")

    st = Transcriber(args.stt, args.stt_model, args.language, beam=args.beam)
    installed = Transcriber.available_backends()
    print(f"speech-to-text : {st.backend}"
          f"{' ' + (args.stt_model or '') if args.stt_model else ''}"
          f"   (installed: {', '.join(installed) or 'none'})")
    if len(installed) > 1:
        print("   -> run 'voicepilot --compare' to test them on your own voice")
    if not st.available and args.stt != "none":
        ok = False
        print("   -> pip install faster-whisper      (local, recommended)")
        print("   -> or: pip install openai + export OPENAI_API_KEY=...")
        print("   -> or: pip install SpeechRecognition  (free, needs internet)")

    d = Desktop()
    print(f"desktop control: {d.os}")
    try:
        x0, y0, x1, y1 = d.screen_bounds()
        print(f"   -> displays span {int(x1 - x0)}x{int(y1 - y0)} "
              f"from ({int(x0)}, {int(y0)}); cursor at {d.cursor_pos()}")
    except Exception as e:
        ok = False
        print(f"   -> cannot read the display: {e}")
    acc = d.accessibility_ok()
    if acc is False:
        print("   -> no Accessibility permission: cursor moves and opening "
              "apps work,\n      but clicks, typing and key presses will not.")
        print("      System Settings > Privacy & Security > Accessibility > "
              "enable your terminal")
    elif acc:
        print("   -> accessibility granted (clicks and typing will work)")

    if mic_ready and st.available:
        print("\nloading model...")
        st.warm()
        print("say something now (recording stops on silence)...")
        rec = Recorder(threshold=args.mic_threshold, device=args.mic_device)
        wav = rec.record(max_wait=8.0, silence=args.silence, max_len=15.0)
        if not wav:
            ok = False
            print("   -> heard nothing. Speak louder/closer, or set a lower "
                  "--mic-threshold")
        else:
            try:
                text = st.transcribe(wav)
                print(f'   -> transcript: "{text}"')
                if not text:
                    ok = False
            finally:
                os.unlink(wav)

    next_command = "voicepilot solo" if not HAVE_PTY else "voicepilot claude"
    print("\n" + (f"ALL GOOD - run:  {next_command}" if ok
                  else "fix the items above, then re-run --check"))
    return 0 if ok else 1


# -------------------------------------------------------------------- entry --

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="voicepilot",
        description="Talk to any terminal AI agent by voice. "
                    "It tells you when the task is done and takes your next "
                    "prompt out loud.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  voicepilot.py claude              wrap an agent\n"
               "  voicepilot.py --idle 4 codex --model gpt-5\n"
               "  voicepilot.py solo                voice-control the machine\n"
               "  voicepilot.py solo --no-wake\n"
               "  voicepilot.py --check             verify mic / TTS / STT\n",
    )
    p.add_argument("--version", action="version",
                   version=f"voicepilot {__version__}")
    p.add_argument("--check", action="store_true",
                   help="verify mic / TTS / STT and exit")
    p.add_argument("--trace", action="store_true",
                   help="print the decisions it makes as it makes them "
                        "(to stderr, so: --trace claude 2> trace.log)")
    p.add_argument("--compare", action="store_true",
                   help="record one phrase and transcribe it with every "
                        "installed engine, so you can pick the best for your "
                        "voice and microphone")

    g = p.add_argument_group("identity")
    g.add_argument("--name", default="Pilot", metavar="NAME",
                   help="what to call the assistant; doubles as the solo-mode "
                        "wake word (default: Pilot)")
    g.add_argument("--user", default="", metavar="NAME",
                   help="what to call you, e.g. --user Sarbesh")
    g.add_argument("--greeting",
                   default="Good {daypart}, {user}. {name} ready.",
                   help="spoken on startup in solo mode; {name} {user} "
                        "{daypart} are filled in")
    g.add_argument("--save", action="store_true",
                   help=f"write these settings to {CONFIG_PATH} and reuse them")

    g = p.add_argument_group("solo mode (no agent - voice control the machine)")
    g.add_argument("--solo", action="store_true",
                   help="run standalone: open apps, move the cursor, type, "
                        "press keys, volume, screenshots. No AI model involved.")
    g.add_argument("--wake", default=None, metavar="WORD",
                   help="say this before a command so passing speech is "
                        "ignored (defaults to --name)")
    g.add_argument("--no-wake", dest="wake", action="store_const", const="",
                   help="act on everything heard, with no wake word")
    g.add_argument("--solo-wait", type=float, default=300.0, metavar="SEC",
                   help="how long one listening window stays open (300)")

    g = p.add_argument_group("detection")
    g.add_argument("--idle", type=float, default=2.5, metavar="SEC",
                   help="seconds of silent output that mean 'task done' (2.5)")
    g.add_argument("--prompt-idle", type=float, default=0.8, metavar="SEC",
                   help="idle needed when the agent's input prompt is visible, "
                        "which proves it is waiting (0.8)")
    g.add_argument("--busy-idle", type=float, default=15.0, metavar="SEC",
                   help="idle needed when the screen still shows a working/"
                        "interrupt hint, i.e. a silent long tool call (15)")
    g.add_argument("--startup-grace", type=float, default=1.5, metavar="SEC",
                   help="ignore idle for this long after launch (1.5)")
    g.add_argument("--manual", action="store_true",
                   help="never announce automatically; only Ctrl-] talks")

    g = p.add_argument_group("listening")
    g.add_argument("--listen-wait", type=float, default=8.0, metavar="SEC",
                   help="how long to wait for you to start speaking (8)")
    g.add_argument("--silence", type=float, default=1.2, metavar="SEC",
                   help="trailing silence that ends your utterance (1.2)")
    g.add_argument("--max-utterance", type=float, default=90.0, metavar="SEC",
                   help="hard cap on one spoken prompt (90)")
    g.add_argument("--retries", type=int, default=1,
                   help="re-listen attempts when nothing is heard (1)")
    g.add_argument("--confirm", action="store_true",
                   help="read the transcript back and require a spoken yes")
    g.add_argument("--mic-threshold", type=float, default=None, metavar="RMS",
                   help="fixed mic sensitivity instead of auto-calibration")
    g.add_argument("--mic-device", default=None,
                   help="sounddevice input device name or index")

    g = p.add_argument_group("voices")
    g.add_argument("--tts", default="auto",
                   choices=["auto", "say", "espeak-ng", "espeak", "spd-say",
                            "powershell", "pyttsx3", "none"])
    g.add_argument("--voice", default=None, help="TTS voice name")
    g.add_argument("--rate", type=int, default=None, help="TTS words per minute")
    g.add_argument("--no-speak", action="store_true",
                   help="show prompts on screen but stay silent")
    g.add_argument("--stt", default="auto",
                   choices=["auto", "faster-whisper", "parakeet-mlx",
                            "mlx-whisper", "whisper", "openai", "google", "none"])
    g.add_argument("--stt-model", default=None,
                   help="model for the chosen engine, e.g. small.en / "
                        "medium.en / large-v3 (bigger is more accurate, slower)")
    g.add_argument("--beam", type=int, default=5, metavar="N",
                   help="whisper beam width; 1 is fastest, 5 is more accurate (5)")
    g.add_argument("--no-bias", action="store_true",
                   help="don't hint the decoder with the solo command vocabulary")
    g.add_argument("--language", default="en", help="spoken language (en)")
    g.add_argument("--no-warm", dest="warm", action="store_false",
                   help="don't preload the speech model at startup")

    g = p.add_argument_group("wording / sending")
    g.add_argument("--announce", default="Task complete. What's next?",
                   help="fallback line when no reply text could be read")
    g.add_argument("--followup", default="What's next?",
                   help="said after reading a reply that isn't a question")
    g.add_argument("--reply-chars", type=int, default=420, metavar="N",
                   help="how much of the reply to read before pausing (420); "
                        "say 'read more' for the rest")
    g.add_argument("--no-read-reply", dest="read_reply", action="store_false",
                   help="don't read the agent's answer, just announce")
    g.add_argument("--ready-announce",
                   default="Good {daypart}, {user}. {name} ready. "
                           "What should I ask?")
    g.add_argument("--approve-announce",
                   default="The agent is waiting for your approval.")
    g.add_argument("--listen-prompt", default="Listening.")
    g.add_argument("--submit-key", default="\r",
                   help="key sent after the text (default carriage return)")
    g.add_argument("--submit-delay", type=float, default=0.2, metavar="SEC",
                   help="pause between typing and pressing enter (0.2)")
    g.add_argument("--log", default=None, metavar="FILE",
                   help="append every spoken prompt to FILE")

    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="the agent to run, e.g. claude / codex / kimi; "
                        "or the word 'solo' for standalone voice control")
    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()

    cfg = load_config()
    args._config_keys = set()
    for key, flags in CONFIG_KEYS.items():
        if key in cfg and not _flag_given(flags):
            setattr(args, key, cfg[key])
            args._config_keys.add(key)
    # renaming the assistant renames what you call it by, otherwise a saved
    # wake word would silently outlive the name it came from
    if _flag_given(CONFIG_KEYS["name"]) and not _flag_given(CONFIG_KEYS["wake"]):
        args.wake = args.name.lower()
    if args.wake is None:
        args.wake = args.name.lower()
    if args.save:
        print(f"saved settings to {save_config(args)}")
        if not args.command and not args.solo and not args.check:
            return 0          # "just remember this" is a complete request

    enable_windows_ansi()

    global TRACE
    TRACE = Trace(args.trace)

    if args.compare:
        return run_compare(args)
    if args.check:
        return run_check(args)

    argv = args.command
    if argv and argv[0] == "--":
        argv = argv[1:]

    if args.solo or argv[:1] == ["solo"]:
        return SoloSession(args).run()

    if not argv:
        p.print_help()
        return 2
    if not HAVE_PTY and os.name == "nt":
        try:
            import winpty
        except ImportError:
            print("Windows agent mode needs pywinpty. Re-run install.ps1.", file=sys.stderr)
            return 2
    elif not HAVE_PTY:
        print("Wrapping an agent needs a Unix pty - run this under WSL.\n"
              "Solo voice control does work here: voicepilot --solo",
              file=sys.stderr)
        return 2

    return Session(argv, args).run()


if __name__ == "__main__":
    sys.exit(main())
