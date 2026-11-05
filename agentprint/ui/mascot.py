"""The agentprint mascot: a small red agent in an amber targeting frame."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text

from agentprint.ui.theme import HEX, LIGHT_HEX

# A = agent body (red), R = frame and the dot between the eyes (amber), . = empty.
FULL: tuple[str, ...] = (
    "RRR...A...RRR",
    "R.....A.....R",
    "R...AAAAA...R",
    "...AAAAAAA...",
    "...AA.R.AA...",
    "...AAAAAAA...",
    "...A.AAA.A...",
    "....AAAAA....",
    "R...........R",
    "R...........R",
    "RRR.......RRR",
)

SMALL: tuple[str, ...] = (
    "...A...",
    "...A...",
    ".AAAAA.",
    "AAAAAAA",
    "AA.R.AA",
    "AAAAAAA",
    "A.AAA.A",
    ".AAAAA.",
)

_COLOR = {"A": "red", "R": "amber"}


def half_blocks(rows: Sequence[str], light: bool = False) -> list[Text]:
    """Draw pixel rows two per terminal line using half-block characters."""
    hx = LIGHT_HEX if light else HEX
    width = max((len(r) for r in rows), default=0)
    padded = [r.ljust(width, ".") for r in rows]
    if len(padded) % 2:
        padded.append("." * width)
    lines: list[Text] = []
    for top_row, bottom_row in zip(padded[::2], padded[1::2], strict=True):
        line = Text(no_wrap=True)
        for t, b in zip(top_row, bottom_row, strict=True):
            top, bottom = _COLOR.get(t), _COLOR.get(b)
            if top is None and bottom is None:
                line.append(" ")
            elif top == bottom:
                line.append("█", style=hx[top])  # type: ignore[index]
            elif bottom is None:
                line.append("▀", style=hx[top])  # type: ignore[index]
            elif top is None:
                line.append("▄", style=hx[bottom])
            else:
                line.append("▀", style=f"{hx[top]} on {hx[bottom]}")
        lines.append(line)
    return lines
