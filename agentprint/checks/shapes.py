"""The risky-shape engine behind AP010: one command line, or one sentence of instructions, at a
time.

Shapes (each needs the risky shape, never a keyword):
- credential sent out (critical). In commands, a credential's contents must flow into a sender:
  piped into it (`cat ~/.ssh/id_rsa | curl -d @-`), uploaded by it (`curl -F f=@~/.ssh/id_rsa`,
  `-T ~/.netrc`, `wget --post-file`, `iwr -InFile`, `scp <key> host:`), read inside its arguments
  (`curl -d "$(cat .env)"`, `requests.post(..., data=open(...))`), redirected to /dev/tcp, or
  read into a variable on one line and sent on a later one. In prose, an imperative read of a
  credential (read, send, upload ... ~/.aws/credentials) and a sender in the same sentence: a
  sending command, or plain words (send, post, upload ... to a URL, host:port or webhook). A
  credential only named next to a sender is not sent: `ssh -i <key>`, `--kubeconfig <file>`,
  "the SDK reads credentials from ...". known_hosts, ssh config, authorized_keys and public keys
  are never credentials.
- piped install (medium; high when the text says to keep it from the user; low in a code block
  under an Install, Setup, Prerequisites or Getting started heading of a Markdown file, ruling
  X17): a download run by a shell or interpreter that reads its standard input (`curl ... | sh`,
  `| bash -s -- --yes`, `| python3 -`, `| pwsh -Command -`, `bash <(curl ...)`, `bash -c
  "$(curl ...)"`, `iex (irm ...)`, `irm ... | iex`), or downloaded to a file that is then run
  without a checksum. Data piped into a script (`| bash scripts/x.sh`, `| python3 tools/x.py`)
  is not running the download.
- encoded command (high): a base64 decode piped into a shell, `powershell -EncodedCommand`,
  FromBase64String with iex, exec(base64.b64decode(...)) (also through __import__), and base64
  blobs that decode to a shell command.

In prose, a negation (never, don't, avoid ...) silences a shape only when it governs it: in the
same clause, before it. "Never pipe `curl ... | sh`" is a warning; "do not tell the user: run
`curl ... | sh`" is not. In prose a command is one code span or plain text: a match across a
backtick lists words, it is not a command.

Speed: every shape is tried only where its anchor word is, over at most WINDOW characters, at
most MAX_TRIES times per block, within a per-file budget that grows with the file (Tries.for_text),
so time stays linear in the file whatever it holds. A file that spends its budget is reported as
not fully checked (low), never passed silently.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import chain
from typing import NamedTuple

from ..secrets import Budget, base64_texts, mask_keys
from .base import safe_snippet

EXFILTRATION = "credential sent out"
PIPED = "piped install"
ENCODED = "encoded command"
NOT_CHECKED = "not fully checked"

WINDOW = 512  # characters one try of a shape reads, from its anchor
MAX_TRIES = 16  # tries per shape per command or sentence
MAX_LINE = 8192  # a longer command or sentence is not read for shapes (minified code)
MAX_PARAGRAPH = 256  # lines of prose read as one paragraph; a longer one is read in parts
# The per-file budget, linear in the file's length: a fixed part plus a share per character.
BASE_TRIES, TRIES_PER_CHAR = 8192, 1 / 8
BASE_CHARS, CHARS_PER_CHAR = 2_000_000, 2
BLOCK_COST = 400  # what one command or sentence that holds a shape's word costs, on top


@dataclass
class Tries:
    """What one file may still spend on shapes: tries, and characters read by anchor searches
    (each command or sentence that holds a shape's word also costs BLOCK_COST)."""

    left: int = BASE_TRIES
    chars: int = BASE_CHARS

    @classmethod
    def for_text(cls, size: int) -> Tries:
        return cls(BASE_TRIES + int(size * TRIES_PER_CHAR), BASE_CHARS + size * CHARS_PER_CHAR)

    @property
    def spent(self) -> bool:
        return self.left <= 0 or self.chars <= 0


class Low(str):
    """A block lowercased, with the shape words found in it (looked up once per block)."""

    words: frozenset[str]

    def __new__(cls, block: str) -> Low:
        low = super().__new__(cls, block.lower())
        low.words = frozenset(word for word in ALL_WORDS if word in low)
        return low


@dataclass(frozen=True)
class _Shape:
    words: frozenset[str]  # one of these is in the lowercased text wherever the shape is
    anchor: re.Pattern[str]  # where the shape can start
    pattern: re.Pattern[str]  # the shape, matched at an anchor within WINDOW characters

    def present(self, low: str) -> bool:
        """True when one of the shape's words is in `low` (the block, lowercased)."""
        if isinstance(low, Low):
            return not self.words.isdisjoint(low.words)
        return any(word in low for word in self.words)

    def find(
        self, block: str, low: str, tries: Tries, prose: bool, after: int = 0
    ) -> Iterator[re.Match[str]]:
        """Each match, in order. `low` is the block lowercased: a shape none of whose words is
        in it is not looked for."""
        if tries.spent or not self.present(low):
            return
        tries.chars -= len(block) - after  # what the anchor search reads
        for n, anchor in enumerate(self.anchor.finditer(block, after)):
            if n >= MAX_TRIES or tries.left <= 0:
                return
            tries.left -= 1
            start = anchor.start()
            tries.chars -= min(WINDOW, len(block) - start)  # the most one try can read
            match = self.pattern.match(block, start, start + WINDOW)
            if match is not None and not (prose and "`" in match.group()):
                yield match


ALL_WORDS: set[str] = set()  # every shape's words, filled in as shapes are made


def _shape(words: tuple[str, ...], anchor: str, pattern: str, flags: int = 0) -> _Shape:
    ALL_WORDS.update(words)
    return _Shape(frozenset(words), re.compile(anchor, flags), re.compile(pattern, flags))


# --- command words ---------------------------------------------------------------------------

_STARTS = r"(?<![\w./-])"  # a command word, not part of a word, path or URL
_FETCH = rf"{_STARTS}(?i:curl|wget)(?:\.exe)?\b"
_SUDO = r"(?:sudo\s+(?:-\S+\s+)*)?(?:(?:/usr)?/bin/env\s+(?:-\S+\s+)*)?(?:env\s+(?:\w+=\S*\s+)*)?"
_SHELL_NAME = r"(?:/usr/local/bin/|/usr/bin/|/bin/)?(?i:ba|z|da|k|fi)?(?i:sh)\b"
# The end of a command: a separator, or a quote right after it (closing an enclosing string;
# a quote after a space starts an argument, such as a script's path).
_ENDS = r"(?=\s*(?:$|[;&|)#])|[\"'`])"
# A shell that runs its standard input: options only (not -c), then the end of the command;
# or -s, after which the arguments are the script's.
_SHELL = (
    rf"{_SUDO}{_SHELL_NAME}(?:"
    r"(?:\s+-(?!-|c\b)[a-zA-Z]*)*\s+-[a-zA-Z]*s[a-zA-Z]*\b"
    rf"|(?:\s+-(?!c\b)[a-zA-Z]*)*(?:\s+--)?{_ENDS}"
    r")"
)
_INTERP = (
    rf"{_SUDO}(?:(?i:python[23]?(?:\.\d+)?|perl|ruby|node)(?:\s+-(?![cme]\b)[a-zA-Z]+)*(?:\s+-)?"
    r"|(?i:pwsh|powershell)(?:\.exe)?(?:\s+-(?!c\b|command\b)\w+)*(?:\s+-(?i:c|command))?\s+-)"
    rf"{_ENDS}"
)
_BAR = r"\|(?!\|)"  # a pipe, not ||
_PIPE = rf"{_BAR}\s*"
_STAGE = r"[^|;\n]*"  # one stage of a pipeline, its spaces included: up to the next |
_SHELL_WORD = r"(?:(?<=/)|(?<![\w./-]))(?i:ba|z|k)?(?i:sh)"
_PS_FETCH = r"(?<![\w-])(?:irm|iwr|invoke-restmethod|invoke-webrequest)\b"
_PS_RUN = r"(?<![\w-])(?:iex|invoke-expression)\b"
_FETCH_WORDS = ("curl", "wget")
_PS_RUN_WORDS = ("iex", "invoke-expression")
_OUTPUT = r"\s-(?:[a-zA-Z]*[oO]|-output(?:-document)?)[=\s]\s*[\"']?([^\s\"';&|]+)[\"']?"
_CHECKED = r"(?!sha\d*sum|shasum|gpg|cosign|minisign|verify|checksum)"

_PIPED_SHAPES = (
    _shape(
        _FETCH_WORDS,
        _FETCH,
        rf"{_FETCH}{_STAGE}(?:{_BAR}{_STAGE})*?{_PIPE}(?:{_SHELL}|{_INTERP})",
    ),
    _shape(
        _FETCH_WORDS,
        rf"{_SHELL_WORD}\b|{_STARTS}source\b",
        rf"(?:{_SHELL_WORD}|{_STARTS}source)\s+(?:-\S+\s+)*<\(\s*(?i:curl|wget)\b",
    ),
    _shape(_FETCH_WORDS, r"(?:^|(?<=[\s;&|(]))\.\s", r"\.\s+<\(\s*(?i:curl|wget)\b"),
    _shape(
        _FETCH_WORDS,
        rf"{_SHELL_WORD}\b|{_STARTS}eval\b",
        rf"(?:{_SHELL_WORD}\s+-c|{_STARTS}eval)\s+[\"']?\$\(\s*(?i:curl|wget)\b",
    ),
    # Downloaded to a file, then run with no checksum in between.
    _shape(
        _FETCH_WORDS,
        _FETCH,
        rf"{_FETCH}[^|;\n]*?{_OUTPUT}(?:{_CHECKED}[^\n]){{0,400}}?(?:&&|;)\s*{_SUDO}"
        r"(?:(?i:ba|z|da|k)?(?i:sh)|python[23]?|source|\.)\s+[\"']?\1(?=[\"'\s;&|)]|$)",
    ),
    _shape(
        ("irm", "iwr", "invoke-web", "invoke-rest", "webclient"),
        _PS_RUN,
        rf"{_PS_RUN}[\s(]*(?:irm|iwr|invoke-restmethod|invoke-webrequest"
        r"|new-object\s+(?:system\.)?net\.webclient)\b",
        re.I,
    ),
    _shape(_PS_RUN_WORDS, _PS_FETCH, rf"{_PS_FETCH}[^|\n]*\|\s*(?:iex|invoke-expression)\b", re.I),
    _shape(
        ("downloadstring",),
        r"\.downloadstring\s*\(",
        r"\.downloadstring\s*\([^)\n]*\)\s*\|\s*(?:iex|invoke-expression)\b",
        re.I,
    ),
)

# --- credentials and senders -----------------------------------------------------------------

_SEP = r"[\\/]"
_CREDENTIALS = re.compile(
    rf"(?<![\w.-])(?:"
    rf"\.ssh(?:{_SEP}[\w.@-]+)?(?![\w-])"
    rf"|id_(?:rsa|ed25519|ecdsa|dsa)(?:\.pub)?(?![\w.-])"
    rf"|\.aws{_SEP}credentials(?![\w.-])"
    rf"|[._]netrc(?![\w.-])|\.git-credentials(?![\w.-])|\.pypirc(?![\w.-])"
    rf"|\.docker{_SEP}config\.json|\.kube{_SEP}config(?![\w.-])|\.config{_SEP}gh{_SEP}hosts\.ya?ml"
    rf"|\.claude{_SEP}(?:history\.jsonl|projects{_SEP}|\.credentials\.json)|\.claude\.json"
    rf"|\.codex{_SEP}(?:history\.jsonl|sessions{_SEP}|auth\.json)"
    rf"|\.gemini{_SEP}(?:tmp{_SEP}|oauth_creds\.json)"
    rf"|\.cursor{_SEP}projects{_SEP}|\.copilot{_SEP}(?:session-state{_SEP}|config\.json)"
    rf"|\.hermes{_SEP}(?:state\.db|auth\.json|\.env)|\.local{_SEP}share{_SEP}opencode{_SEP}"
    r")"
    r"|(?<![\w-])security\s+(?:find-generic-password|find-internet-password|dump-keychain)\b"
    r"|(?<![\w-])secret-tool\s+lookup\b"
)
# Files under ~/.ssh that hold no secret.
_SSH_NOT_SECRET = re.compile(r"known_hosts2?|config|authorized_keys2?|environment|rc", re.I)
_ENV_FILE = (
    r"(?<![\w$])\.env(?:\.(?:local|production|prod|development|dev|staging|test))?(?![\w.-])"
)
_ENV = re.compile(_ENV_FILE)
_CRED = rf"(?:{_CREDENTIALS.pattern}|{_ENV_FILE})"
_CRED_WORDS = (
    "ssh", "id_", ".aws", "netrc", "credentials", "pypirc", ".docker", ".kube", "hosts.y",
    ".claude", ".codex", ".gemini", ".cursor", ".copilot", ".hermes", "opencode", "security",
    "secret-tool", ".env",
)  # fmt: skip
# The folders in front of a credential (~/, $HOME/, /Users/me/, ./, $HOME\): segments without a
# separator, each followed by one, so the repeat never reads the same characters twice.
_PREFIX = r"(?:[\w.~${}%:-]{0,64}[\\/]){0,8}"
_READERS = r"(?:cat|base64|head|tail|gzip|xxd|od|tar|zip|type|Get-Content|gc|openssl|gpg)"
_OPENERS = r"(?:open|readFileSync|readFile|read_text|read_bytes|createReadStream|Get-Content)"

_SEND_SHAPES = (
    _shape(
        _FETCH_WORDS,
        _FETCH,
        rf"{_FETCH}[^|;\n]*?\s(?:-d|--data(?:-binary|-raw|-urlencode|-ascii)?|-F|--form"
        r"(?:-string)?|-T|--upload-file|--json|(?:-X|--request)\s*['\"]?(?:POST|PUT)\b)",
    ),
    _shape(("--post-",), _FETCH, rf"{_FETCH}[^|;\n]*?--post-(?:data|file)\b"),
    _shape(
        ("fetch",),
        r"(?<![\w.])fetch\s*\(",
        r"fetch\s*\([^\n]*?\bmethod\s*:\s*['\"`]?(?:post|put)\b",
        re.I,
    ),
    _shape(
        (".post", ".put"),
        r"\b(?:requests|httpx|session|client|axios)\.(?:post|put)\s*\(",
        r"(?:requests|httpx|session|client|axios)\.(?:post|put)\s*\(",
    ),
    _shape(
        ("nc ", "nc\t", "ncat", "netcat"),
        rf"{_STARTS}(?:nc|ncat|netcat)\b",
        r"(?:nc|ncat|netcat)(?:\s+-[\w-]+)*\s+[\w.-]+\s+\d{1,5}\b",
    ),
    _shape(("/dev/",), r"/dev/(?:tcp|udp)/", r"/dev/(?:tcp|udp)/[\w.-]+/\d+"),
    _shape(
        ("-method", "-infile"),
        _PS_FETCH,
        rf"{_PS_FETCH}[^|\n]*-(?:method\s+['\"]?post|infile)\b",
        re.I,
    ),
)
# Senders that name the credential themselves: copying a key to another host.
_SELF_SENDERS = (
    _shape(
        ("scp", "rsync"),
        rf"{_STARTS}(?:scp|rsync)\b",
        rf"(?:scp|rsync)(?:\s+-\S+)*\s+[\"']?{_PREFIX}{_CRED}\S*\s+[\"']?[\w.@-]+:",
    ),
)
# A credential's contents flowing into the sender: piped into it or redirected to /dev/tcp
# (from where it is read), or its file uploaded or read inside the sender's arguments (from the
# sender on).
_PIPED_IN = (
    _shape(
        _CRED_WORDS,
        _CRED,
        rf"{_CRED}[\"']?[^;&)\n|]*\|(?:[^;&)\n|]*\|){{0,8}}?\s*(?:sudo\s+)?"
        r"(?:curl|wget|nc|ncat|netcat)\b",
    ),
    _shape(_CRED_WORDS, _CRED, rf"{_CRED}[\"']?[^;&|\n>]{{0,64}}>\s*/dev/(?:tcp|udp)/"),
)
_UPLOADED = (
    _shape(
        ("@", "--post-file", "-t", "--upload-file", "<", "-infile"),
        r"@|--post-file|\s-T\s|--upload-file|<|(?i:-InFile)",
        rf"(?:@|--post-file[=\s]|\s-T\s|--upload-file\s|<|(?i:-InFile)\s)\s*[\"']?{_PREFIX}{_CRED}",
    ),
    _shape(("$(",), rf"\$\(\s*{_READERS}\s", rf"\$\(\s*{_READERS}\s+[^)\n]*{_CRED}"),
    _shape(("`",), rf"`{_READERS}\s", rf"`{_READERS}\s+[^`\n]*{_CRED}"),
    _shape(
        ("open", "readfile", "read_text", "read_bytes", "createreadstream", "get-content"),
        rf"\b{_OPENERS}\b",
        # Into nested calls (open(os.path.expanduser('~/.aws/credentials'))), over empty
        # ones (os.homedir()), never past the call's closing parenthesis.
        rf"{_OPENERS}(?:\s*\(|\s)(?:[^()\n]|\(\s*\)|\()*?{_CRED}",
    ),
    _shape(
        ("read_text", "read_bytes"),
        _CRED,
        rf"{_CRED}[\"']?\s*\)?\s*\.read_(?:text|bytes)\s*\(",
    ),
)
# A variable set from a credential's contents, for a sender on a later line: K=$(cat ~/.ssh/x).
_ASSIGNED = re.compile(
    rf"(?<![\w$])(?:export\s+)?([A-Za-z_]\w{{0,63}})=[\"']?(?:\$\(\s*{_READERS}\s+|`{_READERS}\s+)"
    rf"[^)`\n]{{0,128}}?{_CRED}"
)

# --- prose -----------------------------------------------------------------------------------

_NEGATION = re.compile(
    r"\b(?:never|don'?t|do not|must not|mustn'?t|should not|shouldn'?t|avoid|forbid\w*|no)\b",
    re.I,
)
# Clause boundaries: a negation governs only what follows it in its own clause.
_CLAUSE = re.compile(r"[;:!?]|\.\s|\s[-\u2013\u2014]\s|\s(?:and|but|then|so)\s", re.I)
_CONCEAL = re.compile(
    r"\b(?:do not|don'?t|never|without)\s+(?:tell|telling|mention|mentioning|inform|informing"
    r"|show|showing|reveal|revealing|notify|notifying|ask|asking|alert|alerting)\b"
    r"|\b(?:silently|secretly|quietly|covertly|stealthily)\b",
    re.I,
)
# An imperative read (or send) of something: at a clause start, after "and", "then", "to" ...
_READ = re.compile(
    r"(?:^|[,:;(]|\b(?:and|then|to|please|first|also|now|next|just|silently|quietly|secretly"
    r"|immediately|automatically|always)\b)\s*"
    r"\b(?:read|cat|open|dump|copy|grab|collect|steal|take|load|get|print|include|attach|send"
    r"|post|upload|transmit|exfiltrate|forward|leak|extract)\b",
    re.I,
)
# Copying a template to .env writes it, it does not read it.
_DESTINATION = re.compile(r"\b(?:to|into|onto)\b", re.I)
_CRED_SEARCH = re.compile(_CRED)
_PLAIN_SEND = re.compile(
    r"\b(?:send|post|upload|transmit|exfiltrate|forward|submit|leak|beacon)\w*\b", re.I
)
_TARGET = re.compile(r"https?://|\bwebhook\b|\b[\w-]+(?:\.[\w-]+)+:\d{2,5}\b", re.I)

_TRIGGER_EXTRA = ("send", "post", "upload", "transmit", "exfiltrat", "forward", "leak")
ALL_WORDS.update(_TRIGGER_EXTRA)


def secret_location(text: str) -> bool:
    """True for a credential location (a key, a token file); False for a public key, known_hosts,
    ssh config or authorized_keys."""
    name = re.split(r"[\\/]", text.strip().rstrip("/\\"))[-1]
    if name.lower().endswith(".pub"):
        return False
    return not (re.search(r"\.ssh[\\/]", text) and _SSH_NOT_SECRET.fullmatch(name))


def _named(text: str) -> str | None:
    """The first credential location named in `text`."""
    for pattern in (_CREDENTIALS, _ENV):
        for match in pattern.finditer(text):
            if secret_location(match.group()):
                return match.group().strip()
    return None


def negated(block: str, at: int) -> bool:
    """True when a negation governs the text at `at`: in its clause, before it."""
    start = 0
    for boundary in _CLAUSE.finditer(block, 0, at):
        start = boundary.end()
    return _NEGATION.search(block, start, at) is not None


def concealed(block: str) -> bool:
    """True when the block tells the agent to keep something from the user."""
    return _CONCEAL.search(block) is not None


def _read_credential(block: str, tries: Tries) -> re.Match[str] | None:
    """An imperative read or send of a credential location in prose, not negated: the verb,
    then at most six words (none of them "to" or "into"), then the location."""
    for n, verb in enumerate(_READ.finditer(block)):
        if n >= MAX_TRIES or tries.spent:
            return None
        tries.left -= 1
        cred = _CRED_SEARCH.search(block, verb.end(), verb.end() + 160)
        if cred is None:
            continue
        between = block[verb.end() : cred.start()]
        if len(between.split()) > 7 or _DESTINATION.search(between):
            continue
        if _named(cred.group()) is not None and not negated(block, verb.end() - 1):
            return cred
    return None


# --- encoded commands ------------------------------------------------------------------------

_PIPE_SHELL_WORDS = ("base64",)
_ENCODED_SHAPES = (
    _shape(
        _PIPE_SHELL_WORDS,
        r"(?<![\w-])base64\s+(?:-d|--decode|-D)\b",
        rf"base64\s+(?:-d|--decode|-D)\b[^|\n]*{_PIPE}{_SHELL}",
    ),
    _shape(
        ("powershell", "pwsh"),
        r"(?<![\w-])(?:powershell|pwsh)(?:\.exe)?\b",
        r"(?:powershell|pwsh)(?:\.exe)?\b[^\n]*?\s-e(?:c|nc|ncodedcommand|ncoded|ncod|nco)?"
        r"\s+[A-Za-z0-9+/=]{16}",
        re.I,
    ),
    _shape(
        ("frombase64string",),
        r"frombase64string\s*\(",
        rf"frombase64string\s*\([^\n]*?{_PS_RUN}",
        re.I,
    ),
    _shape(("frombase64string",), _PS_RUN, rf"{_PS_RUN}[^\n]*?frombase64string", re.I),
    _shape(
        ("b64decode", "atob", "buffer.from", "codecs.decode"),
        r"\b(?:exec|eval)\s*\(",
        r"(?:exec|eval)\s*\(\s*(?:__import__\(\s*['\"]base64['\"]\s*\)\.b64decode"
        r"|base64\.b64decode|atob|Buffer\.from|codecs\.decode)\(",
    ),
)
# Decoded base64 that is a shell command: a shebang, or a command word with arguments first.
_DECODED_COMMAND = re.compile(
    r"\s*(?:#!\s*/\S*(?:sh|bash|zsh)\b"
    r"|(?:sudo\s+)?(?:curl|wget|nc|ncat|netcat)\s+\S"
    r"|(?:sudo\s+)?(?:bash|sh|zsh|python[23]?|perl|ruby|node|osascript)\s+(?:-\w|[\"'/.~$<])"
    r"|(?:sudo\s+)?rm\s+-\w*[rf]|(?:sudo\s+)?chmod\s+[+0-7ugoa]|(?:eval|exec)\s+[\"'$(]"
    r"|(?i:powershell|pwsh)\s+-|(?i:iex|invoke-expression)\s*[(\"'$])"
)
MIN_SHORT_BLOB = 10  # a shorter base64 token is not decoded (7 bytes: `sh -c x`)

# Every shape holds one of its words: a block with none of them is skipped at once.
TRIGGER = tuple(
    sorted(
        {
            word
            for shape in (*_SEND_SHAPES, *_SELF_SENDERS, *_PIPED_SHAPES, *_ENCODED_SHAPES)
            for word in shape.words
        }
        | set(_TRIGGER_EXTRA)
    )
)


# --- one block -------------------------------------------------------------------------------


class Hit(NamedTuple):
    rule: str
    match: re.Match[str]
    credential: str | None
    concealed: bool = False


def _first(
    shapes: Iterable[_Shape], block: str, low: str, prose: bool, tries: Tries
) -> re.Match[str] | None:
    for shape in shapes:
        if not shape.present(low):
            continue
        for match in shape.find(block, low, tries, prose):
            if not (prose and negated(block, match.start())):
                return match
    return None


def _flow(
    block: str, low: str, sender: re.Match[str], tries: Tries, assigned: dict[str, str]
) -> str | None:
    """The credential whose contents flow into `sender`, or None."""
    after = sender.start()
    flows = [
        *(
            m
            for shape in _PIPED_IN
            if shape.present(low)
            for m in shape.find(block, low, tries, False)
        ),
        *(
            m
            for shape in _UPLOADED
            if shape.present(low)
            for m in shape.find(block, low, tries, False, after)
        ),
    ]
    for flow in flows:
        named = _named(flow.group())
        if named is not None:
            return named
    for name, credential in assigned.items():
        if re.search(rf"\$\{{?{re.escape(name)}\b", block[after:]):
            return credential
    return None


def shape_hits(
    block: str,
    prose: bool,
    piped: bool,
    tries: Tries,
    assigned: dict[str, str] | None = None,
) -> list[Hit]:
    """Each risky shape in one command or sentence. `assigned` holds variables set from a
    credential on earlier lines (commands only); this block's assignments are added to it."""
    assigned = {} if assigned is None else assigned
    if tries.spent or len(block) > MAX_LINE:
        return []
    low = Low(block)
    if low.words.isdisjoint(TRIGGER) and not (assigned and "$" in block):
        if not prose and "=" in block:
            _assign(block, assigned)
        return []
    tries.chars -= BLOCK_COST
    hits: list[Hit] = []
    for shape in _SELF_SENDERS:
        if not shape.present(low):
            continue
        for match in shape.find(block, low, tries, prose):
            named = _named(match.group())
            if named is not None and not (prose and negated(block, match.start())):
                hits.append(Hit(EXFILTRATION, match, named))
                break
    for shape in _SEND_SHAPES if not hits else ():
        if not shape.present(low):
            continue
        sender = next(shape.find(block, low, tries, prose), None)
        if sender is None:
            continue
        credential = _flow(block, low, sender, tries, assigned)
        if credential is not None and not (prose and negated(block, sender.start())):
            hits.append(Hit(EXFILTRATION, sender, credential))
            break
    if prose and not hits and not low.words.isdisjoint(_CRED_WORDS):
        read = _read_credential(block, tries)
        if read is not None:
            sender = _first(_SEND_SHAPES, block, low, True, tries)
            plain = _PLAIN_SEND.search(block) if _TARGET.search(block) else None
            if sender is not None or plain is not None:
                credential = _named(read.group()) or read.group()
                hits.append(Hit(EXFILTRATION, sender or read, credential))
    if piped:
        match = _first(_PIPED_SHAPES, block, low, prose, tries)
        if match is not None:
            hits.append(Hit(PIPED, match, None))
    match = _first(_ENCODED_SHAPES, block, low, prose, tries)
    if match is not None:
        hits.append(Hit(ENCODED, match, None))
    if not prose and "=" in block:
        _assign(block, assigned)
    if hits and prose and concealed(block):
        hits = [hit._replace(concealed=True) for hit in hits]
    return hits


def _assign(block: str, assigned: dict[str, str]) -> None:
    for n, match in enumerate(_ASSIGNED.finditer(block)):
        if n >= MAX_TRIES:
            return
        named = _named(match.group())
        if named is not None:
            assigned[match.group(1)] = named


def snippet(block: str, match: re.Match[str]) -> str:
    """The matched command with a little before it, keys redacted in the whole block before it
    is cut, so a key that ends just before the command never shows its tail."""
    head = mask_keys(block[: match.start()])
    tail = mask_keys(block[match.start() : match.start() + 1200])
    return safe_snippet(head[-20:] + tail, 100)


def detail(hit: Hit, block: str) -> str:
    text = snippet(block, hit.match)
    if hit.rule == EXFILTRATION:
        program = re.match(r"[\w.$/-]+", hit.match.group())
        how = safe_snippet(program.group() if program else "a request", 20)
        what = safe_snippet(hit.credential or "", 60)
        said = f'reads {what} and sends it out ({how}): "{text}"'
    elif hit.rule == PIPED:
        said = f'runs a download straight in a shell: "{text}"'
    else:
        said = f'runs base64-decoded text as a command: "{text}"'
    return said + (" and says to keep it from the user" if hit.concealed else "")


def shapes(
    block: str, prose: bool = False, piped: bool = True, tries: Tries | None = None
) -> list[tuple[str, str]]:
    """(rule, detail) for each risky shape in one command or sentence."""
    tries = Tries() if tries is None else tries
    return [(hit.rule, detail(hit, block)) for hit in shape_hits(block, prose, piped, tries)]


def encoded_commands(text: str, budget: Budget, tries: Tries) -> Iterator[tuple[int, str]]:
    """(index, decoded) for each base64 blob in `text` that decodes to a shell command; short
    tokens (MIN_SHORT_BLOB to 39 characters) only when they decode to one exactly."""
    for start, _end, decoded in base64_texts(text, budget):
        head = decoded[:4096]
        printable = sum(ch.isprintable() or ch in "\n\t\r" for ch in head)
        if printable * 20 < len(head) * 19:
            continue
        flat = head.replace("\n", " ")
        if _DECODED_COMMAND.match(head) or shape_hits(flat, False, True, tries):
            yield start, head
    short = Budget(blobs=256 + len(text) // 64)
    for start, _end, decoded in base64_texts(text, short, MIN_SHORT_BLOB, 40):
        if decoded.isascii() and decoded.isprintable() and _DECODED_COMMAND.match(decoded):
            yield start, decoded


# --- blocks: one command or one sentence at a time -------------------------------------------


class Block(NamedTuple):
    line: int
    text: str
    prose: bool
    install: bool = False  # a code block under an Install / Setup heading (ruling X17)


_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_INSTALL = re.compile(
    r"\b(?:install\w*|set\s*up|setup|prerequisites?|requirements?|getting\s+started"
    r"|quick\s*start|dependencies)\b",
    re.I,
)
# A sentence ends at . ! ? before a capital or a quote, or where a line ends and the next starts
# with a capital (rule files often leave the full stop out).
_SENTENCE_END = re.compile(r"(?<=[.!?])[ \t]+(?=[A-Z\"'(\[*_`])|[ \t]*\n(?=[A-Z\"'(\[*_`])")


def _commands(lines: list[tuple[int, str]], install: bool = False) -> Iterator[Block]:
    """Logical lines: a line ending in a backslash continues on the next."""
    pending: list[str] = []
    first = 0
    for number, line in lines:
        if not pending:
            first = number
        if line.rstrip().endswith("\\"):
            pending.append(line.rstrip()[:-1])
            continue
        pending.append(line)
        yield Block(first, " ".join(pending), False, install)
        pending = []
    if pending:
        yield Block(first, " ".join(pending), False, install)


def _sentences(paragraph: list[tuple[int, str]]) -> Iterator[Block]:
    """Each sentence of a paragraph. A sentence too long to read whole (a rule file with no
    full stops) is read line by line instead, so nothing depends on how long a paragraph is."""
    offsets: list[int] = []
    numbers: list[int] = []
    parts: list[str] = []
    size = 0
    for number, line in paragraph:
        offsets.append(size)
        numbers.append(number)
        part = line.strip() + "\n"
        parts.append(part)
        size += len(part)
    text = "".join(parts)
    at = 0
    for match in chain(_SENTENCE_END.finditer(text), [None]):  # lazily: a scan may stop early
        end = match.start() if match else len(text)
        sentence = text[at:end].strip()
        if sentence:
            first = bisect_right(offsets, at) - 1
            if len(sentence) <= MAX_LINE:
                yield Block(numbers[first], sentence.replace("\n", " "), True)
            else:
                last = bisect_right(offsets, max(at, end - 1)) - 1
                for i in range(first, last + 1):
                    yield Block(numbers[i], parts[i].strip(), True)
        at = match.end() if match else end


def blocks(text: str, markdown: bool) -> Iterator[Block]:
    """Each command line and, in Markdown, each sentence of prose."""
    lines = list(enumerate(text.split("\n"), start=1))
    if not markdown:
        yield from _commands(lines)
        return
    fence: list[tuple[int, str]] | None = None
    paragraph: list[tuple[int, str]] = []
    install = False
    for number, line in lines:
        if _FENCE.match(line):
            if fence is None:
                yield from _sentences(paragraph)
                paragraph, fence = [], []
            else:
                yield from _commands(fence, install)
                fence = None
            continue
        if fence is not None:
            fence.append((number, line))
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            yield from _sentences(paragraph)
            paragraph = []
            install = _INSTALL.search(heading.group(1)) is not None
            yield Block(number, heading.group(1), True)
        elif not line.strip():
            yield from _sentences(paragraph)
            paragraph = []
        elif line.startswith(("    ", "\t")) and not paragraph:
            yield from _commands([(number, line)], install)  # an indented code line
        else:
            if _ITEM.match(line) or len(paragraph) >= MAX_PARAGRAPH:
                yield from _sentences(paragraph)
                paragraph = []
            paragraph.append((number, line))
    yield from _sentences(paragraph)
    yield from _commands(fence or [], install)
