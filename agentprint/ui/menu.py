"""Bare `agentprint` for a person at a terminal: a small arrow-key menu.

Only a person at a real terminal gets here (`detect.is_interactive`); AI agents, CI and pipes
get the help text instead. Two safety nets for an AI agent nobody recognised: the commands are
printed above the menu, so reading the screen is enough to learn them, and the menu closes by
itself after AGENTPRINT_MENU_IDLE seconds (default 60) without a key, so nothing waits for good.

Keys come from termios on macOS and Linux (the arrow-key menu) and from msvcrt on Windows (a
numbered list: press 1-7). Both read against the same idle deadline.
"""

from __future__ import annotations

import math
import os
import select
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from agentprint import __version__
from agentprint.roster import AgentState
from agentprint.ui.layout import hang
from agentprint.ui.welcome import INDENT, header_lines, summary_lines

# (action, label, help): the action is what the CLI runs for that row.
ITEMS: tuple[tuple[str, str, str], ...] = (
    ("scan", "Sweep this machine", "find prints AI agents left in history and settings"),
    ("pack", "Preflight a package", "check what this folder would publish"),
    ("install", "Arm my agents", "add the tripwire to every agent (shows the changes first)"),
    ("uninstall", "Disarm my agents", "remove what install added"),
    ("status", "Roster", "which agents are armed"),
    ("help", "Help", "every command and option"),
    ("quit", "Quit", ""),
)

# Returns "up", "down", "enter", "quit" or a row number "1".."7"; None when `timeout` seconds
# pass without a key.
KeyReader = Callable[[float], str | None]

COMMANDS = ("scan", "pack", "install", "status", "--help")
HINT = f"↑↓ move · enter pick · 1-{len(ITEMS)} jump · q quit"
IDLE_DEFAULT = 60.0
IDLE_MAX = 3600.0
ESC_WAIT = 0.05  # how long a lone ESC waits for the rest of an arrow key's sequence
LABEL_WIDTH = max(len(label) for _, label, _ in ITEMS)
HELP_MIN = 30  # the rows keep the header's indent while the help column gets this many
INDENTED_WIDTH = len(INDENT) + 2 + LABEL_WIDTH + 2 + HELP_MIN


def idle_seconds(env: Mapping[str, str]) -> float:
    """AGENTPRINT_MENU_IDLE in seconds (up to an hour); anything else means 60."""
    try:
        value = float(env.get("AGENTPRINT_MENU_IDLE", IDLE_DEFAULT))
    except ValueError:
        return IDLE_DEFAULT
    return value if 0 < value <= IDLE_MAX and math.isfinite(value) else IDLE_DEFAULT


def intro_lines(
    states: Sequence[AgentState], cwd: str, light: bool = False, width: int | None = None
) -> list[Text]:
    """What stays on screen above the menu: header, roster summary and the commands, built to
    fit `width` columns."""
    commands = Text("agentprint ")
    for i, word in enumerate(COMMANDS):
        if i:
            commands.append(" · ", style="ap.dim")
        commands.append(word, style="ap.strong")
    return [
        *header_lines(__version__, cwd, light, width),
        Text(""),
        *summary_lines(list(states), width),
        *hang(INDENT, commands, width),
        Text(""),
    ]


def rows(current: int) -> list[tuple[Text, Text, Text]]:
    """(pointer, label, help) per item: the current row gets an amber › and a strong label."""
    out = []
    for i, (_, label, help_text) in enumerate(ITEMS):
        here = i == current
        out.append(
            (
                Text("›", style="ap.amber") if here else Text(" "),
                Text(label, style="ap.strong" if here else "ap.dim"),
                Text(help_text, style="ap.dim"),
            )
        )
    return out


def view(current: int, width: int) -> RenderableType:
    """The menu, under the header's indent. Help text wraps in its own column; only a very
    narrow terminal drops the indent."""
    table = Table.grid(padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True, min_width=2 + LABEL_WIDTH)
    table.add_column()
    for pointer, label, help_text in rows(current):
        table.add_row(Text.assemble(pointer, " ", label), help_text)
    left = len(INDENT) if width >= INDENTED_WIDTH else 0
    hint = Text(HINT, style="ap.dim")
    return Padding(Group(table, Text(""), hint), (0, 0, 0, left), expand=False)


def choose(
    console: Console,
    reader: KeyReader,
    idle: float,
    states: Sequence[AgentState] = (),
    cwd: str = ".",
    light: bool = False,
    numbered: bool = False,
) -> str | None:
    """Print the intro, then let the person pick a row. None: quit, or `idle` s without a key.

    numbered (Windows): a numbered list, picked with a digit, instead of the arrow-key menu.
    """
    for line in intro_lines(states, cwd, light, console.width):
        console.print(line)
    if numbered:
        return _numbered(console, reader, idle)
    current, picked, timed_out = 0, None, False
    live = Live(view(current, console.width), console=console, auto_refresh=False, transient=True)
    # Not `with Live(...)`: a Ctrl-C that lands while the first frame is drawn would skip
    # __exit__ and leave the cursor hidden. stop() is safe to call at any point.
    try:
        live.start(refresh=True)
        while True:
            key = reader(idle)
            if key is None:
                timed_out = True
                break
            if key == "quit":
                break
            if key == "enter":
                picked = current
                break
            if key.isdigit() and 1 <= int(key) <= len(ITEMS):
                picked = int(key) - 1
                break
            if key in ("up", "down"):
                current = (current + (1 if key == "down" else -1)) % len(ITEMS)
                live.update(view(current, console.width), refresh=True)
    finally:
        live.stop()
    if timed_out:
        return _closed(console, idle)
    return _picked(console, picked)


def _closed(console: Console, idle: float) -> None:
    console.print(
        Text(f"{INDENT}menu closed after {idle:.0f} s idle · run agentprint again"),
        style="ap.dim",
    )


def _picked(console: Console, index: int | None) -> str | None:
    if index is None or ITEMS[index][0] == "quit":
        return None
    action, label, _ = ITEMS[index]
    line = Text(INDENT)
    line.append("› ", style="ap.amber")
    line.append(label, style="ap.strong")
    console.print(line)
    console.print()
    return action


def _numbered(console: Console, reader: KeyReader, idle: float) -> str | None:
    """The numbered list: a digit picks, q, Esc or Enter quits, other keys are ignored."""
    for n, (_, label, help_text) in enumerate(ITEMS, 1):
        lead = Text(f"{INDENT}{n}  ")
        lead.append(label.ljust(LABEL_WIDTH), style="ap.strong")
        lead.append("  ")
        for line in hang(lead, Text(help_text, style="ap.dim"), console.width):
            line.rstrip()
            console.print(line)
    console.print()
    console.print(Text(f"{INDENT}press 1-{len(ITEMS)} · q quit", style="ap.dim"))
    while True:
        key = reader(idle)
        if key is None:
            return _closed(console, idle)
        if key in ("quit", "enter"):
            return None
        if key.isdigit() and 1 <= int(key) <= len(ITEMS):
            return _picked(console, int(key) - 1)


def key_reader(
    stream: Any, system: str = os.name
) -> tuple[AbstractContextManager[KeyReader], bool]:
    """This system's key reader, and whether the menu is the numbered list (Windows)."""
    if system == "posix":
        return posix_reader(stream), False
    return windows_reader(), True


# ---- keys on macOS and Linux ------------------------------------------------------------------


class _Fileno(Protocol):
    def fileno(self) -> int: ...


_KEYS = {
    b"\r": "enter",
    b"\n": "enter",
    b"q": "quit",
    b"Q": "quit",
    b"\x04": "quit",  # Ctrl-D
    b"k": "up",
    b"j": "down",
}
_ARROWS = {ord("A"): "up", ord("B"): "down"}


def _next_key(buf: bytes) -> tuple[str | None, int]:
    """The first key in `buf` and how many bytes it used.

    (None, n): n bytes of something the menu ignores. (None, 0): an escape sequence that is
    cut short, so far.
    """
    if buf[:1] != b"\x1b":
        first = buf[:1]
        if first.isdigit() and 1 <= int(first) <= len(ITEMS):
            return first.decode(), 1
        return _KEYS.get(first), 1
    if len(buf) < 2:
        return None, 0
    if buf[1:2] == b"O":  # ESC O A: arrows in "application" cursor mode
        return (_ARROWS.get(buf[2]), 3) if len(buf) >= 3 else (None, 0)
    if buf[1:2] == b"[":  # ESC [ <numbers> <final>: arrows, and keys the menu skips
        for i in range(2, len(buf)):
            if 0x40 <= buf[i] <= 0x7E:
                return _ARROWS.get(buf[i]), i + 1
        return None, 0
    return "quit", 1  # ESC followed by another key: the ESC alone means quit


def _key_reader(fd: int) -> KeyReader:
    pending = bytearray()

    def read_key(timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            while pending:
                key, used = _next_key(bytes(pending))
                if used == 0:  # cut short: give the rest of the sequence a moment to arrive
                    ready, _, _ = select.select([fd], [], [], ESC_WAIT)
                    more = os.read(fd, 64) if ready else b""
                    if more:
                        pending.extend(more)
                        continue
                    key, used = ("quit" if pending == b"\x1b" else None), len(pending)
                del pending[:used]
                if key is not None:
                    return key
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            data = os.read(fd, 64)
            if not data:  # the terminal went away
                return "quit"
            pending.extend(data)

    return read_key


@contextmanager
def posix_reader(stream: _Fileno) -> Iterator[KeyReader]:
    """Single keys from a terminal (macOS, Linux), for the life of the `with` block.

    Inside the block the terminal is in cbreak mode: keys arrive one at a time and are not
    echoed, and Ctrl-C still interrupts. `finally` puts the saved settings back on every exit
    path, and drops keys typed but never read, so they don't spill into the next prompt.
    """
    import termios  # POSIX only: imported here so this module still imports on Windows

    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    try:
        mode = termios.tcgetattr(fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        yield _key_reader(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)


# ---- keys on Windows --------------------------------------------------------------------------

POLL = 0.02  # how often the Windows reader looks for a key while it waits
_WIN_KEYS = {
    "\r": "enter",
    "\n": "enter",
    "q": "quit",
    "Q": "quit",
    "\x1b": "quit",  # Esc
    "\x04": "quit",  # Ctrl-D
    "\x1a": "quit",  # Ctrl-Z
    "k": "up",
    "j": "down",
}
_WIN_ARROWS = {"H": "up", "P": "down"}


def _win_key(ch: str, second: Callable[[], str]) -> str | None:
    """One key from msvcrt.getwch(). Arrows and function keys arrive as two characters: a
    prefix ("\\xe0" or "\\x00"), then their code, which `second` reads."""
    if ch in ("\xe0", "\x00"):
        return _WIN_ARROWS.get(second())
    if ch == "\x03":  # Ctrl-C, when the console hands it over as a key
        raise KeyboardInterrupt
    if len(ch) == 1 and "1" <= ch <= str(len(ITEMS)):
        return ch
    return _WIN_KEYS.get(ch)


@contextmanager
def windows_reader() -> Iterator[KeyReader]:
    """Single keys from the Windows console for the life of the `with` block, each read
    waiting at most `timeout` seconds. Keys typed but never read are dropped on the way out."""
    import msvcrt  # Windows only: imported here so this module still imports elsewhere

    def read_key(timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            while msvcrt.kbhit():
                key = _win_key(msvcrt.getwch(), msvcrt.getwch)
                if key is not None:
                    return key
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(POLL, remaining))

    try:
        yield read_key
    finally:
        while msvcrt.kbhit():
            msvcrt.getwch()
