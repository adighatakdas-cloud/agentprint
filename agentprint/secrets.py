"""Secret detection by pattern. Matches are never validated against any service.

`find_secrets` reads text as it is. `find_secrets_deep` also finds keys hidden on purpose: cut
by invisible characters, split into joined string literals or by an empty quoted string, folded
over lines (backslash continuations, YAML block scalars) or base64-encoded. See `find_hidden`
for how, and its limits.
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
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import islice


def _fold(text: str) -> str:
    """`text` with case folded the way the rules' `(?i)` names match it. Besides upper and lower
    case, Python's regex engine matches the long s, the Kelvin sign and the dotless i and dotted
    capital I as s, k and i. casefold() turns the first two into s and k; the two i forms are
    replaced first (casefold keeps the dotless i and adds a combining dot to the capital)."""
    return text.replace("\u0130", "i").replace("\u0131", "i").casefold()


@dataclass(frozen=True)
class SecretRule:
    id: str
    name: str
    pattern: re.Pattern[str]
    group: int = 0
    min_entropy: float = 0.0
    # Words, at least one of which must be in the text (case folded by _fold) for the rule to be
    # tried at all. For rules that start with a case-insensitive name, which the regex engine
    # cannot skip to. Folded the same way when RULES is built.
    needs: tuple[str, ...] = ()
    # Exact text, at least one of which must be in the text (the rule's own literal start).
    starts: tuple[str, ...] = ()


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


# A key ends where its characters stop: the next character may not continue it. A lookahead, not
# `\\b`, because `_` and `-` are key characters here and a JSON-escaped `\\n` after a key must
# still end it. Short prefixes (`hf_`, `r8_`) are checked for a word before them after the prefix,
# so the pattern still starts with a fixed text and the engine can skip ahead to it.
_END = r"(?![A-Za-z0-9_\-])"


def _before(prefix: str) -> str:
    """`prefix`, not glued to a word before it (a lookbehind placed after it)."""
    return rf"{prefix}(?<![A-Za-z0-9_\-]{prefix})"


# Named context for the formats too common to match bare: the word, then a value within 80
# characters on the same line.
_NEAR = r"[^\n]{0,80}?"

# Specific rules come first; the generic rule only reports text no specific rule claimed.
# Publishing tokens (npm, PyPI) lead the newer formats: a leak there is a supply-chain event.
_RULES: tuple[SecretRule, ...] = (
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
    SecretRule(
        "slack",
        "Slack token",
        re.compile(r"x(?:ox[baprs]-[A-Za-z0-9-]{10,72}|app-\d-[A-Z0-9]+-\d+-[a-z0-9]+)"),
    ),
    SecretRule("google", "Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    SecretRule(
        "google-oauth",
        "Google OAuth client secret",
        re.compile(rf"GOCSPX-[A-Za-z0-9_\-]{{28}}{_END}"),
    ),
    SecretRule("npm", "npm access token", re.compile(rf"{_before('npm_')}[A-Za-z0-9]{{36}}{_END}")),
    SecretRule(
        "pypi",
        "PyPI upload token",
        re.compile(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,1000}"),
    ),
    SecretRule(
        "gitlab",
        "GitLab token",
        re.compile(
            rf"glpat-(?:[A-Za-z0-9_\-]{{27,300}}\.[0-9a-z]{{9}}|[A-Za-z0-9_\-]{{20}}){_END}"
        ),
    ),
    # Docker's token lengths are not documented by Docker: the looser shape, then a look-alike test.
    SecretRule(
        "dockerhub",
        "Docker Hub access token",
        re.compile(rf"dckr_(?:pat|oat)_[A-Za-z0-9_\-]{{27,48}}{_END}"),
    ),
    SecretRule(
        "huggingface",
        "Hugging Face token",
        re.compile(rf"{_before('hf_')}(?=[0-9]*[A-Za-z])[A-Za-z0-9]{{34}}{_END}"),
    ),
    SecretRule(
        "huggingface",
        "Hugging Face token",
        re.compile(rf"api_org_[A-Za-z]{{34}}{_END}"),
    ),
    SecretRule("groq", "Groq API key", re.compile(rf"{_before('gsk_')}[A-Za-z0-9]{{40,64}}{_END}")),
    SecretRule("openrouter", "OpenRouter API key", re.compile(rf"sk-or-v1-[0-9a-f]{{64}}{_END}")),
    SecretRule("xai", "xAI API key", re.compile(rf"xai-[A-Za-z0-9_]{{80}}{_END}")),
    SecretRule("perplexity", "Perplexity API key", re.compile(rf"pplx-[A-Za-z0-9]{{48}}{_END}")),
    SecretRule(
        "replicate",
        "Replicate API token",
        re.compile(rf"{_before('r8_')}[A-Za-z0-9_\-]{{37}}{_END}"),
    ),
    SecretRule(
        "supabase-pat",
        "Supabase access token",
        re.compile(rf"{_before('sbp_')}[a-f0-9]{{40}}{_END}"),
    ),
    # sb_publishable_ keys are meant to be public and are not matched.
    SecretRule(
        "supabase-secret",
        "Supabase secret key",
        re.compile(r"sb_secret_[A-Za-z0-9_\-]{20,}"),
    ),
    SecretRule(
        "cloudflare",
        "Cloudflare API token",
        re.compile(rf"cf[ua]t_[A-Za-z0-9]{{40}}[a-f0-9]{{8}}{_END}"),
    ),
    SecretRule(
        "digitalocean",
        "DigitalOcean token",
        re.compile(rf"do[por]_v1_[a-f0-9]{{64}}{_END}"),
    ),
    SecretRule("linear", "Linear API key", re.compile(rf"lin_api_[A-Za-z0-9]{{40}}{_END}")),
    SecretRule(
        "notion", "Notion API token", re.compile(rf"ntn_[0-9]{{11}}[A-Za-z0-9]{{35}}{_END}")
    ),
    SecretRule(
        "shopify",
        "Shopify access token",
        re.compile(rf"shp(?:at|ca|pa|ss)_[a-fA-F0-9]{{32}}{_END}"),
    ),
    SecretRule(
        "atlassian",
        "Atlassian API token",
        re.compile(r"ATATT3[A-Za-z0-9_\-=]{186}(?![A-Za-z0-9_\-=])"),
    ),
    SecretRule(
        "postman", "Postman API key", re.compile(rf"PMAK-[a-f0-9]{{24}}-[a-f0-9]{{34}}{_END}")
    ),
    SecretRule(
        "sendgrid",
        "SendGrid API key",
        re.compile(rf"SG\.[A-Za-z0-9_\-]{{22}}\.[A-Za-z0-9_\-]{{43}}{_END}"),
    ),
    SecretRule(
        "doppler",
        "Doppler token",
        re.compile(
            r"dp\.(?:pt|ct|sa|scim|audit|st(?:\.[a-z0-9_\-]{2,35})?)\.[A-Za-z0-9]{40,44}" + _END
        ),
    ),
    SecretRule(
        "onepassword",
        "1Password service account token",
        re.compile(r"ops_eyJ[A-Za-z0-9+/]{250,}+={0,3}"),
    ),
    # Context-only formats: too common as plain text, so a match needs the provider's name
    # on the same line, before the value.
    SecretRule(
        "deepseek",
        "DeepSeek API key",
        re.compile(rf"(?i:deepseek)[\w .\-]{{0,30}}[:=\"'\\ ]{{1,6}}(sk-[a-f0-9]{{32}}){_END}"),
        group=1,
        needs=("deepseek",),
    ),
    SecretRule(
        "twilio",
        "Twilio API key SID",
        re.compile(rf"(?i:twilio){_NEAR}(?<![A-Za-z0-9])(SK[0-9a-f]{{32}})(?![A-Za-z0-9])"),
        group=1,
        needs=("twilio",),
    ),
    SecretRule(
        "discord",
        "Discord bot token",
        re.compile(
            rf"(?i:discord){_NEAR}(?<![A-Za-z0-9_.\-])"
            rf"([MNO][A-Za-z0-9_\-]{{23,25}}\.[A-Za-z0-9_\-]{{6}}\.[A-Za-z0-9_\-]{{27,38}})"
            r"(?![A-Za-z0-9_.\-])"
        ),
        group=1,
        needs=("discord",),
    ),
    SecretRule(
        "telegram",
        "Telegram bot token",
        re.compile(rf"(?i:telegr){_NEAR}(?<![0-9])([0-9]{{8,10}}:AA[A-Za-z0-9_\-]{{33}}){_END}"),
        group=1,
        needs=("telegr",),
    ),
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
        needs=("key", "secret", "token", "passw"),
    ),
    SecretRule(
        "generic",
        "Generic secret",
        re.compile(r"(?i)(?<![a-z0-9])bearer[ \t]{1,4}([A-Za-z0-9_\-.+]{20,200})"),
        group=1,
        min_entropy=3.5,
        needs=("bearer",),
    ),
)

# The text each newer rule starts with. A plain substring test over the text costs a fraction
# of one more regex pass, and most files hold none of them.
_STARTS: dict[str, tuple[str, ...]] = {
    "google-oauth": ("GOCSPX-",),
    "npm": ("npm_",),
    "pypi": ("pypi-AgEIcHlwaS5vcmc",),
    "gitlab": ("glpat-",),
    "dockerhub": ("dckr_pat_", "dckr_oat_"),
    "huggingface": ("hf_", "api_org_"),
    "groq": ("gsk_",),
    "openrouter": ("sk-or-v1-",),
    "xai": ("xai-",),
    "perplexity": ("pplx-",),
    "replicate": ("r8_",),
    "supabase-pat": ("sbp_",),
    "supabase-secret": ("sb_secret_",),
    "cloudflare": ("cfut_", "cfat_"),
    "digitalocean": ("dop_v1_", "doo_v1_", "dor_v1_"),
    "linear": ("lin_api_",),
    "notion": ("ntn_",),
    "shopify": ("shpat_", "shpca_", "shppa_", "shpss_"),
    "atlassian": ("ATATT3",),
    "postman": ("PMAK-",),
    "sendgrid": ("SG.",),
    "doppler": ("dp.pt.", "dp.ct.", "dp.sa.", "dp.st.", "dp.scim.", "dp.audit."),
    "onepassword": ("ops_eyJ",),
}
RULES: tuple[SecretRule, ...] = tuple(
    replace(
        rule,
        starts=_STARTS.get(rule.id, rule.starts),
        needs=tuple(_fold(word) for word in rule.needs),
    )
    for rule in _RULES
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


# Key-shaped placeholders from READMEs and config examples (ghp_ then 36 x's, AKIA then 16 X's,
# a Slack prefix then your-bot-token, the AWS docs' key ID that ends in EXAMPLE) are not keys.
# A match is a placeholder when its body, the part after its literal prefix, is:
#   (a) one character repeated (any case, separators aside), or separators only;
#   (b) or holds a placeholder word as a whole token, an edge or a separator on both sides;
#   (c) or holds a word of six letters or more (sample, example, ...) or a run of eight or more
#       x's that starts or ends the body or touches a separator;
#   (d) or is a placeholder word followed only by one repeated character (dummy0000...).
# These are tight on purpose: every drop is a silent miss, so a random key must almost never
# look like one (a test runs 100,000 random bodies of each rule's shape). "your" or "xxxx" glued
# to random characters does not count. FAKE, the marker in agentprint's own test keys, is not a
# placeholder. A key head followed directly by a template is a placeholder too (see
# _template_after). Output still redacts placeholders (see _matches).

# Where each older rule's key body starts; a newer rule's body starts after its `starts`.
_OLDER_PREFIXES = {
    "anthropic": r"sk-ant-(?:api|admin)\d\d-",
    "openai": r"sk-(?:proj|svcacct|admin)-|sk-",
    "github": r"github_pat_|gh[pousr]_",
    "aws": r"AKIA|ASIA",
    "stripe": r"(?:sk|rk)_live_",
    "slack": r"xox[baprs]-|xapp-\d-",
    "google": r"AIza",
    "deepseek": r"sk-",
    "twilio": r"SK",
    "telegram": r"[0-9]+:AA",
}
# Literal starts first, longest first, so sk-or-v1- wins over the sk- of the older rules.
_KEY_PREFIX = re.compile(
    "|".join(
        [
            *sorted({re.escape(s) for r in RULES for s in r.starts}, key=lambda s: (-len(s), s)),
            *_OLDER_PREFIXES.values(),
        ]
    )
)
_SEPARATORS = "-_./+=:"
_LONG_WORDS = ("sample", "example", "replace", "changeme", "redacted", "placeholder")
_PLACEHOLDER_WORDS = ("your", "dummy", *_LONG_WORDS, "xxxx")  # each is in a placeholder's body
_SEP = re.escape(_SEPARATORS)
_WORD = rf"(?:your|dummy|{'|'.join(_LONG_WORDS)}|x{{4,}})"
_LONG = rf"(?:{'|'.join(_LONG_WORDS)}|x{{8,}})"
_EDGE_BEFORE, _EDGE_AFTER = rf"(?:^|(?<=[{_SEP}]))", rf"(?=[{_SEP}]|\Z)"
_PLACEHOLDER_TOKEN = re.compile(  # (b) and (c), in the lowercased body
    rf"{_EDGE_BEFORE}{_WORD}{_EDGE_AFTER}|{_EDGE_BEFORE}{_LONG}|{_LONG}{_EDGE_AFTER}"
)
_LEADING_WORD = re.compile(_WORD)


def _one_character(text: str) -> bool:
    """Is `text` one character repeated, separators aside (True for separators only, or none)?"""
    first = text.lstrip(_SEPARATORS)[:1]
    return not first or not text.replace(first, "").strip(_SEPARATORS)


def is_placeholder(value: str) -> bool:
    """Is this matched key an obvious placeholder (see above), not a key?"""
    prefix = _KEY_PREFIX.match(value)
    body = (value[prefix.end() :] if prefix else value).lower()
    if _one_character(body):  # (a)
        return True
    if not any(map(body.__contains__, _PLACEHOLDER_WORDS)):  # a substring test is the fast no
        return False
    if _PLACEHOLDER_TOKEN.search(body):  # (b), (c)
        return True
    bare = body.lstrip(_SEPARATORS)
    lead = _LEADING_WORD.match(bare)  # (d)
    return lead is not None and _one_character(bare[lead.end() :])


# A key head followed directly by a template, as in docs: xoxb-1234567890-<your-token>,
# xoxb-1234567890-{SLACK_TOKEN}, xoxp-1234567890-${SLACK_USER_TOKEN}. Only when the bracketed
# text holds a placeholder word or is an UPPER_SNAKE name (with an underscore inside <...>, so
# <BR> is not one). A closing tag never counts, so a key inside XML (<token>KEY</token>) is
# still reported.
_TEMPLATE = re.compile(r"<(?!/)([^<>{}\s]{1,64})>|\$?\{([^<>{}\s]{1,64})\}")
_UPPER_SNAKE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


def _template_after(text: str, end: int) -> bool:
    template = _TEMPLATE.match(text, end)
    if template is None:
        return False
    angle, curly = template.groups()
    inner = angle or curly or ""
    if any(map(inner.lower().__contains__, _PLACEHOLDER_WORDS)):
        return True
    return _UPPER_SNAKE.fullmatch(inner) is not None and (curly is not None or "_" in inner)


def _matches(
    text: str,
    rules: tuple[SecretRule, ...] = RULES,
    pos: int = 0,
    endpos: int | None = None,
    placeholders: bool = False,
) -> list[tuple[SecretMatch, int, int]]:
    """Every key in text[pos:endpos] (lookbehinds still see what comes before `pos`), with the
    span of the whole rule match (name and separator included for the generic rules), in text
    order. A rule never reports text an earlier rule claimed: the claimed spans are kept sorted,
    so each check is a binary search, not a scan of every match so far. Obvious placeholders are
    left out unless `placeholders` (for redaction, which hides anything key-shaped)."""
    end_at = len(text) if endpos is None else endpos
    found: list[tuple[SecretMatch, int, int]] = []
    window = text[pos:end_at] if pos or end_at != len(text) else text
    folded = _fold(window) if any(r.needs for r in rules) else ""
    starts: list[int] = []  # value spans claimed by earlier rules, in text order (never overlap)
    ends: list[int] = []
    for rule in rules:
        if rule.needs and not any(word in folded for word in rule.needs):
            continue
        if rule.starts and not any(word in window for word in rule.starts):
            continue
        mine: list[tuple[SecretMatch, int, int]] = []
        for m in rule.pattern.finditer(text, pos, end_at):
            start, end = m.span(rule.group)
            i = bisect_right(starts, start)
            if (i and ends[i - 1] > start) or (i < len(starts) and starts[i] < end):
                continue
            value = m.group(rule.group)
            if rule.min_entropy and not _looks_random(value, rule.min_entropy):
                continue
            if not placeholders and (is_placeholder(value) or _template_after(text, end)):
                continue
            mine.append((SecretMatch(rule.id, rule.name, value, start, end), m.start(), m.end()))
        if mine:  # one rule's matches never overlap each other (finditer)
            found += mine
            claimed = [*zip(starts, ends, strict=True), *((f.start, f.end) for f, _, _ in mine)]
            spans = sorted(claimed)
            starts = [a for a, _ in spans]
            ends = [b for _, b in spans]
    return sorted(found, key=lambda f: f[0].start)


def find_secrets(text: str, *, placeholders: bool = False) -> list[SecretMatch]:
    """Every key written out plainly in `text`, in text order. Obvious placeholders are left
    out; with `placeholders`, they are kept (for redaction)."""
    return [match for match, _, _ in _matches(text, placeholders=placeholders)]


def redact(value: str) -> str:
    return f"{value[:4]}…(len {len(value)})"


# ---- keys hidden on purpose -------------------------------------------------------------------
#
# A key can be written so that no plain pattern sees it: cut by invisible characters, split into
# string literals joined by an operator or side by side, cut by an empty quoted string, folded
# over lines (backslash continuations, YAML block scalars), or base64-encoded (a token under 40
# characters only when it is one whole key). `find_hidden` undoes each trick on a copy of the
# text (a "view") and runs the same rules over it. A view drops the characters that hide a key
# (the "joins") and keeps a map back to the text, so a hidden key is placed at its first
# character. Only matches that span a join are reported: anything else was plain text all along.
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
# Text kept on each side of a join. Most rule matches are under 260 characters; a PyPI token
# (up to 1000+) or a 1Password token (no upper bound) hidden by a join is read only if it fits in
# about 2 * CONTEXT. Longer ones are listed under "Still missed" in the AP001/AP002 notes.
CONTEXT = 320
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
    dropped_at: int = -1  # base64_texts: where a token was left undecoded (budget spent)


Join = tuple[int, int, str]  # text[start:end] is dropped from the view; the trick's name

# The patterns that scan a whole text start with a fixed character where they can, so the regex
# engine skips ahead to it: a leading lookbehind or a top-level alternation makes it try every
# position, several times slower.

# Invisible format characters: soft hyphen, Mongolian vowel separator, zero-width space,
# non-joiner and joiner, left-to-right and right-to-left marks, bidi embeddings and isolates,
# word joiner and invisible operators, byte-order mark (zero-width no-break space).
INVISIBLE_CHARS = (
    "\u00ad\u180e\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff"
)
_INVISIBLE = re.compile(f"[{INVISIBLE_CHARS}]+")

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

# An empty quoted string inside a word: a shell reads abc""def and abc''def as abcdef, and
# "abc""def" is two literals side by side. Only between letters, digits, `_`, `-` or `.`, so
# `x = ""`, f("", "") and a CSV "" escape next to a space or a quote are left alone. A run of
# them (abc""''def) is one join.
_WORD_CHAR = r"[A-Za-z0-9_.\-]"
_EMPTY_QUOTES = tuple(
    re.compile(rf"{q}(?<={_WORD_CHAR}{q})(?:\"\"|'')*+(?={_WORD_CHAR})") for q in ('""', "''")
)

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
# A shorter token (24 to 39 characters, so 18 to 29 bytes) is read only when it is one whole
# key. The only keys that short are AWS key IDs, Slack tokens and short GitLab tokens (a test
# checks every rule's shortest match). A key's first three characters encode to the token's
# first four, so only a token that starts like the base64 of one of these is decoded.
_MIN_SHORT_BASE64 = 24
_SHORT_KEY_STARTS = ("AKI", "ASI", "xox", "xap", "glp")
_SHORT_BASE64_LEADS = tuple(base64.b64encode(s.encode()).decode() for s in _SHORT_KEY_STARTS)
_NOT_LEAD = re.compile(r"[^A-Za-f]")  # base64 of ASCII text starts each 4 characters with A-f
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_CONTROL_BYTES = bytes(b for b in range(32) if b not in b"\t\n\r") + b"\x7f"

# A double-quoted string with at least one backslash escape (JSON, and YAML and TOML too).
_ESCAPED_STRING = re.compile(r'"[^"\\\n]*+(?:\\.[^"\\\n]*+)++"')
# The tricks above as they look inside such a string: an escaped invisible character, an escaped
# backslash-newline, escaped quotes joined by an operator or a space, two escaped quotes inside
# a word (abc\"\"def: "a""b" side by side, or an empty string), the escaped newline that ends a
# YAML block header.
_JSON_HINT = re.compile(
    r"\\(?:u(?:00[aA][dD]|180[eE]|200[b-fB-F]|202[a-eA-E]|206[0-46-9]|[fF][eE][fF][fF])"
    r"|\\\\n"
    r'|"[ \t]*+\+[ \t]*+\\"|"[ \t]++(?:\.\.?+[ \t]++)?+\\"'
    rf'|"\\"(?<={_WORD_CHAR}\\"\\")(?={_WORD_CHAR})'
    r"|n(?:(?<=[:-] [|>]\\n)|(?<=[:-] [|>][1-9+-]\\n)))"
)


def _invisible(text: str) -> Iterator[Join]:
    """Each run of invisible characters, in text order. Found with str.find per character: a
    character-class regex over the whole text is several times slower on non-ASCII text."""
    if text.isascii():
        return
    joins: list[Join] = []
    for ch in INVISIBLE_CHARS:
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


def _emptied(text: str) -> Iterator[Join]:
    """Each run of empty quoted strings inside a word."""
    for pattern in _EMPTY_QUOTES:
        for m in pattern.finditer(text):
            yield m.start(), m.end(), "split"


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


_COLLECTORS = (_invisible, _split, _emptied, _continued, _folded)


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


# Every generic match holds one of these words (folded); a view without any skips the (slow)
# generic rules.
_GENERIC_WORDS = tuple(word for rule in RULES if rule.id == "generic" for word in rule.needs)
_SPECIFIC_RULES = tuple(rule for rule in RULES if rule.id != "generic")
# No rule matches fewer characters than this (a Slack token: xoxb- and ten more; a test checks
# every rule), so a shorter view holds no key.
_SHORTEST_MATCH = 12


@dataclass
class _View:
    """text[start:end] without its joins, and the way back to the text."""

    source: str  # the whole text the view was cut from
    start: int
    end: int
    text: str
    view_at: list[int]  # where each kept piece starts in the view ...
    text_at: list[int]  # ... and in the text
    cuts: list[int]  # where each join was, in the view
    kinds: list[str]
    plain: tuple[list[int], list[int]] | None = None  # plain keys in source[start:end], once

    def in_text(self, i: int) -> int:
        k = bisect_right(self.view_at, i) - 1
        return self.text_at[k] + i - self.view_at[k]


def _view(text: str, start: int, end: int, joins: list[Join]) -> _View:
    view = _View(text, start, end, "", [], [], [], [])
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


def _plain_near(view: _View, v_start: int, v_end: int, start: int, end: int) -> bool:
    """Is there a plain key in the text that overlaps text[start:end], the span of the view's
    match view.text[v_start:v_end]? The plain keys of the window are found once per view. When
    long joins make the window much longer than its view, only the kept pieces under the match
    are read instead (a plain key never spans a join), each no further than CONTEXT beyond it."""
    if view.end - view.start <= 2 * len(view.text) + 4 * CONTEXT:
        if view.plain is None:
            found = _matches(view.source, RULES, view.start, view.end)
            view.plain = ([m.start for m, _, _ in found], [m.end for m, _, _ in found])
        starts, ends = view.plain
        i = bisect_right(starts, start)
        return bool((i and ends[i - 1] > start) or (i < len(starts) and starts[i] < end))
    first = bisect_right(view.view_at, v_start) - 1
    last = bisect_right(view.view_at, v_end - 1) - 1
    for k in range(first, last + 1):
        piece_start = view.text_at[k]
        view_end = view.view_at[k + 1] if k + 1 < len(view.view_at) else len(view.text)
        piece_end = piece_start + view_end - view.view_at[k]
        lo, hi = max(piece_start, start - CONTEXT), min(piece_end, end + CONTEXT)
        if lo < hi and any(
            m.start < end and start < m.end for m, _, _ in _matches(view.source, RULES, lo, hi)
        ):
            return True
    return False


def _in_view(view: _View, plain_wins: bool) -> Iterator[SecretMatch]:
    """Keys in a view that span at least one join. With `plain_wins`, not those that overlap a
    plain key: a view joins pieces with no separator, so a rule with a loose length (anthropic,
    openai, generic, ...) would read a plain key plus the next word as one longer key, and the
    plain key is what is reported. Redaction keeps them, to hide every character either way."""
    if len(view.text) < _SHORTEST_MATCH:
        return
    folded = _fold(view.text)
    rules = RULES if any(word in folded for word in _GENERIC_WORDS) else _SPECIFIC_RULES
    cuts = view.cuts
    # Redaction (plain_wins off) keeps placeholders too, so anything key-shaped is hidden.
    for match, full_start, full_end in _matches(view.text, rules, placeholders=not plain_wins):
        k = bisect_right(cuts, full_start)
        if k == len(cuts) or cuts[k] >= full_end:
            continue  # no join inside: plain text, which find_secrets reads
        inside = bisect_right(cuts, match.start)
        if inside < len(cuts) and cuts[inside] < match.end:
            k = inside  # name the trick that cut the key itself
        start, end = view.in_text(match.start), view.in_text(match.end - 1) + 1
        if plain_wins and _plain_near(view, match.start, match.end, start, end):
            continue
        yield SecretMatch(match.rule_id, match.name, match.value, start, end, view.kinds[k])


def _joined(text: str, plain_wins: bool) -> Iterator[SecretMatch]:
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
        yield from _in_view(view, plain_wins)


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


def _base64_tokens(
    text: str, mask: bytes, shortest: int = _MIN_BASE64, longest: int | None = None
) -> Iterator[tuple[int, int]]:
    """(start, end) of each whole base64 token of `shortest` characters or more, and fewer than
    `longest` (padding not counted, but included in the span). `mask` is the text's base64
    mask. A token may follow a JSON escape such as \\n; other escapes rule it out."""
    run = b"A" * shortest
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
        if pad < 3 and shortest <= end - start and (longest is None or end - start < longest):
            yield start, end + pad


def base64_texts(
    text: str, budget: Budget, shortest: int = _MIN_BASE64, longest: int | None = None
) -> Iterator[tuple[int, int, str]]:
    """(start, end, decoded) for each base64 token of `text` (`shortest` characters or more,
    fewer than `longest`) that decodes to text, within the limits of the hidden-key search:
    MAX_RUNS tokens looked at, budget.blobs of them decoded, each to at most MAX_BLOB_CHARS
    characters of base64 (AP010 reads the same blobs)."""
    mask = text.encode("ascii", "replace").translate(_BASE64_MASK)
    for start, end in _base64_tokens(text, mask, shortest, longest):
        head = text[start : min(end, start + MAX_BLOB_CHARS + 2)]
        if not _plausible(head):
            continue
        if budget.blobs <= 0:
            budget.dropped_at = start
            return
        budget.blobs -= 1
        decoded = _decode(head)
        if decoded is not None:
            yield start, end, decoded


def _in_base64(text: str, budget: Budget, placeholders: bool = False) -> Iterator[SecretMatch]:
    for start, end, decoded in base64_texts(text, budget):
        for found in find_secrets(decoded, placeholders=placeholders):
            yield SecretMatch(found.rule_id, found.name, found.value, start, end, "base64")
    # Shorter tokens, after the long ones so they never take a long one's place in the budget.
    mask = text.encode("ascii", "replace").translate(_BASE64_MASK)
    for start, end in _base64_tokens(text, mask, _MIN_SHORT_BASE64, _MIN_BASE64):
        if budget.blobs <= 0:
            return
        if not text.startswith(_SHORT_BASE64_LEADS, start):
            continue
        budget.blobs -= 1
        decoded = _decode(text[start:end])
        if decoded is None:
            continue
        key = decoded.removesuffix("\n").removesuffix("\r")  # `echo key | base64` adds one
        found = find_secrets(key, placeholders=placeholders)
        if len(found) == 1 and found[0].value == key:  # the whole text is one key
            yield SecretMatch(found[0].rule_id, found[0].name, key, start, end, "base64")


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


def _hidden_in(
    text: str, budget: Budget, plain_wins: bool = True, nested: bool = False
) -> Iterator[SecretMatch]:
    yield from _joined(text, plain_wins)
    if nested:
        return
    yield from _in_base64(text, budget, placeholders=not plain_wins)
    for start, end, decoded in _in_json_strings(text, budget):
        for found in _hidden_in(decoded, budget, plain_wins, nested=True):
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


def hidden_spans(text: str) -> list[SecretMatch]:
    """Every hidden key in `text`, each occurrence, in text order: what output must redact.
    Unlike find_hidden it keeps every copy, keys also written out plainly, and longer matches
    that overlap a plain key (so the rest of a key cut after a plain-looking prefix is hidden
    too)."""
    return sorted(_hidden_in(text, Budget(), plain_wins=False), key=lambda m: m.start)


def find_secrets_deep(text: str) -> list[SecretMatch]:
    """find_secrets, plus the keys find_hidden finds that are not also written out plainly."""
    direct = find_secrets(text)
    hidden = find_hidden(text, {m.value for m in direct})
    return sorted([*direct, *hidden], key=lambda m: m.start)


# ---- masking text agentprint echoes back ------------------------------------------------------

# A known key start glued to other text (a file name, a flag): output masks it even where the
# detector, which wants a word boundary, would not report it.
_GLUED = re.compile(rf"(?:{_KEY_PREFIX.pattern})[A-Za-z0-9_\-]{{16,}}")


def key_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, key) for every key mask_keys cuts: what find_secrets sees (placeholders
    too), every hidden copy (spanning the text it was hidden in), and any known key start glued
    to other text. Unsorted; spans may overlap."""
    spans = [(m.start, m.end, m.value) for m in find_secrets(text, placeholders=True)]
    spans += [(m.start, m.end, m.value) for m in hidden_spans(text)]
    spans += [(m.start(), m.end(), m.group()) for m in _GLUED.finditer(text)]
    return spans


def mask_keys(text: str) -> str:
    """`text` with every key in it cut to its redact() stub: what find_secrets sees (placeholders
    too), every hidden copy, and any known key start glued to other text. For text agentprint
    repeats back, such as a command line it asks the agent to run again."""
    spans = key_spans(text)
    out: list[str] = []
    end = 0
    for start, stop, value in sorted(spans, key=lambda s: (s[0], -s[1])):
        if start >= end:
            out += [text[end:start], redact(value)]
            end = stop
        elif stop > end:  # an overlapping, longer span: extend the cut, keep the first stub
            end = stop
    return "".join([*out, text[end:]])
