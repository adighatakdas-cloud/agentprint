"""Secret detection by pattern. Matches are never validated against any service.

`find_secrets` reads text as it is. `find_secrets_deep` also finds keys hidden on purpose: cut
by invisible characters, split into joined string literals, folded over lines (backslash
continuations, YAML block scalars) or base64-encoded. See `find_hidden` for how, and its limits.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import islice


@dataclass(frozen=True)
class SecretRule:
    id: str
    name: str
    pattern: re.Pattern[str]
    group: int = 0
    min_entropy: float = 0.0


@dataclass(frozen=True)
class SecretMatch:
    rule_id: str
    name: str
    value: str
    start: int
    end: int
    # How the key was hidden ("invisible", "split", "folded", "base64"); None for a key written
    # out plainly. For a hidden key, start/end span the text it was hidden in.
    obfuscation: str | None = None


# Specific rules come first; the generic rule only reports text no specific rule claimed.
RULES: tuple[SecretRule, ...] = (
    SecretRule(
        "anthropic",
        "Anthropic API key",
        re.compile(r"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{80,120}"),
    ),
    SecretRule(
        "openai",
        "OpenAI API key",
        re.compile(
            r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,200}"
            r"|sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"
        ),
    ),
    SecretRule(
        "github",
        "GitHub token",
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82}"),
    ),
    SecretRule("aws", "AWS access key ID", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    SecretRule("stripe", "Stripe live key", re.compile(r"(?:sk|rk)_live_[A-Za-z0-9]{24,99}")),
    SecretRule("slack", "Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,72}")),
    SecretRule("google", "Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    # Generic rules: a secret-ish name, a real separator, then a random-looking value.
    # Possessive quantifiers (*+) and bounded spacing keep matching linear on hostile input.
    SecretRule(
        "generic",
        "Generic secret",
        re.compile(
            r"(?i)(?<![a-z0-9])(?:api[_-]?key|secret|token|passw(?:or)?d)[\w-]{0,40}+"
            r"\\?[\"']?[ \t]{0,4}[:=][ \t]{0,4}\\?[\"']?([A-Za-z0-9_\-.+]{20,200})"
        ),
        group=1,
        min_entropy=3.5,
    ),
    SecretRule(
        "generic",
        "Generic secret",
        re.compile(r"(?i)(?<![a-z0-9])bearer[ \t]{1,4}([A-Za-z0-9_\-.+]{20,200})"),
        group=1,
        min_entropy=3.5,
    ),
)


def shannon_entropy(s: str) -> float:
    """Bits of randomness per character: ~0 for 'aaaa', 4+ for random keys."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _looks_random(value: str, min_entropy: float) -> bool:
    has_digit = any(ch.isdigit() for ch in value)
    has_alpha = any(ch.isalpha() for ch in value)
    return has_digit and has_alpha and shannon_entropy(value) >= min_entropy


def _matches(
    text: str, rules: tuple[SecretRule, ...] = RULES
) -> list[tuple[SecretMatch, int, int]]:
    """Every key in `text`, with the span of the whole rule match (name and separator
    included for the generic rules), in text order."""
    found: list[tuple[SecretMatch, int, int]] = []
    for rule in rules:
        for m in rule.pattern.finditer(text):
            start, end = m.span(rule.group)
            if any(start < f.end and f.start < end for f, _, _ in found):
                continue
            value = m.group(rule.group)
            if rule.min_entropy and not _looks_random(value, rule.min_entropy):
                continue
            found.append((SecretMatch(rule.id, rule.name, value, start, end), m.start(), m.end()))
    return sorted(found, key=lambda f: f[0].start)


def find_secrets(text: str) -> list[SecretMatch]:
    return [match for match, _, _ in _matches(text)]


def redact(value: str) -> str:
    return f"{value[:4]}…(len {len(value)})"


# ---- keys hidden on purpose -------------------------------------------------------------------
#
# A key can be written so that no plain pattern sees it: cut by invisible characters, split into
# string literals joined by an operator, folded over lines (backslash continuations, YAML block
# scalars), or base64-encoded. `find_hidden` undoes each trick on a copy of the text (a "view")
# and runs the same rules over it. A view drops the characters that hide a key (the "joins") and
# keeps a map back to the text, so a hidden key is placed at its first character. Only matches
# that span a join are reported: anything else was plain text all along.
#
# The same tricks written inside a JSON string (a transcript line) are escaped (\u200b, \", \n);
# such a string is decoded and read again. Every step is linear in the text and capped below.

# How each way of hiding a key is named in a finding: "<rule>: <redacted> (hidden: <words>)".
HIDDEN: dict[str, str] = {
    "invisible": "invisible characters",
    "split": "split string",
    "folded": "folded lines",
    "base64": "base64",
}
HIDDEN_MARKER = " (hidden: "

# Limits. Each bounds the work on hostile input; past one, keys hidden further on in that text
# are not found (the scan fails open, as everywhere else).
MAX_JOINS = 10_000  # joins collected per trick per text
MAX_BLOCKS = 10_000  # YAML block scalars read per text
MAX_INDENT = 256  # YAML blocks indented deeper are not read
CONTEXT = 320  # text kept on each side of a join; the longest rule match is about 260
MAX_WINDOW = 64 * 1024  # text one view covers at most (views overlap at their edges)
MAX_VIEW_CHARS = 1024 * 1024  # view text searched per text
MAX_BLOBS = 256  # base64 blobs decoded per file
MAX_BLOB_CHARS = 64 * 1024  # base64 characters decoded per blob (at most 48 KiB of text)
MAX_RUNS = 65_536  # base64-looking runs looked at per text
MAX_STRINGS = 4096  # escaped JSON strings looked at per file
DEEP_CHUNK_CHARS = 8 * 1024 * 1024  # a file's text is read for hidden keys in chunks this size


def hidden_note(match: SecretMatch) -> str:
    """' (hidden: split string)' and the like for a hidden key; '' for a plain one."""
    return f"{HIDDEN_MARKER}{HIDDEN[match.obfuscation]})" if match.obfuscation else ""


def hidden_how(detail: str) -> str | None:
    """How a finding's key was hidden ('split string', ...), read from its detail; None for a
    key written out plainly."""
    _, marker, rest = detail.partition(HIDDEN_MARKER)
    how = rest.partition(")")[0]
    return how if marker and how in HIDDEN.values() else None


@dataclass
class Budget:
    """What one file may still spend on the costliest steps, shared by all of its chunks."""

    blobs: int = field(default_factory=lambda: MAX_BLOBS)
    strings: int = field(default_factory=lambda: MAX_STRINGS)


Join = tuple[int, int, str]  # text[start:end] is dropped from the view; the trick's name

# The patterns that scan a whole text start with a fixed character where they can, so the regex
# engine skips ahead to it: a leading lookbehind or a top-level alternation makes it try every
# position, several times slower.

# Invisible format characters: soft hyphen, Mongolian vowel separator, zero-width space,
# non-joiner and joiner, left-to-right and right-to-left marks, bidi embeddings and isolates,
# word joiner and invisible operators, byte-order mark (zero-width no-break space).
_INVISIBLE_CHARS = (
    "\u00ad\u180e\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff"
)
_INVISIBLE = re.compile(f"[{_INVISIBLE_CHARS}]+")

# Quoted literals, each on one line, joined by +, . or .. or simply written next to each other.
# A joint may break the line around an operator, or before an indented literal (Python's
# implicit concatenation inside parentheses); two literals on lines of their own are not joined.
_LITERAL = r""""[^"\\\r\n]*+"|'[^'\\\r\n]*+'"""
_JOINT = (
    r"(?:[ \t]*+(?:\+|\.\.?+)?+[ \t]*+"
    r"|[ \t\r\n]*+(?:\+|\.\.?+)[ \t\r\n]*+"
    r"|[ \t]*+\r?\n[ \t]++)"
)
_JOINED = rf"(?:{_JOINT}(?:{_LITERAL}))++"
_CHAINS = (
    re.compile(rf'"[^"\\\r\n]*+"{_JOINED}'),
    re.compile(rf"'[^'\\\r\n]*+'{_JOINED}"),
)
_PIECE = re.compile(_LITERAL)

_CONTINUATION = re.compile(r"\\\r?\n[ \t]*+")

# The end of a YAML block scalar header ("key: |", "key: >-", "- |2"), with an optional comment;
# what comes before the indicator is checked in _header_indent. "<br>" and the like never match.
_BLOCK_HEADERS = (
    re.compile(r"\|(?<=[ \t]\|)[1-9+-]{0,2}+[ \t\r]*+(?:#[^\n]*+)?$", re.M),
    re.compile(r">(?<=[ \t]>)[1-9+-]{0,2}+[ \t\r]*+(?:#[^\n]*+)?$", re.M),
)
_NONBLANK = re.compile(r"[^ \t\r\n]")
_FOLD = re.compile(r"[ \t\r]*+\n[ \t\r\n]*+")

# Base64 tokens are found on a mask of the text: each base64 character becomes "A", anything
# else ".", so a token is a run of "A" that bytes.find locates without a per-character loop.
_BASE64_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
_BASE64_MASK = bytes(ord("A") if b in _BASE64_CHARS else ord(".") for b in range(256))
_MIN_BASE64 = 40
_NOT_LEAD = re.compile(r"[^A-Za-f]")  # base64 of ASCII text starts each 4 characters with A-f
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_CONTROL_BYTES = bytes(b for b in range(32) if b not in b"\t\n\r") + b"\x7f"

# A double-quoted string with at least one backslash escape (JSON, and YAML and TOML too).
_ESCAPED_STRING = re.compile(r'"[^"\\\n]*+(?:\\.[^"\\\n]*+)++"')
# The tricks above as they look inside such a string: an escaped invisible character, an escaped
# backslash-newline, escaped quotes joined by an operator or a space, the escaped newline that
# ends a YAML block header.
_JSON_HINT = re.compile(
    r"\\(?:u(?:00[aA][dD]|180[eE]|200[b-fB-F]|202[a-eA-E]|206[0-46-9]|[fF][eE][fF][fF])"
    r"|\\\\n"
    r'|"[ \t]*+\+[ \t]*+\\"|"[ \t]++(?:\.\.?+[ \t]++)?+\\"'
    r"|n(?:(?<=[:-] [|>]\\n)|(?<=[:-] [|>][1-9+-]\\n)))"
)


def _invisible(text: str) -> Iterator[Join]:
    """Each run of invisible characters, in text order. Found with str.find per character: a
    character-class regex over the whole text is several times slower on non-ASCII text."""
    if text.isascii():
        return
    joins: list[Join] = []
    for ch in _INVISIBLE_CHARS:
        at = text.find(ch)
        for _ in range(MAX_JOINS):
            if at < 0:
                break
            end = _INVISIBLE.match(text, at).end()  # type: ignore[union-attr]
            joins.append((at, end, "invisible"))
            at = text.find(ch, end)
    yield from sorted(joins)


def _split(text: str) -> Iterator[Join]:
    """Each joint between two joined literals: closing quote, operator, opening quote. A chain
    that starts with the other kind of quote may be found twice; its joints merge."""
    for chains in _CHAINS:
        for chain in chains.finditer(text):
            previous = None
            for piece in _PIECE.finditer(text, chain.start(), chain.end()):
                if previous is not None:
                    yield previous.end() - 1, piece.start() + 1, "split"
                previous = piece


def _continued(text: str) -> Iterator[Join]:
    for m in _CONTINUATION.finditer(text):
        yield m.start(), m.end(), "folded"


@lru_cache(maxsize=64)
def _block_body(indent: int) -> re.Pattern[str]:
    """The lines of a block: blank, or indented deeper than its header."""
    deeper = f"[ ]{{{indent + 1}}}"
    return re.compile(rf"(?:{deeper}[^\n]*+\n|[ \t\r]*+\n)*+(?:{deeper}[^\n]++)?")


def _header_indent(text: str, indicator: int) -> int | None:
    """The indentation of a YAML block header line whose indicator is at `indicator` (the line
    is 'key: |' or '- |'), or None if the line is something else."""
    line_start = text.rfind("\n", 0, indicator) + 1
    lead = text[line_start:indicator]
    name = lead.rstrip(" \t")
    if len(name) == len(lead) or not (name.endswith(":") or name.lstrip(" ") == "-"):
        return None
    return len(lead) - len(lead.lstrip(" "))


def _folded(text: str) -> Iterator[Join]:
    """In each YAML block scalar: the header's indicator up to the first content character (so
    'key: |' joins its value), then every line break with the indentation around it."""
    pending = [p.search(text) for p in _BLOCK_HEADERS]
    pos = 0
    for _ in range(MAX_BLOCKS):
        for i, header in enumerate(pending):
            if header is not None and header.start() < pos:
                pending[i] = _BLOCK_HEADERS[i].search(text, pos)
        found = [h for h in pending if h is not None]
        if not found:
            return
        header = min(found, key=lambda h: h.start())
        pos = header.end()
        indent = _header_indent(text, header.start())
        if indent is None or indent > MAX_INDENT or pos >= len(text):
            continue
        body_start = pos + 1
        body_end = _block_body(indent).match(text, body_start).end()  # type: ignore[union-attr]
        pos = max(pos, body_end)
        first = _NONBLANK.search(text, body_start, body_end)
        if first is None:
            continue
        content_end = body_start + len(text[body_start:body_end].rstrip())
        yield header.start(), first.start(), "folded"
        for fold in _FOLD.finditer(text, first.start(), content_end):
            yield fold.start(), fold.end(), "folded"


_COLLECTORS = (_invisible, _split, _continued, _folded)


def _windows(text: str, joins: list[Join]) -> Iterator[tuple[int, int, list[Join]]]:
    """Joins in text order, overlapping ones merged, grouped into windows of text that hold
    them with CONTEXT characters on each side. A window stops growing at MAX_WINDOW; the next
    one reaches 2 * CONTEXT back and takes the joins there again, so a key that spans the
    boundary is whole in it."""
    group: list[Join] = []
    start = end = 0
    for j_start, j_end, kind in joins:
        if group and j_start < group[-1][1]:  # overlaps the previous join
            if j_end > group[-1][1]:
                group[-1] = (group[-1][0], j_end, group[-1][2])
                end = max(end, min(len(text), j_end + CONTEXT))
            continue
        near = bool(group) and j_start - CONTEXT <= end
        if near and end - start < MAX_WINDOW:
            group.append((j_start, j_end, kind))
            end = max(end, min(len(text), j_end + CONTEXT))
            continue
        if group:
            yield start, end, group
        start = max(0, j_start - 2 * CONTEXT if near else j_start - CONTEXT)
        carried = [j for j in group if j[1] > start] if near else []
        if carried:
            start = min(start, carried[0][0])
        group = [*carried, (j_start, j_end, kind)]
        end = min(len(text), j_end + CONTEXT)
    if group:
        yield start, end, group


# Every generic match holds one of these words; a view without any skips the (slow) generic rules.
_GENERIC_WORDS = ("key", "secret", "token", "passw", "bearer")
_SPECIFIC_RULES = tuple(rule for rule in RULES if rule.id != "generic")


@dataclass
class _View:
    """text[start:end] without its joins, and the way back to the text."""

    text: str
    view_at: list[int]  # where each kept piece starts in the view ...
    text_at: list[int]  # ... and in the text
    cuts: list[int]  # where each join was, in the view
    kinds: list[str]

    def in_text(self, i: int) -> int:
        k = bisect_right(self.view_at, i) - 1
        return self.text_at[k] + i - self.view_at[k]


def _view(text: str, start: int, end: int, joins: list[Join]) -> _View:
    view = _View("", [], [], [], [])
    parts: list[str] = []
    pos = start
    length = 0
    for j_start, j_end, kind in joins:
        view.view_at.append(length)
        view.text_at.append(pos)
        parts.append(text[pos:j_start])
        length += j_start - pos
        view.cuts.append(length)
        view.kinds.append(kind)
        pos = j_end
    view.view_at.append(length)
    view.text_at.append(pos)
    parts.append(text[pos:end])
    view.text = "".join(parts)
    return view


def _in_view(view: _View) -> Iterator[SecretMatch]:
    """Keys in a view that span at least one join."""
    lower = view.text.lower()
    rules = RULES if any(word in lower for word in _GENERIC_WORDS) else _SPECIFIC_RULES
    cuts = view.cuts
    for match, full_start, full_end in _matches(view.text, rules):
        k = bisect_right(cuts, full_start)
        if k == len(cuts) or cuts[k] >= full_end:
            continue  # no join inside: plain text, which find_secrets reads
        inside = bisect_right(cuts, match.start)
        if inside < len(cuts) and cuts[inside] < match.end:
            k = inside  # name the trick that cut the key itself
        yield SecretMatch(
            match.rule_id,
            match.name,
            match.value,
            view.in_text(match.start),
            view.in_text(match.end - 1) + 1,
            view.kinds[k],
        )


def _joined(text: str) -> Iterator[SecretMatch]:
    joins: list[Join] = []
    for collect in _COLLECTORS:
        joins += islice(collect(text), MAX_JOINS)
    joins.sort()
    room = MAX_VIEW_CHARS
    for start, end, group in _windows(text, joins):
        view = _view(text, start, end, group)
        room -= len(view.text)
        if room < 0:
            return
        yield from _in_view(view)


def _plausible(head: str) -> bool:
    """Could this be base64 of text? Each 4-character group of base64 of ASCII starts with A-f,
    and text encodes to both cases. Paths, hex hashes and plain words fail fast."""
    leads = head[:256:4]
    return (
        len(_NOT_LEAD.findall(leads)) * 8 <= len(leads)
        and _UPPER.search(head) is not None
        and _LOWER.search(head) is not None
    )


def _decode(head: str) -> str | None:
    """The text a base64 token encodes (its first MAX_BLOB_CHARS characters), or None if it is
    not valid base64 or decodes to binary."""
    body = head.rstrip("=")[:MAX_BLOB_CHARS]
    if len(body) % 4 == 1:
        body = body[:-1]
    try:
        data = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    controls = len(data) - len(data.translate(None, _CONTROL_BYTES))
    if not data or controls * 50 > len(data):
        return None
    return data.decode("utf-8", errors="replace")


def _base64_tokens(text: str) -> Iterator[tuple[int, int]]:
    """(start, end) of each whole base64 token of at least _MIN_BASE64 characters (padding
    included). A token may follow a JSON escape such as \\n; other escapes rule it out."""
    mask = text.encode("ascii", "replace").translate(_BASE64_MASK)
    run = b"A" * _MIN_BASE64
    pos = 0
    for _ in range(MAX_RUNS):
        start = mask.find(run, pos)
        if start < 0:
            return
        end = mask.find(b".", start)
        pos = end = len(mask) if end < 0 else end
        if start and text[start - 1] == "\\":
            if text[start] not in "nrt":
                continue
            start += 1
        pad = len(text[end : end + 3]) - len(text[end : end + 3].lstrip("="))
        if pad < 3 and end - start >= _MIN_BASE64:
            yield start, end + pad


def _in_base64(text: str, budget: Budget) -> Iterator[SecretMatch]:
    for start, end in _base64_tokens(text):
        if budget.blobs <= 0:
            return
        head = text[start : min(end, start + MAX_BLOB_CHARS + 2)]
        if not _plausible(head):
            continue
        budget.blobs -= 1
        decoded = _decode(head)
        if decoded is None:
            continue
        for found in find_secrets(decoded):
            yield SecretMatch(found.rule_id, found.name, found.value, start, end, "base64")


def _string_around(text: str, at: int, floor: int) -> tuple[int, int] | None:
    """The double-quoted string that holds position `at`: back to the nearest quote that is not
    escaped (inside a JSON string every quote is), then forward. Never looks before `floor`."""
    floor = max(floor, text.rfind("\n", floor, at) + 1)
    quote = at
    while True:
        quote = text.rfind('"', floor, quote)
        if quote < 0:
            return None
        before = text[max(floor, quote - 64) : quote]
        slashes = len(before) - len(before.rstrip("\\"))
        if slashes == 64:
            return None
        if slashes % 2 == 0:
            break
    m = _ESCAPED_STRING.match(text, quote)
    return m.span() if m is not None and m.end() > at else None


def _in_json_strings(text: str, budget: Budget) -> Iterator[tuple[int, int, str]]:
    """(start, end, decoded) for each escaped string that shows a hint of a trick."""
    done = 0
    while budget.strings > 0:
        hint = _JSON_HINT.search(text, done)
        if hint is None:
            return
        budget.strings -= 1
        span = _string_around(text, hint.start(), done)
        done = max(hint.end(), span[1] if span else 0)
        if span is None:
            continue
        try:
            decoded = json.loads(text[span[0] : span[1]])
        except (ValueError, RecursionError):
            continue
        if isinstance(decoded, str):
            yield span[0], span[1], decoded


def _hidden_in(text: str, budget: Budget, nested: bool = False) -> Iterator[SecretMatch]:
    yield from _joined(text)
    if nested:
        return
    yield from _in_base64(text, budget)
    for start, end, decoded in _in_json_strings(text, budget):
        for found in _hidden_in(decoded, budget, nested=True):
            yield SecretMatch(found.rule_id, found.name, found.value, start, end, found.obfuscation)


def find_hidden(
    text: str, known: Collection[str] = (), budget: Budget | None = None
) -> list[SecretMatch]:
    """Keys hidden on purpose in `text`, each once, in text order, leaving out `known` (the keys
    already found plainly). A match's `obfuscation` names the trick. Its start and end span the
    key's characters in the text (joins included), or the whole base64 token or JSON string it
    was found in."""
    seen = set(known)
    found: list[SecretMatch] = []
    for match in sorted(_hidden_in(text, budget or Budget()), key=lambda m: m.start):
        if match.value not in seen:
            seen.add(match.value)
            found.append(match)
    return found


def find_secrets_deep(text: str) -> list[SecretMatch]:
    """find_secrets, plus the keys find_hidden finds that are not also written out plainly."""
    direct = find_secrets(text)
    hidden = find_hidden(text, {m.value for m in direct})
    return sorted([*direct, *hidden], key=lambda m: m.start)
