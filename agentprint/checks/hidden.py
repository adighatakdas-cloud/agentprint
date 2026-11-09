"""Hidden characters in text an AI agent reads (AP010): characters a person does not see.

What is looked for: the invisible format characters the hidden-key search uses
(secrets.INVISIBLE_CHARS), the Arabic letter mark, the deprecated format characters
U+206A-206F, the combining grapheme joiner, the blank fillers (Hangul U+115F, U+1160, U+3164,
U+FFA0; braille U+2800), Unicode tag characters, and variation selectors.

Ruling X16, how strong a find is:
- high: Unicode tags (text an AI reads and a person cannot see), bidi overrides, bidi isolates
  and embeddings in left-to-right text, deprecated format characters, runs of four or more
  variation selectors, and runs of two or more zero-width or blank characters (or more than
  MAX_LONE single ones in a file: enough to carry text one bit at a time);
- low: a single stray zero-width character, a single left-to-right or right-to-left mark in
  left-to-right text, a soft hyphen outside a word, a variation selector inside an ASCII word.
  One character hides no text; it is still worth removing.
- not counted: one byte-order mark at the start of a file; a zero-width joiner inside an emoji
  sequence, or a joiner between letters of a script that uses them (Hebrew, Arabic, Persian, the
  Indic scripts); a zero-width space inside Thai, Lao, Khmer or Myanmar text (they write no
  spaces between words); bidi marks, embeddings and isolates in a paragraph that has
  right-to-left letters; a soft hyphen inside a word; the tag characters of an emoji flag
  (England, Scotland, Wales); a Mongolian vowel separator in Mongolian; a braille blank in
  braille art; a combining grapheme joiner next to a combining mark.

Linear in the text: each paragraph's right-to-left letters are looked up once, flags by bisect,
and at most MAX_HIDDEN characters are looked at.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field

from ..secrets import INVISIBLE_CHARS

MAX_HIDDEN = 4096  # hidden characters looked at per file; past that the count is "at least"
MAX_LONE = 16  # single zero-width characters a file may hold before they count as text

_MORE = "\u061c\u206a-\u206f\u034f\u115f\u1160\u3164\uffa0\u2800"
_TAGS = "\U000e0000-\U000e007f"
_SELECTORS = "\ufe00-\ufe0f\U000e0100-\U000e01ef"
# Every character looked at, and runs of them (a run of zero-width characters can carry text).
_FLAGGED = re.compile(f"[{INVISIBLE_CHARS}{_MORE}{_TAGS}]+")
_SELECTORS_RUN = re.compile(f"[{_SELECTORS}]+")
# An emoji tag sequence (the England, Scotland and Wales flags): black flag, tags, cancel tag.
_FLAG = re.compile("\U0001f3f4[\U000e0020-\U000e007e]+\U000e007f")
_PARAGRAPH = re.compile(r"\n[ \t]*\n")

_OVERRIDES = frozenset("\u202d\u202e")  # reorder what a person sees
_ISOLATES = frozenset("\u202a\u202b\u202c\u2066\u2067\u2068\u2069")  # embeddings and isolates
_MARKS = frozenset("\u200e\u200f\u061c")  # left-to-right, right-to-left, Arabic letter mark
_DEPRECATED = frozenset("\u206a\u206b\u206c\u206d\u206e\u206f")
_RTL = re.compile("[\u0590-\u061b\u061d-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")  # not U+061C
_JOINING = re.compile("[\u0590-\u08ff\u0900-\u0dff\u1000-\u109f\u1780-\u18af\ufb1d-\ufdff]")
_NO_SPACES = re.compile("[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff]")  # Thai, Lao, Myanmar, Khmer
_MODIFIERS = frozenset(chr(c) for c in range(0x1F3FB, 0x1F400)) | {"\ufe0f", "\ufe0e"}

HIGH, LOW = "high", "low"
_KIND_NAMES = {
    "tag": ("Unicode tag character", "Unicode tag characters"),
    "bidi": ("bidi control", "bidi controls"),
    "zero-width": ("zero-width character", "zero-width characters"),
    "blank": ("blank filler character", "blank filler characters"),
    "format": ("deprecated format character", "deprecated format characters"),
    "soft hyphen": ("soft hyphen", "soft hyphens"),
    "selector": ("variation selector", "variation selectors"),
}


@dataclass
class Hidden:
    """What one text holds: where the first counted character is, how many of each kind, and
    whether any of them is strong enough for a high print (ruling X16)."""

    first: int = -1
    counts: dict[str, int] = field(default_factory=dict)
    strong: bool = False
    capped: bool = False

    def add(self, kind: str, at: int, strong: bool, n: int = 1) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + n
        self.first = at if self.first < 0 else min(self.first, at)
        self.strong = self.strong or strong

    def describe(self) -> str:
        parts = []
        for kind, (one, many) in _KIND_NAMES.items():
            n = self.counts.get(kind, 0)
            if n:
                parts.append(f"{n} {one if n == 1 else many}")
        return ("at least " if self.capped else "") + ", ".join(parts)


def _mark(ch: str) -> bool:
    return bool(ch) and unicodedata.category(ch).startswith("M")


def _emoji(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0x1F000 or 0x2190 <= cp <= 0x2BFF or ch in "\u00a9\u00ae\u203c\u2049\u2122"


def _joins_emoji(text: str, at: int) -> bool:
    before = at - 1
    while before >= 0 and at - before < 8 and text[before] in _MODIFIERS:
        before -= 1
    after = at + 1
    return before >= 0 and after < len(text) and _emoji(text[before]) and _emoji(text[after])


def _kind(
    text: str, at: int, rtl: Callable[[int], bool], flag: Callable[[int], bool]
) -> str | None:
    """What the character at `at` is, or None when it is ordinary text there."""
    ch = text[at]
    before = text[at - 1] if at else ""
    after = text[at + 1] if at + 1 < len(text) else ""
    if "\U000e0000" <= ch <= "\U000e007f":
        return None if flag(at) else "tag"
    if ch in _OVERRIDES:
        return "bidi"
    if ch in _ISOLATES or ch in _MARKS:
        return None if rtl(at) else "bidi"
    if ch in _DEPRECATED:
        return "format"
    if ch == "\ufeff" and at == 0:
        return None  # a byte-order mark
    if ch == "\u00ad":
        return None if before.isalpha() and after.isalpha() else "soft hyphen"
    if ch in "\u200c\u200d" and (_JOINING.match(before or " ") or _JOINING.match(after or " ")):
        return None  # Hebrew, Arabic, Persian and Indic text use joiners
    if ch == "\u200d" and _joins_emoji(text, at):
        return None
    if ch == "\u200b" and (_NO_SPACES.match(before or " ") or _NO_SPACES.match(after or " ")):
        return None  # Thai, Lao, Khmer and Myanmar mark word breaks this way
    if ch == "\u180e" and ("\u1800" <= before <= "\u18af" or "\u1800" <= after <= "\u18af"):
        return None  # Mongolian vowel separator in Mongolian text
    if ch == "\u2800":
        braille = "\u2801" <= before <= "\u28ff" or "\u2801" <= after <= "\u28ff"
        return None if braille or before == "\u2800" and after == "\u2800" else "blank"
    if ch in "\u115f\u1160\u3164\uffa0":
        return "blank"
    if ch == "\u034f" and (_mark(before) or _mark(after)):
        return None  # a combining grapheme joiner among combining marks, where it belongs
    return "zero-width"


def hidden_characters(text: str) -> Hidden:
    """The hidden characters in `text`, classified as ruling X16 says."""
    found = Hidden()
    if text.isascii():
        return found
    flags = [m.span() for m in _FLAG.finditer(text)]
    starts = [start for start, _ in flags]
    paragraphs: list[int] | None = None
    rtl_cache: dict[int, bool] = {}

    def flag(at: int) -> bool:
        i = bisect_right(starts, at) - 1
        return i >= 0 and flags[i][0] < at < flags[i][1]

    def rtl(at: int) -> bool:
        nonlocal paragraphs
        if paragraphs is None:
            paragraphs = [m.end() for m in _PARAGRAPH.finditer(text)]
        i = bisect_right(paragraphs, at)
        if i not in rtl_cache:
            start = paragraphs[i - 1] if i else 0
            end = paragraphs[i] if i < len(paragraphs) else len(text)
            rtl_cache[i] = _RTL.search(text, start, end) is not None
        return rtl_cache[i]

    looked = 0
    lone = 0
    for run in _FLAGGED.finditer(text):
        kinds = []
        for at in range(run.start(), run.end()):
            if looked >= MAX_HIDDEN:
                found.capped = True
                break
            looked += 1
            kind = _kind(text, at, rtl, flag)
            if kind is not None:
                kinds.append((at, kind))
        if found.capped and not kinds:
            break
        counted = [k for _, k in kinds if k not in ("tag", "bidi", "format")]
        for at, kind in kinds:
            if kind in ("tag", "format") or text[at] in _OVERRIDES:
                strong = True
            elif kind == "bidi":
                strong = text[at] in _ISOLATES or len(kinds) > 1
            else:
                strong = len(counted) > 1  # a run can carry text, one character cannot
            found.add(kind, at, strong)
        if len(counted) == 1:
            lone += 1
        if found.capped:
            break
    if lone > MAX_LONE:
        found.strong = True
    for n, run in enumerate(_SELECTORS_RUN.finditer(text)):
        if n >= MAX_HIDDEN:
            break
        size = run.end() - run.start()
        if size >= 4:  # one base character takes at most one: a run carries data
            found.add("selector", run.start(), True, size)
        elif size == 1 and _in_word(text, run.start()):
            found.add("selector", run.start(), False)
    return found


def _in_word(text: str, at: int) -> bool:
    """True when the character at `at` sits between two printable ASCII characters."""
    return 0 < at < len(text) - 1 and "!" <= text[at - 1] <= "~" and "!" <= text[at + 1] <= "~"


def strip(text: str) -> str:
    """`text` without the characters looked for here, nor variation selectors inside ASCII
    words, so that no hidden character can split a command word (cu<ZWSP>rl, c<VS15>url)."""
    if text.isascii():
        return text
    text = _FLAGGED.sub("", text)
    return _SELECTORS_RUN.sub(lambda m: "" if _in_word(m.string, m.start()) else m.group(), text)
