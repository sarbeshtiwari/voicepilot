# voicepilot

Talk to any terminal AI agent by voice — or drop the agent entirely and just
talk to your computer.

```
you ──speak──► voicepilot ──types──► claude / codex / aider / ollama / kimi ...
                    ▲                              │
                    └──── reads you the reply ◄────output goes quiet
```

It wraps the agent in a pty, so the agent behaves exactly as normal and you can
still type whenever you want. voicepilot only watches the output stream.

Everything runs locally. No API keys, and after the first run no internet —
your voice never leaves the machine.

## Quick start

```bash
voicepilot claude          # wrap an agent and talk to it
voicepilot solo            # no agent: voice-control the machine
voicepilot --check         # verify mic / speech / permissions
```

## Name it, and let it know you

```bash
voicepilot --name Nova --user Sarbesh --save
```

`--save` writes to `~/.config/voicepilot/config.json`, so later runs need no
flags. An explicit flag always beats the saved value.

> *"Good evening, Sarbesh. Nova ready. What should I ask?"*

The greeting is time-aware. The assistant's name is also the solo-mode wake
word, so you say **"Nova, open Safari"**. Renaming it renames the wake word
too, unless you set one explicitly with `--wake`.

Any spoken line can be reworded, with `{name}`, `{user}` and `{daypart}`
filled in:

```bash
voicepilot --greeting "Morning boss, {name} standing by." --save
voicepilot --announce "All done, {user}." --save
```

Put a placeholder at the end of a clause — *"ready, {user}."* — so an unset
name leaves a comma to clean up rather than a gap.

---

# Agent mode

## The loop

1. The agent's output goes quiet → it stopped thinking.
2. voicepilot **reads you its actual reply**, stripped of TUI chrome.
3. It records you, stopping automatically when you pause.
4. It transcribes locally and types your prompt in, then presses enter.
5. Back to 1.

It's a conversation, not a notification. If the agent says *"Done. Which
environment should I deploy to?"* you hear exactly that and just answer —
voicepilot notices the reply ends in a question and doesn't talk over it with
"what's next?". Only a non-question reply gets the `--followup` line appended.

Long replies stop at `--reply-chars` (420) on a sentence boundary. Say
**"read more"** for the rest, or **"repeat"** to hear it again.

## Keys

| Key | Does |
|---|---|
| `Ctrl-]` | talk right now (push-to-talk), or cancel a listen in progress |
| `Ctrl-\` | toggle automatic voice mode on/off |
| any key | cancels a listen, so you can just type instead |

## Spoken commands

| You say | It does |
|---|---|
| "repeat" / "what did it say" | reads the last reply again |
| "read more" / "continue" | reads the rest of a long reply |
| "cancel" / "never mind" | sends nothing |
| "stop listening" / "voice off" | turns auto voice off (`Ctrl-\` re-enables) |
| "interrupt" / "escape" | sends ESC to the agent |
| "yes" / "one" / "two" | picks an option at a permission prompt |
| "no" / "deny" | sends ESC to reject it |
| "literally cancel" | sends the word instead of running the command |

When the agent is blocked on a permission prompt, voicepilot notices and says
*"it's waiting for your approval"* instead of "task complete".

## Working with any agent

Agents differ, so voicepilot uses three signals rather than a fixed timeout:

| Situation | Wait before speaking |
|---|---|
| the agent's **input prompt is on screen** — it is provably idle | `--prompt-idle` (0.8s) |
| output simply went quiet | `--idle` (2.5s) |
| the screen still shows "esc to interrupt" — maybe a long silent tool call | `--busy-idle` (15s) |

Prompt detection covers the shapes real CLIs draw — a boxed `│ >` input,
`>>>`, a bare `>` or `$`, `(main) >`, `? for shortcuts` — which is what makes
this work beyond Claude Code, including agents that repaint while idle.

Known agents are auto-tuned by command name (`claude`, `codex`, `aider`,
`ollama`, `gemini`, `kimi`, `kiro`, `opencode`, `crush`, `goose`,
`cursor-agent`, plus `python`/`node` REPLs). Anything else gets conservative
defaults and still works. Override per run or save:

```bash
voicepilot --idle 4 some-new-agent
voicepilot --idle 4 --save
```

## How the reply is extracted

An agent TUI repaints constantly, overwrites lines with `\r`, and draws boxes,
spinners and hint bars. voicepilot reassembles the output into lines (modelling
`\n`, `\r`, and the CRLF a pty emits for every newline), marks where your
prompt went in, then drops what is furniture rather than speech: box rules, the
input box interior, `? for shortcuts`, mode indicators, spinner and status
lines, token/cost footers, tool-result gutters, and the pty's echo of your own
prompt. Lines repeated by repaints are collapsed.

What is left is flattened for speech — code fences become "code block", URLs
become "a link", markdown punctuation is dropped — then truncated at a sentence
boundary.

If your agent's chrome leaks into the speech, `--no-read-reply` falls back to a
plain completion announcement.

---

# Solo mode

No agent, no wrapping — plain voice control of the machine.

```bash
voicepilot solo              # wake word is the assistant's name
voicepilot solo --no-wake    # act on everything it hears
```

It listens continuously but only acts on speech starting with the wake word, so
ordinary conversation in the room is ignored.

## Commands

**Apps and windows**

| Say | Does |
|---|---|
| "Nova, open Safari" | launches an app (knows nicknames: chrome, vs code, settings) |
| "Nova, switch to Chrome" | brings an app to the front |
| "Nova, quit Spotify" | closes an app |
| "Nova, minimize" / "maximize" / "go full screen" | window state |
| "Nova, close window" / "switch window" | window control |

**Pointer and keyboard**

| Say | Does |
|---|---|
| "Nova, move the cursor left 200" | also up/down/right, "to 500 600", "center the cursor" |
| "Nova, click" / "double click" / "right click" | clicks |
| "Nova, drag to 800 400" | press, move, release |
| "Nova, scroll down" / "scroll up ten" | scrolls |
| "Nova, type hello there" | types into whatever is focused |
| "Nova, press command s" | any key with modifiers; also "press escape", "press tab" |
| "Nova, copy" / "paste" / "undo" / "select all" / "close tab" | the usual shortcuts, right modifier per OS |

**Files, web, clipboard**

| Say | Does |
|---|---|
| "Nova, open downloads" | also documents, desktop, home, pictures, music, applications, trash |
| "Nova, go to github dot com" | opens a URL |
| "Nova, search for pasta recipes" | browser search |
| "Nova, read the clipboard" | speaks the clipboard contents |

**System**

| Say | Does |
|---|---|
| "Nova, volume up" / "mute" | audio |
| "Nova, play" / "next track" / "previous track" | media transport |
| "Nova, brightness up" / "down" | display brightness |
| "Nova, take a screenshot" | saves to the desktop |
| "Nova, turn off the display" / "lock the screen" | display and session |
| "Nova, shut down" / "restart" / "log out" | **asks first** — see below |

**Conversation**

| Say | Does |
|---|---|
| "Nova, start dictating" | see below |
| "Nova, where is the cursor" / "what time is it" | spoken answers |
| "Nova, go to sleep" | stops acting until "Nova, wake up" |
| "Nova, help" | reads the command list back |
| "Nova, quit voicepilot" | exits |

## Dictation

```
"Nova, start dictating"
```

Everything you then say is typed into the focused app — verbatim, with the
punctuation Whisper infers. No wake word while dictating, or it would be
unusable. Say **"new line"** or **"new paragraph"** for breaks, and
**"stop dictating"** to finish.

## Destructive actions ask first

Shut down, restart and log out are parked until confirmed:

> — *"Nova, shut down"*
> — *"Shut down the computer? Say yes to confirm."*
> — *"Nova, yes"*

Anything other than yes cancels it.

## Solo mode permissions

Moving the cursor and opening apps need no permission. **Clicks, typing and key
presses do**, and that is separate from the microphone:

- **macOS** — System Settings → Privacy & Security → **Accessibility** → enable
  your terminal app. `voicepilot --check` reports whether this is granted.
- **Linux** — needs `xdotool`; `wmctrl` for window focus, `playerctl` for
  media, `brightnessctl` for brightness, `xclip` for the clipboard.
- **Windows** — solo mode drives the desktop natively; agent mode uses ConPTY.

Solo mode adds no dependencies: CoreGraphics via `ctypes` plus `osascript` on
macOS, `user32` on Windows, `xdotool` on Linux.

Multiple monitors are handled — a display above or left of the main one sits at
negative coordinates, and the cursor reaches it.

Two platform caveats, stated honestly: macOS exposes no generic media key, so
"play"/"next track" drive Spotify or Music if one is running; and brightness
uses the F14/F15 key codes, which aren't wired on every Mac. Windows brightness
is not implemented and says so.

---

# If it mishears you

Start by testing engines **on your own voice** — this is the only measurement
that means anything, because every engine transcribes clean synthetic speech
near perfectly:

```bash
voicepilot --compare
```

It records one phrase and shows what each installed engine makes of it, with
timings. Save whichever wins:

```bash
voicepilot --stt parakeet-mlx --save
voicepilot --stt faster-whisper --stt-model medium.en --save
```

## Engines

| Engine | Install | Notes |
|---|---|---|
| **faster-whisper** (default) | `pip install faster-whisper` | Cross-platform. Model sizes `tiny.en` → `base.en` → `small.en` (default) → `medium.en` → `large-v3`. Bigger is more accurate and slower. |
| **parakeet-mlx** | `pip install parakeet-mlx` | Apple Silicon only. NVIDIA Parakeet TDT — excellent for English and about as fast as `small.en`. Try this first on a Mac. |
| **mlx-whisper** | `pip install mlx-whisper` | Apple Silicon only. Whisper `large-v3-turbo` — the most robust for accents and background noise, at some speed cost. |
| openai | `pip install openai` + `OPENAI_API_KEY` | Cloud. Not free. |
| google | `pip install SpeechRecognition` | Free but needs internet, and weaker than the above. |

## Other things that help

- **Move up the model ladder.** `--stt-model medium.en --save` is the single
  biggest lever for faster-whisper.
- **`--beam 5`** (the default) is more accurate than `--beam 1`. Drop to 1 only
  if you want speed.
- **Vocabulary biasing** is on in solo mode: the decoder is told which commands
  and app names to expect, which makes short commands far more reliable. It is
  deliberately **off in agent mode**, where you dictate free-form prose and
  biasing would corrupt it. Disable with `--no-bias`.
- **Mic level.** `--check` prints your room tone and the threshold speech has to
  clear. If your voice is quiet, `--mic-threshold 400`.
- **Cut-off words?** Raise `--silence 1.5` so it waits longer before deciding
  you finished.

---

# Tuning

| Flag | Meaning |
|---|---|
| `--idle 4` | seconds of quiet that mean "done" |
| `--prompt-idle 0.5` | quiet needed when the input prompt is visible |
| `--busy-idle 20` | patience when an interrupt hint is still on screen |
| `--reply-chars 250` | read less of each reply before pausing |
| `--followup "go ahead"` | said after a reply that isn't a question |
| `--no-read-reply` | don't read replies, just announce completion |
| `--manual` | never announce on its own; only `Ctrl-]` talks |
| `--confirm` | read the transcript back and require a spoken yes before sending |
| `--stt-model medium.en` | bigger, more accurate speech model |
| `--stt parakeet-mlx` | switch speech engine (see above) |
| `--beam 1` | faster, less accurate decoding |
| `--no-bias` | turn off solo-mode vocabulary hinting |
| `--voice Daniel --rate 190` | macOS voice and speed (`say -v '?'` lists them) |
| `--mic-threshold 600` | fixed mic sensitivity if auto-calibration misfires |
| `--silence 1.5` | how long a pause ends your utterance |
| `--no-speak` | show prompts on screen but stay silent |
| `--log ~/prompts.txt` | append every spoken prompt to a file |
| `--name` / `--user` | who it is, and who you are |
| `--wake WORD` / `--no-wake` | solo-mode trigger |
| `--save` | persist the current settings |

# Install on another machine

Copy **`voicepilot.py`, `windows_terminal.py`, `codex_screen.py`,
`requirements.txt`, `install.sh`, `install.ps1`, `README.md`** and run
the installer. **Do not copy `.venv`** — it hard-codes paths from the old
machine.

```bash
cd voicepilot
./install.sh
voicepilot --check
```

`install.sh` picks a Python that has wheels (3.12 first, since compiled deps
like `ctranslate2` lag behind new releases), builds `.venv` (`.venv-wsl` in WSL),
and writes a launcher into `~/.local/bin`. It uses
`uv` if present, otherwise `venv` + `pip`.

| OS | Notes |
|---|---|
| **macOS** | Works as-is. TTS is the built-in `say`. |
| **Linux** | Also `sudo apt install espeak-ng libportaudio2 xdotool`. |
| **Windows** | See below — both solo and agent modes run natively. |

### Windows

`install.sh` is for macOS, Linux and WSL. On native Windows use the PowerShell
installer, from the voicepilot folder:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

It builds `.venv-win` with the Windows layout (`Scripts\`, not `bin/`), installs a
`voicepilot.cmd` launcher under `%LOCALAPPDATA%\voicepilot`, and adds that to
your PATH — **open a new terminal** afterwards. Then:

```
voicepilot --check
voicepilot solo
voicepilot codex
```

Native Windows supports agent mode through **ConPTY**, installed via `pywinpty`.
Run `voicepilot codex` in PowerShell or Windows Terminal; WSL is not required.
Codex's terminal repaints are reconstructed before VoicePilot extracts its final
answer. To use a Linux-installed agent instead, install and run inside WSL:

```bash
cd /mnt/d/self-ai/voicepilot/voicepilot  # adjust to your checkout
bash install.sh
~/.local/bin/voicepilot --check
~/.local/bin/voicepilot claude
```

Windows and WSL now use separate environments, so installing one preserves the
other. Existing `.venv` directories are left intact. Shell scripts are checked
out with LF endings to avoid `pipefail` / `bash\r` errors in WSL.

WSL microphone access depends on the [WSLg audio bridge](https://github.com/microsoft/wslg).
If `--check` reports a recording timeout, check Windows microphone permissions
and WSLg audio; reinstalling Python dependencies will not repair a stalled bridge.
Native Windows solo mode uses the microphone directly.
List audio devices using the appropriate environment:

```powershell
.\.venv-win\Scripts\python.exe -m sounddevice
voicepilot --mic-device 1 --check  # replace 1 with your input device index
```

Inside WSL, use `.venv-wsl/bin/python -m sounddevice`. Numeric microphone IDs
are supported, and recording falls back to the device's native sample rate
when its driver cannot capture at 16 kHz.

### Record a Windows demo with Codex

The optional recording tools run a real Codex session in an isolated fixture.
Synthesized prompt WAVs replace the microphone; they pass through the normal
local speech recognizer and VoicePilot turn loop. The resulting MP4 is a replay
of recorded terminal output, with synthesized speech labeled and long waits
shortened. It is not a live microphone or desktop screen recording.

```powershell
.\.venv-win\Scripts\python.exe -m pip install -r tools/demo-requirements.txt
.\.venv-win\Scripts\python.exe tools/capture_demo.py --output artifacts/my-demo
.\.venv-win\Scripts\python.exe tools/render_demo.py --input artifacts/my-demo
```

Requires a signed-in Codex CLI and the Windows David/Zira speech voices. The
capture script preserves earlier recordings; choose a new output folder for
each take. Output includes the MP4, a preview PNG, original speech WAVs, the
timestamped terminal log, and the actual code edited by Codex.

# Setup notes

Dependencies live next to the script in `.venv`, `.venv-win`, or `.venv-wsl`.
The installed launcher selects the correct interpreter. Python 3.12 is preferred;
availability of compiled dependency wheels varies with Python and platform.

**Microphone permission:** macOS silently returns all-zero audio when the
terminal lacks mic access. `--check` detects exactly that.

The first run downloads the Whisper `base.en` model (~75 MB) to
`~/.cache/huggingface`. After that everything is offline.
