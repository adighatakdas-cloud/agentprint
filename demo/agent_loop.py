"""A simulated AI-agent turn that uses the real agentprint, for demo/agent_loop.tape.

    python demo/agent_loop.py <demo world folder from build_demo.py>

The agent is simulated: its "decisions" are fixed lines in this file, printed dim and marked
"agent:" (the demo's own narration is dim too). Everything else on screen is real agentprint
output. The script runs the installed `agentprint` (override with AGENTPRINT_CMD):

1. the agent wants to run `npm publish`; the pre-tool hook calls `agentprint guard`, which says no;
2. the agent runs `agentprint fix --pack --yes --format agent` in the package folder (ignore
   lines, key masked), then `agentprint fix --yes --format agent` for the machine's agent files;
3. it retries the publish; the hook lets it through;
4. the agent tells the human which keys to rotate, read from the two fix answers.

Keys are always shown redacted by agentprint; nothing outside the world folder is touched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path

BANNER = "simulated agent · real agentprint output"
LEGEND = "(dim lines: the simulated agent and the demo)"
COMMAND = "npm publish"
FIX_PACK = "agentprint fix --pack --yes --format agent"
FIX = "agentprint fix --yes --format agent --home fake-home my-app"
WIDTH = 100
SPEED = float(os.environ.get("AGENT_LOOP_SPEED", "1"))  # tests set 0: no pauses, no screen clears
AGENT = "\033[2;36m"  # dim cyan: the simulated agent's own words
NARRATION = "\033[2m"  # dim: the demo describing what happens
RESET = "\033[0m"
CUT = "Tell the human"  # fix answers are shown up to their closing reminder
ROTATE = re.compile(r'Rotate the (.+?) that starts ("[^"]*")')


def pause(seconds: float) -> None:
    time.sleep(seconds * SPEED)


def say(text: str = "") -> None:
    print(text, flush=True)


def agent_says(text: str) -> None:
    """A line the simulated agent "says": written in this file, not printed by agentprint."""
    say(f"{AGENT}agent: {text}{RESET}")


def narrate(text: str) -> None:
    say(f"{NARRATION}{text}{RESET}")


def screen(title: str) -> None:
    if SPEED:
        print("\033[2J\033[H", end="")
    say(f"\033[1;33m[{BANNER}]\033[0m {NARRATION}{LEGEND}{RESET}")
    say()
    say(f"\033[1m{title}\033[0m")
    say()


def wrapped(text: str) -> None:
    """Print agentprint's text re-flowed to the screen width (layout only, same words)."""
    for line in text.splitlines():
        indent = line[: len(line) - len(line.lstrip())]
        parts = textwrap.wrap(
            line.strip(), WIDTH, initial_indent=indent, subsequent_indent=indent + "  "
        )
        for part in parts or [""]:
            say(part)


def agentprint(*args: str, stdin: str | None = None, cwd: Path) -> subprocess.CompletedProcess:
    windows = os.name == "nt"  # POSIX splitting would eat the backslashes of C:\\path\\python.exe
    cmd = [
        part.strip('"') if windows else part
        for part in shlex.split(os.environ.get("AGENTPRINT_CMD", "agentprint"), posix=not windows)
    ]
    return subprocess.run(
        [*cmd, *args], input=stdin, capture_output=True, text=True, cwd=cwd, timeout=120
    )


def guard(project: Path, cwd: Path) -> str | None:
    """The hook call: the reason the command is blocked, or None when it may run."""
    payload = {"tool_name": "Bash", "tool_input": {"command": COMMAND}, "cwd": str(project)}
    result = agentprint("guard", stdin=json.dumps(payload), cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(f"agentprint guard failed: {result.stderr}")
    if not result.stdout.strip():
        return None
    out = json.loads(result.stdout)["hookSpecificOutput"]
    return out["permissionDecisionReason"] if out["permissionDecision"] == "deny" else None


def run_fix(shown: str, args: list[str], where: Path) -> str:
    """The agent runs a real `agentprint fix` in `where`; we show its answer and return it."""
    agent_says(f"running (in {where.name}): {shown}")
    pause(1.5)
    fixed = agentprint(*args, cwd=where)
    if fixed.returncode != 0:
        raise SystemExit(f"{shown} failed: {fixed.stdout}{fixed.stderr}")
    head, cut, _ = fixed.stdout.partition(CUT)
    wrapped(head.rstrip())
    if cut:
        narrate("(shortened)")
    return fixed.stdout


def keys_to_rotate(*answers: str) -> list[str]:
    """Each key the fix answers tell the human to rotate, once: its name and redacted stub."""
    found: list[str] = []
    for answer in answers:
        for name, stub in ROTATE.findall(answer):
            if f"{name} {stub}" not in found:
                found.append(f"{name} {stub}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python demo/agent_loop.py <demo world folder>", file=sys.stderr)
        return 2
    world = Path(argv[0]).resolve()
    project = world / "my-app"
    if not (world / ".agentprint-demo").is_file():
        raise SystemExit(f"{world} is not a demo world made by demo/build_demo.py")

    screen("1. The agent wants to publish")
    agent_says(f"running: {COMMAND}")
    pause(1.5)
    narrate("hook: agentprint guard checks what would be uploaded ...")
    pause(1.5)
    reason = guard(project, world)
    if reason is None:
        raise SystemExit("expected the first publish to be blocked")
    screen("2. BLOCK: this is what the agent receives")
    wrapped(reason)
    pause(9)

    screen("3. The agent fixes the package")
    packed = run_fix(FIX_PACK, ["fix", "--pack", "--yes", "--format", "agent"], project)
    pause(6)

    screen("4. The agent tidies the machine")
    tidied = run_fix(
        FIX, ["fix", "--yes", "--format", "agent", "--home", "fake-home", "my-app"], world
    )
    pause(7)

    screen("5. The agent retries")
    agent_says(f"running again: {COMMAND}")
    pause(1.5)
    narrate("hook: agentprint guard checks again ...")
    pause(1.5)
    if guard(project, world) is not None:
        raise SystemExit("expected the retry to be allowed")
    agent_says("the hook let it run this time: agentprint guard did not block it.")
    pause(2)
    say()
    keys = keys_to_rotate(packed, tidied)
    if not keys:
        raise SystemExit("expected fix to ask the human to rotate keys")
    which = "this key" if len(keys) == 1 else f"these {len(keys)} keys"
    agent_says(f"Left for you: rotate {which}. Only their provider can stop them working:")
    for key in keys:
        agent_says(f"  - {key}")
    pause(6)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
