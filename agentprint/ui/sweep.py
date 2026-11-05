"""The welcome sweep: scrambled glyphs settle behind a red edge, left to right."""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

from rich.console import Console
from rich.live import Live
from rich.text import Text

GLYPHS = "▓▒░#%*+=/|~"
EDGE = 3


def _noise(line: Text, start: int, stop: int, style: str, rng: random.Random) -> Text:
    """Columns start..stop of `line`, each non-space character replaced by a random glyph."""
    out = Text(no_wrap=True)
    for ch in line.plain[start:stop]:
        out.append(ch if ch == " " else rng.choice(GLYPHS), style=None if ch == " " else style)
    return out


def _frame_text(lines: list[Text], width: int, edge: int, rng: random.Random) -> Text:
    """One frame: columns below edge-EDGE are final, the next EDGE are red, the rest noise."""
    out = []
    for line in lines:
        settled = max(0, min(edge - EDGE, len(line.plain)))
        shown = max(0, min(edge, len(line.plain)))
        row = Text(no_wrap=True, overflow="crop")
        row.append_text(line[:settled])
        row.append_text(_noise(line, settled, shown, "ap.red", rng))
        row.append_text(_noise(line, shown, width, "ap.faint", rng))
        out.append(row)
    joined = Text("\n", no_wrap=True, overflow="crop").join(out)
    return joined


def frames(lines: list[Text], steps: int = 30, *, rng: random.Random) -> Iterator[Text]:
    """Yield `steps` frames, each one Text (lines joined by newlines). The last is final."""
    width = max((len(line.plain) for line in lines), default=0)
    for i in range(steps):
        edge = round((i + 1) * (width + EDGE) / steps)
        yield _frame_text(lines, width, edge, rng)


def play(console: Console, lines: list[Text], animate: bool, interval: float = 0.04) -> None:
    """Play the sweep when animating, else print the final lines once."""
    if not animate:
        for line in lines:
            console.print(line, soft_wrap=True)
        return
    rng = random.Random()
    with Live(console=console, transient=False, auto_refresh=False) as live:
        for frame in frames(lines, rng=rng):
            live.update(frame, refresh=True)
            time.sleep(interval)
