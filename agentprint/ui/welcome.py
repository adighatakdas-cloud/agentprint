"""The welcome screen: mascot, name, roster summary and the three commands."""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text

from agentprint.install import STATE_DIR
from agentprint.roster import AgentState, summary_line
from agentprint.sanitize import clean
from agentprint.ui.layout import MIN_ROOM, fit_middle, hang, wrap
from agentprint.ui.mascot import SMALL, half_blocks

INDENT = " " * 10  # mascot (7 columns) + GAP
GAP = "   "
PLACE = " · offline · no telemetry"
COMMANDS = (
    ("scan", "sweep this machine for prints"),
    ("pack", "preflight a package before it ships"),
    ("install", "arm every agent"),
)
TAGLINE = "The security check your AI coding agent runs for you."


def header_lines(
    version: str, cwd: str, light: bool = False, width: int | None = None
) -> list[Text]:
    """Mascot, name, tagline and folder, built to fit `width` columns (None: no limit).

    The folder name is cleaned here, for every caller: a folder can be named with escape
    sequences (window titles, clipboard writes). On a narrow terminal it is shortened with a
    "…" in the middle, and text that still does not fit wraps under the name.
    """
    cwd = clean(cwd)
    room = None if width is None else width - len(INDENT)
    if room is not None:
        cwd = fit_middle(cwd, max(room - len(PLACE), MIN_ROOM))
    mascot = half_blocks(SMALL, light=light)
    first = mascot[0].copy()
    first.rstrip()
    name = Text()
    name.append("agent", style="ap.strong")
    name.append("print", style="ap.brand")
    name.append(f" {version}", style="ap.dim")
    beside = [
        name,
        *wrap(Text(TAGLINE), room),
        *wrap(Text(cwd + PLACE, style="ap.dim"), room),
    ]
    lines = [first]
    for i, text in enumerate(beside, 1):
        line = mascot[i].copy() if i < len(mascot) else Text(INDENT[: -len(GAP)])
        line.append(GAP)
        line.append_text(text)
        lines.append(line)
    return lines


def summary_lines(states: list[AgentState], width: int | None = None) -> list[Text]:
    """The roster summary, indented, with "armed" in amber and the unarmed names dim."""
    summary = summary_line(states)
    body = Text()
    head, sep, tail = summary.partition("armed")
    body.append(head)
    if sep:
        body.append("armed", style="ap.amber")
        if tail.startswith(" · "):
            body.append(" · ", style="ap.dim")
            body.append(tail[3:], style="ap.dim")
        else:
            body.append(tail)
    return hang(INDENT, body, width)


def welcome_lines(
    states: list[AgentState],
    version: str,
    cwd: str,
    light: bool = False,
    width: int | None = None,
) -> list[Text]:
    """The whole welcome, every line fitting `width` columns (None: the spec's layout as is)."""
    lines = header_lines(version, cwd, light, width)
    lines += [Text(""), *summary_lines(states, width), Text("")]
    for cmd, desc in COMMANDS:
        lead = Text(INDENT)
        lead.append("› ", style="ap.dim")
        lead.append("agentprint ")
        lead.append(cmd, style="ap.strong")
        lead.append(" " * (10 - len(cmd)))
        lines += hang(lead, Text(desc, style="ap.dim"), width)
    return lines


def seen_welcome(home: Path) -> bool:
    """True once the welcome was shown. A folder that can't be read counts as seen: the mark
    couldn't be written there either, so the welcome would otherwise play on every run."""
    try:
        return (home / STATE_DIR / "welcomed").exists()
    except OSError:
        return True


def mark_welcome(home: Path) -> None:
    """Record that the welcome was shown. Never fails: a read-only home is fine."""
    try:
        folder = home / STATE_DIR
        if not folder.exists():
            folder.mkdir(parents=True)
            folder.chmod(0o700)
        fd = os.open(folder / "welcomed", os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        (folder / "welcomed").chmod(0o600)
    except OSError:
        pass
