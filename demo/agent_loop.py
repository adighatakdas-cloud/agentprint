"""A simulated AI-agent turn that uses the real agentprint, for demo/agent_loop.tape.

    python demo/agent_loop.py <demo world folder from build_demo.py>

The agent is simulated: its "decisions" are fixed lines in this file. Everything agentprint
prints is real. The script runs the installed `agentprint` (override with AGENTPRINT_CMD):

1. the agent wants to run `npm publish`; the pre-tool hook calls `agentprint guard`, which says no;
2. the agent runs `agentprint fix --yes --format agent`, then does the one fix step left itself
   (keep `.claude/` out of the package: that is an edit by the agent, not something `fix` makes);
3. it retries the publish; the hook lets it through;
4. one line says what is left for the human.

Keys are always shown redacted by agentprint; nothing outside the world folder is touched.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path

BANNER = "simulated agent · real agentprint output"
COMMAND = "npm publish"
FIX = "agentprint fix --yes --format agent"
WIDTH = 100
SPEED = float(os.environ.get("AGENT_LOOP_SPEED", "1"))  # tests set 0: no pauses, no screen clears


def pause(seconds: float) -> None:
    time.sleep(seconds * SPEED)


def say(text: str = "") -> None:
    print(text, flush=True)


def screen(title: str) -> None:
    if SPEED:
        print("\033[2J\033[H", end="")
    say(f"\033[1;33m[{BANNER}]\033[0m")
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
    cmd = shlex.split(os.environ.get("AGENTPRINT_CMD", "agentprint"))
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


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python demo/agent_loop.py <demo world folder>", file=sys.stderr)
        return 2
    world = Path(argv[0]).resolve()
    project, home = world / "my-app", world / "fake-home"
    if not (world / ".agentprint-demo").is_file():
        raise SystemExit(f"{world} is not a demo world made by demo/build_demo.py")

    screen("1. The agent wants to publish")
    say(f"agent wants to run: {COMMAND}")
    pause(1.5)
    say("hook: agentprint guard checks what would be uploaded ...")
    pause(1.5)
    reason = guard(project, world)
    if reason is None:
        raise SystemExit("expected the first publish to be blocked")
    screen("2. BLOCK: this is what the agent receives")
    wrapped(reason)
    pause(9)

    screen("3. The agent fixes it")
    say(f"agent runs: {FIX}")
    pause(1.5)
    fixed = agentprint(
        "fix", "--yes", "--format", "agent", "--home", str(home), str(project), cwd=world
    )
    if fixed.returncode != 0:
        raise SystemExit(f"agentprint fix failed: {fixed.stderr}")
    wrapped(fixed.stdout.split("Tell the human")[0].rstrip())
    pause(7)
    say()
    say("agent does the last fix step itself: keep .claude/ out of the package")
    (project / ".npmignore").write_text(".claude/\n", encoding="utf-8")
    (project / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    say("  wrote .npmignore (.claude/) and .gitignore (.claude/settings.local.json)")
    pause(4)

    screen("4. The agent retries")
    say(f"agent runs again: {COMMAND}")
    pause(1.5)
    say("hook: agentprint guard checks again ...")
    pause(1.5)
    if guard(project, world) is not None:
        raise SystemExit("expected the retry to be allowed")
    say("\033[1;32mCLEAR: allowed. Nothing in the package leaks.\033[0m")
    pause(2)
    say()
    say("Left for you: rotate the 3 exposed keys. Masking hides them; only their provider")
    say("can stop them working. The agent has told you which.")
    pause(6)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
