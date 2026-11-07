"""Fitting text to the terminal: word wrap with a hanging indent, and a middle "…" for paths.

Lines are built to fit the terminal before they are printed, so prose never folds back to
column 0 mid-word. Path lines are the exception: callers print those whole (soft_wrap), so a
path can still be copied in one piece.
"""

from __future__ import annotations

import re

from rich.cells import cell_len
from rich.text import Text

MIN_ROOM = 16  # never wrap into fewer columns than this; a tinier terminal folds the rest

# A word and the spaces after it. A masked key stub ("sk-a…(len 105)", see secrets.redact) is
# one word, so it never splits across lines.
_WORD = re.compile(r"(?:\S{1,4}…\(len \d+\)|\S+)\s*")


def _breaks(plain: str, room: int) -> list[int]:
    """Where to start new lines so each fits `room` columns.

    Text joined with " · " breaks after a "·" first, so each part ("2 critical", "keys
    masked") stays whole; a part too wide on its own wraps word by word, and a word too wide
    on its own is cut.
    """
    parts: list[list[tuple[int, int]]] = [[]]
    for m in _WORD.finditer(plain):
        parts[-1].append(m.span())
        if m.group().rstrip() == "·":
            parts.append([])

    def size(start: int, end: int) -> int:
        return cell_len(plain[start:end].rstrip())

    breaks: list[int] = []
    col = 0  # where the next word starts on the current line
    for part in filter(None, parts):
        start, end = part[0][0], part[-1][1]
        if col + size(start, end) <= room:
            col += cell_len(plain[start:end])
            continue
        if size(start, end) <= room:
            breaks.append(start)
            col = cell_len(plain[start:end])
            continue
        for w_start, w_end in part:  # the part is too wide on its own: word by word
            if col and col + size(w_start, w_end) > room:
                breaks.append(w_start)
                col = 0
            if size(w_start, w_end) > room:  # at a line start: cut the word
                cuts = list(range(w_start + room, w_start + size(w_start, w_end), room))
                breaks += cuts
                col = cell_len(plain[cuts[-1] : w_end])
            else:
                col += cell_len(plain[w_start:w_end])
    return breaks


def wrap(body: Text, room: int | None) -> list[Text]:
    """`body` wrapped into lines of at most `room` columns (None: one line)."""
    if room is None or body.cell_len <= room:
        return [body.copy()]
    lines = list(body.divide(_breaks(body.plain, max(room, MIN_ROOM))))
    for line in lines:
        line.rstrip()
    return lines


def hang(lead: Text | str, body: Text | str, width: int | None) -> list[Text]:
    """`lead` then `body`, wrapped to `width` columns; every further line starts under the
    first character of `body`. With width None, one line."""
    lead = Text(lead) if isinstance(lead, str) else lead
    body = Text(body) if isinstance(body, str) else body
    room = None if width is None else width - lead.cell_len
    out = []
    for i, piece in enumerate(wrap(body, room)):
        line = lead.copy() if i == 0 else Text(" " * lead.cell_len)
        line.append_text(piece)
        out.append(line)
    return out


def fit_middle(text: str, room: int) -> str:
    """`text` shortened to `room` characters with a "…" in the middle, if it is longer. Two
    thirds of what is kept is the end: for a path, that is the folder's own name."""
    if len(text) <= room:
        return text
    if room < 2:
        return "…"
    keep = room - 1
    head = keep // 3
    return text[:head] + "…" + text[len(text) - (keep - head) :]
