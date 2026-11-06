"""The welcome screen: mascot, name, roster summary and the three commands."""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text

from agentprint.install import STATE_DIR
from agentprint.roster import AgentState, summary_line
from agentprint.ui.mascot import SMALL, half_blocks

INDENT = " " * 10
COMMANDS = (
    ("scan", "sweep this machine for prints"),
    ("pack", "preflight a package before it ships"),
    ("install", "arm every agent"),
)
TAGLINE = "The security check your AI coding agent runs for you."


def header_lines(version: str, cwd: str, light: bool = False) -> list[Text]:
    mascot = half_blocks(SMALL, light=light)
    first = mascot[0].copy()
    first.rstrip()
    lines = [first]
    name = Text()
    name.append("agent", style="ap.strong")
    name.append("print", style="bold ap.amber")
    name.append(f" {version}", style="ap.dim")
    beside = [
        name,
        Text(TAGLINE, style="ap.strong"),
        Text(f"{cwd} · offline · no telemetry", style="ap.dim"),
    ]
    for art, text in zip(mascot[1:], beside, strict=True):
        line = art.copy()
        line.append("   ")
        line.append_text(text)
        lines.append(line)
    return lines


def summary_text(states: list[AgentState]) -> Text:
    """The roster summary, indented, with "armed" in amber and the unarmed names dim."""
    summary = summary_line(states)
    line = Text(INDENT)
    head, sep, tail = summary.partition("armed")
    line.append(head)
    if not sep:
        return line
    line.append("armed", style="ap.amber")
    if tail.startswith(" · "):
        line.append(" · ", style="ap.dim")
        line.append(tail[3:], style="ap.dim")
    else:
        line.append(tail)
    return line


def welcome_lines(
    states: list[AgentState], version: str, cwd: str, light: bool = False
) -> list[Text]:
    lines = header_lines(version, cwd, light)
    lines += [Text(""), summary_text(states), Text("")]
    for cmd, desc in COMMANDS:
        line = Text(INDENT)
        line.append("› ", style="ap.dim")
        line.append("agentprint ")
        line.append(f"{cmd:<10}".rstrip(), style="bold")
        line.append(" " * (10 - len(cmd)))
        line.append(desc, style="ap.dim")
        lines.append(line)
    return lines


def seen_welcome(home: Path) -> bool:
    return (home / STATE_DIR / "welcomed").exists()


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
