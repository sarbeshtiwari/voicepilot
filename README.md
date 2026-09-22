# voicepilot

Talk to any terminal AI agent by voice. It tells you out loud when the agent
has finished, listens for your next prompt, and types it in for you.

```
you ──speak──► voicepilot ──types──► claude / codex / kimi / kiro / aider ...
                    ▲                              │
                    └──── reads you the reply ◄────output goes quiet
```

It also runs **standalone** with no agent at all, as plain voice control of
your computer — see [Solo mode](#solo-mode---no-agent-at-all).

It wraps the agent in a pty, so the agent behaves exactly as normal — you can
still type at any moment. voicepilot only watches the output stream.

## Run it

```bash
~/Desktop/voicepilot/voicepilot.py claude
~/Desktop/voicepilot/voicepilot.py codex --model gpt-5
~/Desktop/voicepilot/voicepilot.py --check     # verify mic / TTS / STT
```

A `voicepilot` launcher is symlinked into `~/.local/bin`, so plain
`voicepilot claude` works from anywhere.

## Name it, and let it know you

```bash
voicepilot --name Nova --user Sarbesh --save
```

`--save` writes these to `~/.config/voicepilot/config.json`, so every later run
just picks them up — no flags needed. An explicit flag always beats the saved
value, so you can override for one run without changing the file.

Now it greets you properly:

> *"Good evening, Sarbesh. Nova ready. What should I ask?"*

The greeting is time-aware (morning/afternoon/evening). The assistant's name
also becomes the solo-mode wake word, so you say *"Nova, open Safari"*.

Any spoken line can be reworded, with `{name}`, `{user}` and `{daypart}`
filled in:

```bash
voicepilot --greeting "Morning boss, {name} standing by." --save
voicepilot --announce "All done, {user}." --save
```

Put a placeholder at the end of a clause — *"ready, {user}."* — so that if a
name isn't set the leftover comma gets cleaned up rather than leaving a gap.
Saved settings also cover the voice, speech model, language and idle timing:

```bash
voicepilot --voice Daniel --rate 190 --idle 3 --save
voicepilot --check        # shows the current identity and config path
```

## The loop

1. The agent's output goes quiet for `--idle` seconds → it stopped thinking.
2. voicepilot **reads you the agent's actual reply**, stripped of TUI chrome.
3. It records you, stopping automatically when you pause.
4. It transcribes locally and types the text into the agent, then presses enter.
5. Back to 1.

So it's a conversation, not a notification. If the agent says *"Done. I
refactored the login handler. Which environment should I deploy to?"* you hear
exactly that and can just answer — voicepilot notices the reply ends in a
question and doesn't talk over it with "what's next?". Only a reply that isn't
a question gets the `--followup` line appended.

If no readable reply can be found it falls back to `--announce`
("Task complete. What's next?"). The first turn of a session says *"Agent
ready. What should I ask?"* so you can start the whole thing by voice.

Long replies are cut at `--reply-chars` (420) on a sentence boundary — say
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
| "yes" / "one" / "two" | picks an option when the agent asks permission |
| "no" / "deny" | sends ESC to reject a permission prompt |
| "literally cancel" | sends the word instead of running the command |

When the agent is blocked on a permission prompt, voicepilot notices and says
*"The agent is waiting for your approval"* instead of "task complete".

## Tuning

The only setting that usually matters is how long the output must stay quiet
before voicepilot decides the task is done:

```bash
voicepilot --idle 4 claude        # slower, fewer false "done" calls
voicepilot --idle 1.5 claude      # snappier
```

Other useful flags:

| Flag | Meaning |
|---|---|
| `--busy-idle 15` | extra patience when the screen still shows "esc to interrupt" (a long silent tool call) |
| `--manual` | never announce on its own; only `Ctrl-]` talks |
| `--confirm` | read the transcript back and require a spoken "yes" before sending |
| `--reply-chars 250` | read less of each reply before pausing |
| `--followup "go ahead"` | what it says after a reply that isn't a question |
| `--no-read-reply` | don't read replies, just announce completion |
| `--stt-model small.en` | more accurate transcription, a bit slower than `base.en` |
| `--voice Daniel --rate 200` | pick a macOS voice and speed (`say -v '?'` lists them) |
| `--mic-threshold 600` | fixed mic sensitivity if auto-calibration misfires |
| `--no-speak` | show the prompts on screen but stay silent |
| `--log ~/prompts.txt` | append every spoken prompt to a file |
| `--name` / `--user` | who it is, and who you are |
| `--save` | persist the current settings to the config file |

## How the reply is extracted

An agent TUI repaints constantly, overwrites lines with `\r`, and draws boxes,
spinners and hint bars. voicepilot reassembles the output into lines (modelling
`\n` and `\r`, and the CRLF a pty emits for every newline), marks where your
prompt was typed, then drops anything that is furniture rather than speech:
box-drawing rules, the input box interior, `? for shortcuts`, mode indicators,
spinner and status lines, tool-result gutters, and the pty's echo of your own
prompt. Lines repeated by repaints are collapsed.

What is left gets flattened for speech — code fences become "code block", URLs
become "a link", markdown punctuation is dropped — then truncated at a sentence
boundary.

It is a heuristic, tuned against Claude Code's output. If your agent's chrome
leaks into the speech, `--no-read-reply` falls back to a plain completion
announcement.

## How "done" is detected

Agents repaint a spinner while they work, so a quiet output stream is a strong
"finished" signal. voicepilot looks only at the agent's **final frame** — the
burst of output written just before it went quiet — rather than the whole
scrollback, because a TUI erases text it has already drawn. If that final frame
still shows an interrupt hint, it waits `--busy-idle` seconds instead, which
covers an agent sitting silently inside a long tool call.

If your agent repaints something periodically while idle (a clock in a status
line, say), idle is never reached — raise `--idle` won't help there; use
`--manual` and `Ctrl-]`.

## Solo mode - no agent at all

voicepilot also runs standalone as plain voice control of the machine. No AI
model, no wrapping, nothing to attach to:

```bash
voicepilot solo              # wake word "computer"
voicepilot solo --no-wake    # act on everything it hears
voicepilot solo --wake jarvis
```

It listens continuously but only acts on speech that starts with the wake
word, so ordinary conversation in the room is ignored.

| Say | It does |
|---|---|
| "computer open safari" | launches an app (knows common nicknames: chrome, vs code, settings) |
| "computer quit spotify" | closes an app |
| "computer search for pasta recipes" | opens a browser search |
| "computer move the cursor left 200" | moves the pointer; also "up/down/right", "to 500 600", "center the cursor" |
| "computer click" | also "double click", "right click" |
| "computer scroll down" / "scroll up ten" | scrolls |
| "computer type hello there" | types into whatever is focused |
| "computer press command s" | any key with modifiers; also "press escape", "press tab" |
| "computer copy" / "paste" / "undo" / "select all" / "close tab" | the usual shortcuts, with the right modifier per OS |
| "computer volume up" / "mute" | audio |
| "computer take a screenshot" | saves to the desktop |
| "computer lock the screen" | locks |
| "computer where is the cursor" / "what time is it" | spoken answers |
| "computer go to sleep" | stops acting until "computer wake up" |
| "computer quit voicepilot" | exits |
| "computer help" | reads the list back |

Multiple monitors are handled — a display above or left of the main one sits at
negative coordinates, and the cursor can reach it.

### Solo mode permissions

Moving the cursor and opening apps need no permission. **Clicks, typing and key
presses do**, and this is separate from the microphone:

- **macOS** — System Settings → Privacy & Security → **Accessibility** → enable
  your terminal app. `voicepilot --check` reports whether this is granted.
- **Linux** — needs `xdotool` (`sudo apt install xdotool`).
- **Windows** — works natively, unlike agent-wrapping mode, since solo mode
  drives the desktop rather than a pty. Untested by the author.

Solo mode adds no dependencies: it uses CoreGraphics via `ctypes` plus
`osascript` on macOS, `user32` on Windows, and `xdotool` on Linux.

## Install on another machine

Copy the four files — `voicepilot.py`, `requirements.txt`, `install.sh`,
`README.md` — to the new machine and run the installer. **Do not copy `.venv`**;
it hard-codes paths and binaries from the old machine.

```bash
cd voicepilot
./install.sh
voicepilot --check
```

`install.sh` picks a Python that has wheels (3.12 first, since compiled deps
like `ctranslate2` lag behind new releases), builds `.venv`, rewrites the
script's shebang to that venv's absolute path, and symlinks `voicepilot` into
`~/.local/bin`. It uses `uv` if present, otherwise plain `venv` + `pip`.

Per-platform:

| OS | Notes |
|---|---|
| **macOS** | Works as-is. TTS is the built-in `say`. Grant the terminal mic permission. |
| **Linux** | Also run `sudo apt install espeak-ng libportaudio2` (TTS engine + audio backend). `install.sh` tells you if either is missing. |
| **Windows** | Agent-wrapping needs **WSL** (`pty`/`termios`/`fcntl` don't exist natively). Solo mode runs on native Windows. |

No API keys and no internet are needed after the first run — the Whisper model
is cached locally.

## Setup notes

Dependencies live in `.venv` next to the script (Python 3.12 — `faster-whisper`
has no wheels for your default Python 3.14). The shebang points at that venv,
so the script just runs.

To reinstall:

```bash
cd ~/Desktop/voicepilot
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

**Microphone permission:** macOS silently returns all-zero audio when the
terminal lacks mic access. `--check` detects exactly that and tells you. Grant
it in System Settings → Privacy & Security → Microphone, then restart your
terminal app.

The first run downloads the Whisper `base.en` model (~75 MB) to
`~/.cache/huggingface`. After that everything is offline and local — your voice
never leaves the machine.
