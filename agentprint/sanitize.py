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


def clean(text: str, limit: int | None = MAX_LEN) -> str:
    """Strip escape sequences and invisible characters, then cap the length (prose only: never
    use this on a fix step or anything that may hold a command; see `safe_step`)."""
    text = _ESCAPES.sub("", text)
    text = "".join(
        " " if ch in _AS_SPACE else ch
        for ch in text
        if ch in _AS_SPACE or unicodedata.category(ch) not in _DROP
    )
    if limit is not None and len(text) > limit:
        head = min(_HEAD, limit // 2)
        text = text[:head] + "…" + text[-(limit - head - 1) :]
    return text


# Shown instead of a fix step that can't be shown whole and unchanged. Plain words, no command.
SAFE_STEP = (
    "The exact fix is too long or holds unusual characters, so agentprint does not show it (a "
    "shortened or altered command could run part of a file name). Fix the files named in where "
    "by hand, one at a time, as the finding title describes."
)


def safe_step(text: str, limit: int = FIX_LEN) -> str:
    """A fix step exactly as written, or SAFE_STEP. Never shortened, never edited: cutting or
    cleaning a shell-quoted command can break its quoting and run part of a hostile file name."""
    if len(text) <= limit and clean(text, None) == text:
        return text
    return SAFE_STEP


def quote(text: str) -> str:
    """`clean()` text rendered as a one-line JSON string: unmistakably a quoted value."""
    return json.dumps(clean(text), ensure_ascii=False)


def safe_text(text: str, limit: int | None = MAX_LEN) -> str:
    """Text a person or a repository wrote into a file agentprint reads (a config's key names,
    values and ignore reasons; a baseline's paths and reasons), ready to show or store: every
    key in it cut to its redact() stub, then `clean()`ed. Masked before cleaning (a key hidden
    by invisible characters) and after (a key an escape sequence cut in two), and only then
    shortened, so the cut never leaves part of a key behind."""
    from .secrets import mask_keys  # the key rules are only compiled when there is such text

    return clean(mask_keys(clean(mask_keys(text), None)), limit)


def safe_quote(text: str) -> str:
    """`safe_text()` as a one-line JSON string: a quoted value with no key in it."""
    return json.dumps(safe_text(text), ensure_ascii=False)
