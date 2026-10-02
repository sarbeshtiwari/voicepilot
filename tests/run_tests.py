#!/usr/bin/env python3
"""voicepilot self-tests.  Run:  ./tests/run_tests.py  [-q]

No test framework, to match the project's zero-dependency policy.
"""
import importlib.util, os, re, select, sys, time
try:
    import pty
except ImportError:
    pty = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("vp", os.path.join(ROOT, "voicepilot.py"))
vp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(vp)

QUIET = "-q" in sys.argv
PASS = FAIL = 0
GRN, RED, DIM, RST = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        if not QUIET:
            print(f"  {GRN}PASS{RST}  {name}")
    else:
        FAIL += 1
        print(f"  {RED}FAIL{RST}  {name}" + (f"\n        {detail}" if detail else ""))


def section(title):
    if not QUIET:
        print(f"\n{DIM}── {title} {'─' * max(0, 58 - len(title))}{RST}")


def screen_of(text, crlf=True):
    """Feed text through Screen the way a pty would: newlines become CRLF."""
    sc = vp.Screen()
    sc.feed(text.replace("\n", "\r\n") if crlf else text)
    return sc


# ---------------------------------------------------------------- extraction
section("reply extraction")

RAW = ("● Done.\n"
       "  You're in /Users/x (not a git repo). What are we working on?\n"
       "✻ Sauteed for 3s · done\n"
       "╭─────────────╮\n│ >           │\n╰─────────────╯\n"
       "  ? for shortcuts\n  ⏵⏵ accept edits on (shift+tab)\n")
sc = screen_of(RAW); sc.mark(); sc.feed(RAW.replace("\n", "\r\n"))
reply = sc.reply()
check("keeps the answer", "Done." in reply)
check("keeps the trailing question", "What are we working on?" in reply)
check("drops box drawing", "╭" not in reply and "│" not in reply)
check("drops the hint bar", "shortcuts" not in reply)
check("drops spinner and status", "Sauteed" not in reply)
check("drops the mode indicator", "accept edits" not in reply)
check("strips the bullet glyph", not reply.lstrip().startswith("●"))

# a pty rewrites every \n as \r\n; treating that CR as an overwrite used to
# blank every line just before it was committed
crlf = screen_of("alpha\nbravo\n"); crlf.mark(); crlf.feed("charlie\r\ndelta\r\n")
check("CRLF does not erase lines", crlf.reply().splitlines() == ["charlie", "delta"],
      repr(crlf.reply()))

echo = vp.Screen(); echo.mark("my typed prompt")
echo.feed("my typed prompt\r\n● The answer.\r\n")
check("pty echo of your own prompt is dropped", echo.reply() == "The answer.",
      repr(echo.reply()))

head, tail = vp.speakable("A. " * 200, 200)
check("long replies truncate with a remainder", len(head) <= 210 and bool(tail))
spoken, _ = vp.speakable("Here is code:\n```\nx = 1\n```\nSee https://a.example/b", 400)
check("code fences and URLs are spoken as words",
      "code block" in spoken and "a link" in spoken, repr(spoken))

# ------------------------------------------------------------ done detection
section("knowing the agent finished")

PROMPTS = {
    "claude box": "● Done.\n╭───────╮\n│ >     │\n╰───────╯\n  ? for shortcuts\n",
    "python repl": "42\n>>> ", "aider prompt": "Applied edit\n\n> ",
    "shell prompt": "total 4\n$ ", "branch prompt": "ok\n(main) > ",
}
for name, text in PROMPTS.items():
    check(f"prompt detected: {name}", screen_of(text).at_prompt())

NOT_PROMPTS = {
    "plain reply": "I refactored the handler and all tests pass.\n",
    "box border only": "╭─────╮\n│ out │\n╰─────╯\n",
    "working spinner": "✻ Thinking... (3s · esc to interrupt)\n",
    "text ending in >": "the value is greater than 5 >\nmore text\n",
}
for name, text in NOT_PROMPTS.items():
    check(f"no false prompt: {name}", not screen_of(text).at_prompt())

for argv, want in ([["claude"], 2.5], [["/usr/bin/aider", "--model", "x"], 2.0],
                   [["ollama", "run", "llama3"], 1.5], [["python3"], 1.0],
                   [["some-unknown-agent"], None]):
    check(f"profile for {argv[0]}", vp.agent_profile(argv).get("idle") == want)

# ---------------------------------------------------------------- identity
section("naming and config")

g = "Good {daypart}, {user}. {name} ready. What should I ask?"
with_user = vp.personalize(g, "Nova", "Sarbesh")
no_user = vp.personalize(g, "Nova", "")
check("greets you by name", "Sarbesh" in with_user and with_user.startswith("Good "))
check("uses the assistant name", "Nova ready" in with_user)
check("time of day is real",
      any(d in with_user for d in ("morning", "afternoon", "evening")))
check("no stray comma when the name is unset",
      ", ." not in no_user and " ." not in no_user, repr(no_user))

def _args(*extra):
    saved, sys.argv = sys.argv, ["voicepilot"] + list(extra)
    try:
        a = vp.build_parser().parse_args(list(extra) + ["echo"])
    finally:
        sys.argv = saved
    # only fill in identity where the caller did not ask for one, so an
    # explicit --name still drives the wake word
    if a.name == "Pilot":
        a.name = "Nova"
    if not a.user:
        a.user = "Sarbesh"
    return a


sess = object.__new__(vp.Session)
sess.a = _args(); sess.turns = 1; sess.reply_tail = ""
sess.reply_head = 'I set it to {"a": 1, "b": {"c": 2}}'
line = sess._announce_line("idle", False)
check("a reply full of braces survives verbatim",
      '{"a": 1, "b": {"c": 2}}' in line, repr(line))
sess.turns = 0; sess.reply_head = ""
check("first turn greets you", "Sarbesh" in sess._announce_line("idle", False))
sess.turns = 1
check("fallback line stays clean",
      sess._announce_line("idle", False) == "Task complete. What's next?")


# ------------------------------------------------------------- solo grammar
section("solo command grammar")

vp.status = lambda msg: None          # the code under test narrates; hush it

RECORDED = ("open_app", "quit_app", "web_search", "move_to", "move_by", "center",
            "click", "scroll", "type_text", "press", "volume", "screenshot",
            "lock", "cursor_pos", "focus_app", "window", "media", "brightness",
            "sleep_display", "clipboard_get", "clipboard_set", "open_path",
            "open_url", "drag_to", "power")
CALLS = []


class FakeDesk(vp.Desktop):
    def __getattribute__(self, n):
        if n in RECORDED:
            def rec(*a, **k):
                CALLS.append((n, a, k)); return f"{n} ok"
            return rec
        return object.__getattribute__(self, n)


solo = vp.SoloSession(_args("--solo", "--no-speak", "--tts", "none",
                            "--name", "Computer"))
solo.desk = FakeDesk(); solo.rules = solo._build_rules()


def heard(utterance):
    CALLS.clear()
    cmd = solo._addressed(utterance)
    if cmd is None:
        return "NOT-ADDRESSED"
    if solo.pending:
        solo._resolve_pending(cmd)
        return CALLS[0] if CALLS else "CANCELLED"
    if not solo.awake and cmd not in ("wake up", "start listening", "resume"):
        return "ASLEEP"
    solo._execute(cmd)
    return CALLS[0] if CALLS else "NO-CALL"


MOD = ("command",)
SHORTCUT_MOD = ("command",) if solo.desk.os == "mac" else ("control",)
for utterance, want in [
    ("computer open safari",               ("open_app", ("safari",), {})),
    ("computer click",                     ("click", ("left",), {})),
    ("computer double click",              ("click", ("left", 2), {})),
    ("computer type hello there",          ("type_text", ("hello there",), {})),
    ("computer press command s",           ("press", ("s", MOD), {})),
    ("computer copy",                      ("press", ("c", SHORTCUT_MOD), {})),
    ("computer scroll down",               ("scroll", ("down", 5), {})),
    ("computer move the cursor left 200",  ("move_by", ("left", 200), {})),
    ("computer volume up",                 ("volume", ("up",), {})),
    # ordering traps: these must not fall through to open_app / quit_app
    ("computer open downloads",            ("open_path", ("downloads",), {})),
    ("computer go to github dot com",      ("open_url", ("github dot com",), {})),
    ("computer close window",              ("window", ("close",), {})),
    ("computer close tab",                 ("press", ("w", SHORTCUT_MOD), {})),
    ("computer quit spotify",              ("quit_app", ("spotify",), {})),
    ("computer switch to chrome",          ("focus_app", ("chrome",), {})),
    ("computer minimize",                  ("window", ("minimize",), {})),
    ("computer play",                      ("media", ("play",), {})),
    ("computer brightness down",           ("brightness", ("down",), {})),
    ("computer read the clipboard",        ("clipboard_get", (), {})),
    ("computer drag to 800 400",           ("drag_to", (800, 400), {})),
    ("so anyway I told him it was fine",   "NOT-ADDRESSED"),
    ("computer flibbertigibbet",           "NO-CALL"),
]:
    got = heard(utterance)
    check(utterance, got == want, f"got {got}\n        want {want}")

for raw, dflt, want in [("", 100, 100), ("200", 100, 200), ("to 100", 100, 100),
                        ("by about 250", 100, 250), ("ten", 5, 10),
                        ("two hundred pixels", 100, 200), ("gibberish", 42, 42)]:
    check(f"amount from {raw!r}", vp._amount(raw, dflt) == want)

section("dictation and confirmation")
heard("computer start dictating")
check("'start dictating' enters dictation", solo.dictating)
CALLS.clear(); solo._dictate("Hello there, this is a test.")
check("raw transcript typed verbatim",
      CALLS and CALLS[0] == ("type_text", ("Hello there, this is a test.",), {}))
CALLS.clear(); solo._dictate("new line")
check("'new line' presses return", CALLS and CALLS[0] == ("press", ("return",), {}))
solo._dictate("stop dictating")
check("'stop dictating' exits", not solo.dictating)

CALLS.clear(); solo._execute("shut down")
check("'shut down' asks before acting", solo.pending and not CALLS)
check("saying no cancels", heard("computer no") == "CANCELLED" and not solo.pending)
solo._execute("restart")
check("saying yes runs it", heard("computer yes") == ("power", ("restart",), {}))

# ------------------------------------------------------- platform coverage
section("windows and linux command generation")


class FakeU32:
    def __getattr__(self, n):
        def f(*a, **k):
            CALLS.append(("user32." + n, a)); return 0
        return f


def probe(osname, call):
    d = vp.Desktop(); d.os = osname; d._u32 = FakeU32()
    d._sh = lambda cmd: CALLS.append(("sh", list(cmd)))
    d._osa = lambda sc: CALLS.append(("osa", sc))
    d._xdo = lambda *a: CALLS.append(("xdo", list(a)))
    d.cursor_pos = lambda: (100, 100)
    d.screen_bounds = lambda: (0, 0, 1920, 1080)
    CALLS.clear()
    try:
        call(d)
    except vp.DesktopError as e:
        return ("declines", str(e))
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")
    return CALLS[0] if CALLS else ("none", "")


win_open = probe("win", lambda d: d.open_app("chrome"))
check("windows launches chrome, not the macOS app name",
      "chrome" in " ".join(win_open[1]) and "Google Chrome" not in " ".join(win_open[1]),
      str(win_open))
lin_open = probe("linux", lambda d: d.open_app("chrome"))
check("linux resolves google-chrome", "google-chrome" in str(lin_open), str(lin_open))
# SendKeys has no Win key and cannot do Alt+Tab, so these must be raw key events
win_min = probe("win", lambda d: d.window("minimize"))
check("windows minimize uses the Win key, not SendKeys",
      win_min[0] == "user32.keybd_event" and win_min[1][0] == 0x5B, str(win_min))
win_switch = probe("win", lambda d: d.window("switch"))
check("windows alt-tab uses raw key events",
      win_switch[0] == "user32.keybd_event" and win_switch[1][0] == 0x12, str(win_switch))
for osname in ("win", "linux"):
    for label, call in [("type", lambda d: d.type_text("hi")),
                        ("press", lambda d: d.press("s", ("command",))),
                        ("click", lambda d: d.click("left")),
                        ("scroll", lambda d: d.scroll("down", 3)),
                        ("volume", lambda d: d.volume("up")),
                        ("lock", lambda d: d.lock()),
                        ("power", lambda d: d.power("restart"))]:
        r = probe(osname, call)
        check(f"{osname}: {label} issues a command", r[0] != "error", str(r))


# -------------------------------------------------------- windows runtime
section("runs without a pty (native Windows)")


def import_without_pty():
    """Windows has no pty/termios/tty/fcntl; the module must still load."""
    import builtins
    blocked = {"pty", "termios", "tty", "fcntl"}
    real = builtins.__import__

    def guarded(name, *a, **k):
        if name in blocked:
            raise ImportError(f"no module named {name} (simulated)")
        return real(name, *a, **k)

    builtins.__import__ = guarded
    try:
        spec = importlib.util.spec_from_file_location(
            "vpwin", os.path.join(ROOT, "voicepilot.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        builtins.__import__ = real


try:
    win = import_without_pty()
    check("module imports with no pty available", True)
    check("HAVE_PTY reports false", win.HAVE_PTY is False)
    saved, sys.argv = sys.argv, ["voicepilot"]
    wa = win.build_parser().parse_args(["--solo", "--tts", "none", "--no-speak"])
    sys.argv = saved
    wa.name, wa.user, wa.wake = "Nova", "Sarbesh", "nova"
    win.status = lambda m: None
    wsolo = win.SoloSession(wa); wsolo.desk.os = "win"
    check("solo mode builds without a pty", len(wsolo.rules) > 30)

    wcalls = []
    wsolo.desk._sh = lambda c: wcalls.append(list(c))

    class _U32:
        def __getattr__(self, n):
            return lambda *x: wcalls.append([n, *x]) or 0

    wsolo.desk._u32 = _U32()
    for cmd in ("open notepad", "minimize", "type hello", "volume up"):
        wcalls.clear()
        try:
            wsolo._execute(cmd)
            check(f"windows: {cmd!r} issues a command", bool(wcalls))
        except Exception as e:
            check(f"windows: {cmd!r} issues a command", False, f"{type(e).__name__}: {e}")
except Exception as e:
    check("module imports with no pty available", False, f"{type(e).__name__}: {e}")


# ------------------------------------------------------- end to end, in a pty
section("agent loop, end to end in a pty")


def agent_loop():
    """Drive a real pty session against the fake agent, with voice stubbed."""
    script = ["first task", "repeat", "second task", "stop listening"]

    def child():
        spec = importlib.util.spec_from_file_location(
            "vp2", os.path.join(ROOT, "voicepilot.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

        class Rec:
            def __init__(self, **k): pass
            @staticmethod
            def available(): return True
            def record(self, **k): time.sleep(0.2); return "/dev/null"

        class Stt:
            backend = "fake"; available = True; model_name = None; bias = None
            def __init__(self, *a, **k): self.i = 0
            def warm(self): pass
            def transcribe(self, path):
                t = script[self.i] if self.i < len(script) else ""
                self.i += 1
                return t

        class Spk:
            backend = "fake"; available = True
            def __init__(self, *a, **k): pass
            def say(self, t): print(f"[SPOKE] {t}", flush=True)
            def stop(self): pass

        m.Recorder, m.Transcriber, m.Speaker = Rec, Stt, Spk
        sys.argv = ["voicepilot"]
        a = m.build_parser().parse_args(
            ["--idle", "1.0", "--startup-grace", "0.5",
             sys.executable, os.path.join(HERE, "fake_agent.py")])
        os.unlink = lambda p: None        # the stub hands back /dev/null
        sys.exit(m.Session(a.command, a).run())

    pid, fd = pty.fork()
    if pid == 0:
        child()
    out = b""
    deadline = time.time() + 45
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.3)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if os.waitpid(pid, os.WNOHANG)[0]:
            break
    return out.decode("utf-8", "ignore")


if pty is not None:
    txt = agent_loop()
    spoke = [l.split("[SPOKE] ", 1)[1].strip()
             for l in txt.splitlines() if "[SPOKE] " in l]
    check("reads the agent's real reply aloud",
          any("refactored the login handler" in l for l in spoke), str(spoke))
    check("no generic 'Task complete' over a real reply",
          not any(l.startswith("Task complete") for l in spoke), str(spoke))
    check("a reply ending in a question omits the follow-up",
          any(l.rstrip().endswith("Which environment should I deploy to?")
              for l in spoke), str(spoke))
    check("a non-question reply gets the follow-up",
          any("tests pass" in l and l.rstrip().endswith("What's next?") for l in spoke),
          str(spoke))
    check("'repeat' re-speaks it",
          sum("refactored the login handler" in l for l in spoke) >= 2)
    check("'repeat' is never sent to the agent", "unknown task repeat" not in txt)
    check("the agent received both tasks",
          "refactored" in txt and "tests pass" in txt)
    check("your own prompt is not read back",
          not any(l.strip() in ("first task", "second task") for l in spoke))


def exit_code_case():
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable,
                 [sys.executable, os.path.join(ROOT, "voicepilot.py"), "--manual", "--no-speak", "--stt", "none",
                  "/bin/sh", "-c", "echo FINAL-LINE; exit 42"])
    out, status = b"", None
    deadline = time.time() + 20
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.3)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        got, st = os.waitpid(pid, os.WNOHANG)
        if got:
            status = st
            break
    if status is None:
        _, status = os.waitpid(pid, 0)
    return out.decode("utf-8", "ignore"), os.waitstatus_to_exitcode(status)


if pty is not None:
    text, code = exit_code_case()
    check("final output before exit is not dropped", "FINAL-LINE" in text)
    check("the agent's exit code is propagated", code == 42, f"got {code}")
else:
    print("SKIP: Unix PTY integration tests require macOS/Linux/WSL")

# ---------------------------------------------------------------- summary
print(f"\n{'=' * 62}")
if FAIL:
    print(f"  {RED}{FAIL} failed{RST}, {PASS} passed")
else:
    print(f"  {GRN}all {PASS} checks passed{RST}")
sys.exit(1 if FAIL else 0)
