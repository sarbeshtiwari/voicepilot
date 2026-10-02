"""A stand-in terminal agent: prints a spinner, then a reply, then a prompt box."""
import sys, time

REPLIES = {
    "first task": "Done. I refactored the login handler. Which environment should I deploy to?",
    "second task": "All tests pass and the build is green.",
}

def box():
    print("╭──────────────╮")
    print("│ >            │")
    print("╰──────────────╯")
    print("  ? for shortcuts")

print("FakeAgent ready."); box(); sys.stdout.flush()
for line in sys.stdin:
    task = line.strip()
    if not task:
        continue
    if task == "/exit":
        break
    for i in range(2):                       # spinner, overwritten with \r
        print(f"\r  working{'.' * (i + 1)}  esc to interrupt", end="")
        sys.stdout.flush(); time.sleep(0.25)
    print(f"\n● {REPLIES.get(task, 'unknown task ' + task)}")
    box(); sys.stdout.flush()
