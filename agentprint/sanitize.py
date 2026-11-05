"""Make text taken from scanned files safe to show to a human terminal or an AI agent.

Paths, archive member names and file names come from whatever is being scanned, so a hostile
package can choose them. Before any of it is printed it goes through `clean()` (no escape
sequences, no control or invisible characters, bounded length) and, in agent-facing output,
`quote()` (a one-line JSON string), so it reads as data and cannot pose as instructions.
"""

from __future__ import annotations

import json
import re
import unicodedata

MAX_LEN = 300
FIX_LEN = 1200  # fix hints and steps: agentprint's own words, but they can embed scanned paths
_HEAD = 120

# Terminal escape sequences, removed whole so no "[2J"-style residue is left behind.
_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]{0,512}(?:\x07|\x1b\\)?"  # OSC: window titles, hyperlinks
    r"|(?:\x1b\[|\x9b)[0-?]{0,64}[ -/]{0,8}[@-~]"  # CSI: colours, cursor moves, clears
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
)
# Control, format (bidi overrides, zero-width), surrogate, private-use, line/para separators.
_DROP = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}
_AS_SPACE = {"\t", "\n", "\r", "\v", "\f", " ", " ", "\x85"}


def clean(text: str, limit: int = MAX_LEN) -> str:
    """Strip escape sequences and invisible characters, then cap the length."""
    text = _ESCAPES.sub("", text)
    text = "".join(
        " " if ch in _AS_SPACE else ch
        for ch in text
        if ch in _AS_SPACE or unicodedata.category(ch) not in _DROP
    )
    if len(text) > limit:
        head = min(_HEAD, limit // 2)
        text = text[:head] + "…" + text[-(limit - head - 1) :]
    return text


def quote(text: str) -> str:
    """`clean()` text rendered as a one-line JSON string: unmistakably a quoted value."""
    return json.dumps(clean(text), ensure_ascii=False)
