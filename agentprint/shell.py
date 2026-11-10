"""Shell text as a shell reads it: for reading, never for running.

`lex` turns text into tokens: words (each with its quoting and its expansions), operators,
redirections and here-documents. Nothing is dropped but blanks, comments and line continuations;
an unclosed quote or substitution runs to the end of the text, the way the shell would read it.
`$'...'` strings are decoded (C escapes), here-document end words are matched exactly (quote
removal, `<<-` tabs, only a newline ends a line) and `$((1 << 2))`, `$[1 << 2]` and `((x << 1))`
are arithmetic, not here-documents.

`parse` groups the tokens into commands: simple commands, pipelines, and-or lists, `{ }` groups,
`( )` subshells, if/while/until/for/select/case, and function definitions. It never fails: a token
it can't place becomes a word of a simple command, so every word of the text is in the tree.

Both are linear in the size of the text. Substitutions nest at most `MAX_NEST` deep; past that a
`$(` is read as plain text (still in the tree, still seen by the backstop).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_NEST = 48  # $( ... ) inside $( ... ), and backticks inside those
MAX_PARSE_NEST = 120  # nested groups, ifs and loops the parser descends into

_PLAIN = re.compile(r"[^ \t\n'\"\\$`;&|()<>]+")
# A whole word of plain text (no quote, `$`, backslash, or array `=(` after it).
_PLAIN_WORD = re.compile(r"[^ \t\n'\"\\$`;&|()<>#][^ \t\n'\"\\$`;&|()<>]*(?=[ \t\n;&|)<>]|\Z)")
_DQ_PLAIN = re.compile(r'[^"\\$`]+')
_ANSI_PLAIN = re.compile(r"[^'\\]+")
_BLANKS = re.compile(r"(?:[ \t]+|\\\n)+")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ARRAY_START = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_REDIRECT = re.compile(r"([0-9]*)(<<<|<<-|<<|<>|<&|<|>>|>\||>&|>)|(&>>|&>)")
_OPERATOR = re.compile(r";;&|;;|;&|;|&&|&|\|\||\|&|\||\(|\)")
_SIMPLE_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                   "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"',
                   "?": "?"}  # fmt: skip
_OCTAL = re.compile(r"[0-7]{1,3}")
_HEX = {"x": re.compile(r"[0-9A-Fa-f]{1,2}"), "u": re.compile(r"[0-9A-Fa-f]{1,4}"),
        "U": re.compile(r"[0-9A-Fa-f]{1,8}")}  # fmt: skip
# Words after which the next word is a command again (so `((`, `#` and keywords count there).
_LEADS = {"if", "then", "else", "elif", "do", "while", "until", "{", "!", "time", "in"}
_OP_CHARS = frozenset(";&|()\n")


# ---- tokens ---------------------------------------------------------------------------------


@dataclass(eq=False, slots=True)
class Part:
    """One piece of a word: literal text ("lit"), a parameter ("var": `$X`, `${X:-y}`), a command
    substitution ("sub": its inner text and tokens), arithmetic ("arith") or an array literal
    ("array": `a=(x y)`, its inner tokens)."""

    kind: str
    text: str
    quoted: bool = False
    tokens: list | None = None
    nodes: list | None = None  # `tokens` parsed, once (see `nodes_of`)


@dataclass(eq=False, slots=True)
class Word:
    parts: list[Part]
    start: int = 0
    end: int = 0
    # Set by the command reader; read by the backstop (commands.py).
    consumed: set | None = None  # (action, tool) the reader checked through this word
    delegated: bool = False  # its text was read again elsewhere (bash -c, eval, a stdin...)
    flagged: bool = False  # already reported as "could not check"
    filled: set | None = None  # what the reader filled it in as (None in it: not known then)
    _value: str | None = None

    @property
    def value(self) -> str:
        """Quotes removed and escapes decoded; expansions as written (`$CMD`, `$(date)`)."""
        if self._value is None:
            parts = self.parts
            if len(parts) == 1 and parts[0].kind != "sub":
                self._value = parts[0].text
            else:
                self._value = "".join(
                    "$(" + part.text + ")" if part.kind == "sub" else part.text for part in parts
                )
        return self._value

    @property
    def literal(self) -> str | None:
        """The text when the word has no expansions, else None."""
        for part in self.parts:
            if part.kind != "lit":
                return None
        return self.value

    @property
    def reserved(self) -> str:
        """The word as a shell keyword: one unquoted literal piece, else ""."""
        parts = self.parts
        if len(parts) == 1 and parts[0].kind == "lit" and not parts[0].quoted:
            return parts[0].text
        return ""

    @property
    def quoted(self) -> bool:
        return any(part.quoted for part in self.parts)


@dataclass(eq=False, slots=True)
class Op:
    op: str


@dataclass(eq=False, slots=True)
class Heredoc:
    delimiter: str
    quoted: bool  # 'EOF', "EOF", E"O"F, \EOF: the body is taken as it is
    strip: bool  # <<-: leading tabs go
    body: str = ""
    closed: bool = False
    delegated: bool = False  # set by the reader: the body was read as commands or code
    consumed: set | None = None
    parts: list | None = None  # an unquoted body as the shell expands it (see `body_parts`)


@dataclass(eq=False, slots=True)
class Redirect:
    op: str  # >, >>, <, <<, <<-, <<<, <>, <&, >&, >|, &>, &>>
    fd: str
    target: Word | None = None
    heredoc: Heredoc | None = None


NEWLINE = "\n"
_NEWLINE_OP = Op(NEWLINE)  # shared: operators carry no marks


class _Lexer:
    __slots__ = ("src", "n", "i", "depth", "pending", "broken")

    def __init__(self, src: str, start: int = 0, depth: int = 0) -> None:
        self.src = src
        self.n = len(src)
        self.i = start
        self.depth = depth
        self.pending: list[Heredoc] = []
        self.broken = -1  # where an unclosed quote or substitution starts

    def _break(self, at: int) -> None:
        if self.broken < 0 or at < self.broken:
            self.broken = at

    def run(self, stop: bool = False) -> list:
        """Tokens up to the end, or (with `stop`) up to the `)` that closes a `$(`."""
        src, n = self.src, self.n
        out: list = []
        append = out.append
        paren = case = 0
        command = True  # at a command position
        test = regex = False  # inside `[[ ... ]]`; the next word is the regex after `=~`
        blanks_match, plain_match = _BLANKS.match, _PLAIN_WORD.match
        i = self.i
        while True:
            blanks = blanks_match(src, i)
            if blanks:
                i = blanks.end()
            if i >= n:
                self.i = i
                break
            char = src[i]
            if regex:  # [[ x =~ ^(#|$) ]]: `(`, `)`, `|` and what is inside `( )` are the regex
                regex = False
                if char not in "\n;&)#<>":
                    self.i = i
                    append(self._word(regex=True))
                    i = self.i
                    command = False
                    continue
            if char in _OP_CHARS and not (char == "&" and src.startswith(("&>", "&>>"), i)):
                if char == "\n":
                    append(_NEWLINE_OP)
                    i += 1
                    if self.pending:
                        self.i = i
                        self._bodies()
                        i = self.i
                    command = True
                    continue
                if stop and char == ")" and paren == 0 and case == 0:
                    self.i = i
                    return out
                if char == "(" and src.startswith("((", i):
                    end = _arith_end(src, i + 2, 2)
                    append(Word([Part("arith", src[i:end])], i, end))
                    i = end
                    command = False
                    continue
                operator = _OPERATOR.match(src, i)
                assert operator is not None
                op = operator.group()
                i = operator.end()
                if op == "(":
                    paren += 1
                elif op == ")" and paren:
                    paren -= 1
                append(Op(op))
                command = True
                continue
            if char == "#":  # at a token boundary, so a comment
                end = src.find("\n", i)
                i = n if end < 0 else end
                continue
            if char in "<>&" or char.isdigit():
                if src.startswith(("<(", ">("), i):
                    self.i = i
                    append(self._word())
                    i = self.i
                    command = False
                    continue
                redirect = _REDIRECT.match(src, i)
                if redirect:
                    self.i = i
                    append(self._redirect(redirect))
                    i = self.i
                    continue
            plain = plain_match(src, i)
            if plain:  # most words: plain text up to a blank or an operator
                text = plain.group()
                end = plain.end()
                append(Word([Part("lit", text)], i, end))
                i = end
                if stop and command and text == "case":
                    case += 1
                elif stop and command and text == "esac" and case:
                    case -= 1
                if command and text == "[[":
                    test = True
                elif test:
                    test, regex = text != "]]", text == "=~"
                command = text in _LEADS
                continue
            self.i = i
            word = self._word()
            i = self.i
            append(word)
            reserved = word.reserved
            if stop and command and reserved == "case":
                case += 1
            elif stop and command and reserved == "esac" and case:
                case -= 1
            if command and reserved == "[[":
                test = True
            elif test:
                test, regex = reserved != "]]", reserved == "=~"
            command = reserved in _LEADS
        if self.pending:
            for doc in self.pending:  # here-document with no newline after it: no body
                doc.closed = False
            self.pending = []
        if stop:
            self._break(self.i)
        return out

    def _bodies(self) -> None:
        src, n = self.src, self.n
        for doc in self.pending:
            start = self.i
            while self.i < n:
                newline = src.find("\n", self.i)
                end = n if newline < 0 else newline
                line = src[self.i : end]
                after = n if newline < 0 else newline + 1
                if (line.lstrip("\t") if doc.strip else line) == doc.delimiter:
                    doc.body = src[start : self.i]
                    doc.closed = True
                    self.i = after
                    break
                self.i = after
            else:
                doc.body = src[start:n]  # no end line: the rest of the text, as the shell does
        self.pending = []

    def _redirect(self, match: re.Match[str]) -> Redirect:
        fd, op = match.group(1) or "", match.group(2) or match.group(3)
        self.i = match.end()
        blanks = _BLANKS.match(self.src, self.i)
        if blanks:
            self.i = blanks.end()
        target = None
        if self.i < self.n and self.src[self.i] not in "\n;&|()<>":
            target = self._word()
        if op in {"<<", "<<-"} and target is not None:
            delimiter = "".join(part.text for part in target.parts)
            doc = Heredoc(delimiter, target.quoted, op == "<<-")
            self.pending.append(doc)
            return Redirect(op, fd, target, doc)
        return Redirect(op, fd, target)

    def _word(self, regex: bool = False) -> Word:
        """One word. `regex`: the right side of `=~` in `[[ ]]`, where bash keeps `|`, `(`, `)`
        and everything between the parentheses (blanks, `#`, `<`) in the word."""
        src, n = self.src, self.n
        start = i = self.i
        parts: list[Part] = []
        if src.startswith(("<(", ">("), i):  # process substitution: a command's output as a file
            i = self._substitution(i + 2, parts, False)
        depth = 0
        while i < n:
            char = src[i]
            if char in " \t\n;&|()<>":
                if not regex or (char == ")" and not depth):
                    break
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                elif char != "|" and not depth:
                    break
                parts.append(Part("lit", char))
                i += 1
                continue
            if char == "'":
                end = src.find("'", i + 1)
                if end < 0:
                    self._break(i)
                    parts.append(Part("lit", src[i + 1 :], True))
                    i = n
                    break
                parts.append(Part("lit", src[i + 1 : end], True))
                i = end + 1
            elif char == '"':
                i = self._dquote(i + 1, parts)
            elif char == "\\":
                if i + 1 >= n:
                    parts.append(Part("lit", "\\"))
                    i += 1
                elif src[i + 1] == "\n":
                    i += 2  # a line continuation inside a word
                else:
                    parts.append(Part("lit", src[i + 1], True))
                    i += 2
            elif char == "$":
                i = self._dollar(i, parts, False)
            elif char == "`":
                i = self._backtick(i, parts, False)
            else:
                plain = _PLAIN.match(src, i)
                assert plain is not None
                parts.append(Part("lit", plain.group()))
                i = plain.end()
        self.i = i
        if i < n and src[i] == "(" and parts and _is_array_start(parts):
            lexer = _Lexer(src, i + 1, self.depth + 1)
            inner = lexer.run(stop=True)
            self._merge(lexer)
            end = lexer.i
            parts.append(Part("array", src[i + 1 : end], False, inner))
            self.i = end + 1 if end < n else n
        return Word(_merged(parts), start, self.i)

    def _merge(self, lexer: _Lexer) -> None:
        if lexer.broken >= 0:
            self._break(lexer.broken)

    def _dquote(self, i: int, parts: list[Part]) -> int:
        src, n = self.src, self.n
        start = i - 1
        while i < n:
            char = src[i]
            if char == '"':
                return i + 1
            if char == "\\":
                follow = src[i + 1 : i + 2]
                if follow == "\n":
                    i += 2
                elif follow in {"$", "`", '"', "\\"} and follow:
                    parts.append(Part("lit", follow, True))
                    i += 2
                else:
                    parts.append(Part("lit", "\\", True))
                    i += 1
            elif char == "$":
                i = self._dollar(i, parts, True)
            elif char == "`":
                i = self._backtick(i, parts, True)
            else:
                plain = _DQ_PLAIN.match(src, i)
                assert plain is not None
                parts.append(Part("lit", plain.group(), True))
                i = plain.end()
        self._break(start)
        return n

    def _dollar(self, i: int, parts: list[Part], quoted: bool) -> int:
        src, n = self.src, self.n
        follow = src[i + 1 : i + 2]
        if follow == "'" and not quoted:
            end, text = _ansi_c(src, i + 2)
            if end > n:
                self._break(i)
            parts.append(Part("lit", text, True))
            return min(end, n)
        if follow == '"' and not quoted:  # $"..." (a translated string): a "..." string
            return self._dquote(i + 2, parts)
        if src.startswith("$((", i):
            end = _arith_end(src, i + 3, 2)
            parts.append(Part("arith", src[i:end], quoted))
            return end
        if follow == "(":
            return self._substitution(i + 2, parts, quoted)
        if follow == "[":
            end = _bracket_end(src, i + 2)
            parts.append(Part("arith", src[i:end], quoted))
            return end
        if follow == "{":
            end = self._brace_end(i + 2)
            parts.append(Part("var", src[i:end], quoted))
            return end
        if follow and (follow.isalpha() or follow == "_") and follow.isascii():
            name = _NAME.match(src, i + 1)
            assert name is not None
            parts.append(Part("var", "$" + name.group(), quoted))
            return name.end()
        if follow and follow in "@*#?$!-0123456789":
            parts.append(Part("var", src[i : i + 2], quoted))
            return i + 2
        parts.append(Part("lit", "$", quoted))
        return i + 1

    def _substitution(self, start: int, parts: list[Part], quoted: bool) -> int:
        if self.depth >= MAX_NEST:  # too deep: the text stays plain text
            parts.append(Part("lit", self.src[start - 2 : start], quoted))
            return start
        lexer = _Lexer(self.src, start, self.depth + 1)
        inner = lexer.run(stop=True)
        self._merge(lexer)
        end = lexer.i
        parts.append(Part("sub", self.src[start:end], quoted, inner))
        return end + 1 if end < self.n else self.n

    def _backtick(self, i: int, parts: list[Part], quoted: bool) -> int:
        src, n = self.src, self.n
        out: list[str] = []
        j = i + 1
        escapes = '$`\\"' if quoted else "$`\\"
        while j < n and src[j] != "`":
            if src[j] == "\\" and j + 1 < n and src[j + 1] in escapes:
                out.append(src[j + 1])
                j += 2
            else:
                out.append(src[j])
                j += 1
        if j >= n:
            self._break(i)
        inner = "".join(out)
        if self.depth >= MAX_NEST:
            parts.append(Part("lit", "`" + inner + "`", quoted))
        else:
            lexer = _Lexer(inner, 0, self.depth + 1)
            tokens = lexer.run()
            if lexer.broken >= 0:
                self._break(i)
            parts.append(Part("sub", inner, quoted, tokens))
        return min(j + 1, n)

    def _brace_end(self, i: int) -> int:
        """The end of `${...}` starting at `i` (after the `${`): nested braces, quotes and
        substitutions inside it are skipped whole."""
        src, n = self.src, self.n
        depth = 1
        start = i - 2
        while i < n:
            char = src[i]
            if char == "\\":
                i += 2
            elif char == "'":
                end = src.find("'", i + 1)
                i = n if end < 0 else end + 1
            elif char == '"':
                i = self._dquote(i + 1, [])
            elif char == "`":
                i = self._backtick(i, [], True)
            elif char == "$" and src.startswith("$(", i):
                i = self._substitution(i + 2, [], True)
            elif char == "$" and src.startswith("${", i):
                depth += 1
                i += 2
            elif char == "}":
                depth -= 1
                i += 1
                if depth == 0:
                    return i
            else:
                i += 1
        self._break(start)
        return n


def _is_array_start(parts: list[Part]) -> bool:
    if any(part.kind != "lit" or part.quoted for part in parts):
        return False
    return bool(_ARRAY_START.fullmatch("".join(part.text for part in parts)))


def _merged(parts: list[Part]) -> list[Part]:
    out: list[Part] = []
    for part in parts:
        last = out[-1] if out else None
        if last and part.kind == "lit" and last.kind == "lit" and last.quoted == part.quoted:
            last.text += part.text
        else:
            out.append(part)
    return out


def _arith_end(src: str, i: int, depth: int) -> int:
    n = len(src)
    while i < n:
        char = src[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _bracket_end(src: str, i: int) -> int:
    depth = 1
    n = len(src)
    while i < n:
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _ansi_c(src: str, i: int) -> tuple[int, str]:
    """Decode a `$'...'` string from just after its opening quote, the way bash does. Returns
    where it ends (past the closing quote; past the text when it has none) and its text."""
    n = len(src)
    out: list[str] = []
    while i < n:
        char = src[i]
        if char == "'":
            return i + 1, "".join(out)
        if char != "\\":
            plain = _ANSI_PLAIN.match(src, i)
            assert plain is not None
            out.append(plain.group())
            i = plain.end()
            continue
        letter = src[i + 1 : i + 2]
        if not letter:
            out.append("\\")
            i += 1
        elif letter in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[letter])
            i += 2
        elif letter in _HEX:
            digits = _HEX[letter].match(src, i + 2)
            if digits:
                code = int(digits.group(), 16)
                out.append(chr(code) if code < 0x110000 else "�")
                i = digits.end()
            else:
                out.append("\\" + letter)
                i += 2
        elif letter == "c" and i + 2 < n:
            out.append(chr(ord(src[i + 2]) & 0x1F))
            i += 3
        elif (digits := _OCTAL.match(src, i + 1)) is not None:
            out.append(chr(int(digits.group(), 8) & 0xFF))
            i = digits.end()
        else:
            out.append("\\" + letter)
            i += 2
    return n + 1, "".join(out)


def ansi_c(text: str) -> str:
    """The text a `$'...'` string's inside stands for (`npm\\x20publish` -> `npm publish`)."""
    return _ansi_c(text + "'", 0)[1]


@dataclass(eq=False, slots=True)
class Lexed:
    tokens: list
    broken: int  # where an unclosed quote or substitution starts; -1 when none


def lex(src: str) -> Lexed:
    """`src` as tokens. When a quote or substitution is never closed, the lines from the one it
    opens on are read again one at a time, so one bad line hides only itself."""
    lexer = _Lexer(src)
    tokens = lexer.run()
    if lexer.broken < 0:
        return Lexed(tokens, -1)
    line_start = src.rfind("\n", 0, lexer.broken) + 1
    head = _Lexer(src[:line_start])
    out = head.run()
    for line in src[line_start:].split("\n"):
        out += _Lexer(line).run()
        out.append(Op(NEWLINE))
    return Lexed(out, line_start)


def dq_parts(text: str) -> list[Part]:
    """The text of an unquoted here-document body as the shell expands it: like a "..." string
    (its `$X`, `$(...)` and backticks are expansions), with the quotes themselves plain text."""
    lexer = _Lexer(text)
    parts: list[Part] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\" and text[i + 1 : i + 2] in {"$", "`", "\\", "\n"}:
            if text[i + 1] != "\n":
                parts.append(Part("lit", text[i + 1], True))
            i += 2
        elif char == "$":
            i = lexer._dollar(i, parts, True)
        elif char == "`":
            i = lexer._backtick(i, parts, True)
        else:
            end = i + 1
            while end < n and text[end] not in "\\$`":
                end += 1
            parts.append(Part("lit", text[i:end], True))
            i = end
    return _merged(parts)


def body_parts(doc: Heredoc) -> list[Part]:
    """An unquoted here-document body's expansions, read once (so marks on them stay)."""
    if doc.parts is None:
        doc.parts = [] if doc.quoted else dq_parts(doc.body)
    return doc.parts


# ---- commands -------------------------------------------------------------------------------


@dataclass(eq=False, slots=True)
class Simple:
    words: list[Word]
    redirects: list[Redirect] = field(default_factory=list)
    consumed: set | None = None  # set by the reader: (action, tool) checked here


@dataclass(eq=False, slots=True)
class Compound:
    """`{ }`, `( )`, if, while, until, for, select, case, `(( ))`. `head`: for/select's name and
    words, case's word and patterns, the arithmetic. `bodies`: (commands, runs only sometimes)."""

    kind: str
    head: list[Word] = field(default_factory=list)
    bodies: list[tuple[list, bool]] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    closed: bool = True  # False when the text ends before its `}`, `fi`, `done`...


@dataclass(eq=False, slots=True)
class Function:
    name: Word
    body: object  # Compound or Simple
    record: object = None  # set by the reader


@dataclass(eq=False, slots=True)
class Pipeline:
    commands: list
    bang: bool = False


@dataclass(eq=False, slots=True)
class AndOr:
    pipelines: list[Pipeline]
    ops: list[str]
    background: bool = False


_CASE_ENDS = {";;", ";&", ";;&"}
_STRAY = frozenset({";", NEWLINE, "&", ")", "|", "||", "&&", "|&"} | _CASE_ENDS)
_AND_OR = frozenset({"&&", "||"})
_PIPES = frozenset({"|", "|&"})
_PIPELINE_WORDS = frozenset({"!", "time"})
_COMPOUND = frozenset({"{", "if", "while", "until", "for", "select", "case", "function", "[["})


class _Parser:
    __slots__ = ("tokens", "k", "depth")

    def __init__(self, tokens: list) -> None:
        self.tokens = tokens
        self.k = 0
        self.depth = 0

    def _peek(self, ahead: int = 0):
        k = self.k + ahead
        return self.tokens[k] if k < len(self.tokens) else None

    def _op(self, *ops: str) -> bool:
        token = self._peek()
        return isinstance(token, Op) and token.op in ops

    def _keyword(self, *names: str) -> bool:
        token = self._peek()
        return isinstance(token, Word) and token.reserved in names

    def _newlines(self) -> None:
        while self._op(NEWLINE):
            self.k += 1

    def _separators(self) -> None:
        while self._op(NEWLINE, ";"):
            self.k += 1

    def commands(self, stops: frozenset[str] = frozenset()) -> list[AndOr]:
        out: list[AndOr] = []
        tokens = self.tokens
        count = len(tokens)
        while self.k < count:
            token = tokens[self.k]
            if isinstance(token, Op):
                if token.op in stops:
                    break
                if token.op in _STRAY:
                    self.k += 1  # an empty command or a stray operator
                    continue
            elif isinstance(token, Word) and token.reserved in stops:
                break
            before = self.k
            item = self._and_or(stops)
            if self.k == before:
                self.k += 1
                continue
            out.append(item)
        return out

    def _and_or(self, stops: frozenset[str]) -> AndOr:
        pipelines = [self._pipeline(stops)]
        ops: list[str] = []
        while isinstance(token := self._peek(), Op) and token.op in _AND_OR:
            ops.append(token.op)
            self.k += 1
            self._newlines()
            pipelines.append(self._pipeline(stops))
        background = False
        if isinstance(token := self._peek(), Op) and token.op in {";", NEWLINE, "&"}:
            background = token.op == "&"
            self.k += 1
        return AndOr(pipelines, ops, background)

    def _pipeline(self, stops: frozenset[str]) -> Pipeline:
        bang = False
        while isinstance(token := self._peek(), Word) and token.reserved in _PIPELINE_WORDS:
            bang = bang or token.reserved == "!"
            self.k += 1
            while self._keyword("-p"):
                self.k += 1
        commands = [self._command(stops)]
        while isinstance(token := self._peek(), Op) and token.op in _PIPES:
            self.k += 1
            self._newlines()
            commands.append(self._command(stops))
        return Pipeline(commands, bang)

    def _redirects(self) -> list[Redirect]:
        out = []
        while isinstance(self._peek(), Redirect):
            out.append(self._peek())
            self.k += 1
        return out

    def _expect(self, *names: str) -> bool:
        if self._keyword(*names) or self._op(*names):
            self.k += 1
            return True
        return False

    def _command(self, stops: frozenset[str]):
        token = self._peek()
        if isinstance(token, Word) and token.reserved not in _COMPOUND:
            after = self._peek(1)
            if not (isinstance(after, Op) and after.op == "(") and (
                len(token.parts) != 1 or token.parts[0].kind != "arith"
            ):
                return self._simple(stops)  # most commands: a plain simple command
        if self.depth >= MAX_PARSE_NEST:
            return self._simple(stops)
        self.depth += 1
        try:
            if isinstance(token, Op) and token.op == "(":
                self.k += 1
                body = self.commands(frozenset({")"}))
                self._expect(")")
                return Compound("subshell", [], [(body, False)], self._redirects())
            if not isinstance(token, Word):
                return self._simple(stops)
            keyword = token.reserved
            if keyword == "{":
                self.k += 1
                body = self.commands(frozenset({"}"}))
                closed = self._expect("}")
                return Compound("group", [], [(body, False)], self._redirects(), closed)
            if keyword == "if":
                return self._if()
            if keyword in {"while", "until"}:
                self.k += 1
                condition = self.commands(frozenset({"do"}))
                self._expect("do")
                body = self.commands(frozenset({"done"}))
                self._expect("done")
                bodies = [(condition, False), (body, True)]
                return Compound(keyword, [], bodies, self._redirects())
            if keyword in {"for", "select"}:
                return self._for(keyword)
            if keyword == "case":
                return self._case()
            if keyword == "function":
                self.k += 1
                name = self._peek()
                if not isinstance(name, Word):
                    return Simple([token])
                self.k += 1
                if self._op("(") and isinstance(self._peek(1), Op) and self._peek(1).op == ")":
                    self.k += 2
                self._newlines()
                return Function(name, self._command(stops))
            if keyword == "[[":
                return self._test()
            if (
                _NAME_WORD.fullmatch(keyword or "-")
                and isinstance(self._peek(1), Op)
                and self._peek(1).op == "("
                and isinstance(self._peek(2), Op)
                and self._peek(2).op == ")"
            ):
                self.k += 3
                self._newlines()
                return Function(token, self._command(stops))
            if token.parts and token.parts[0].kind == "arith" and len(token.parts) == 1:
                self.k += 1
                return Compound("arith", [token], [], self._redirects())
            return self._simple(stops)
        finally:
            self.depth -= 1

    def _if(self) -> Compound:
        self.k += 1
        bodies: list[tuple[list, bool]] = [(self.commands(frozenset({"then"})), False)]
        self._expect("then")
        bodies.append((self.commands(frozenset({"elif", "else", "fi"})), True))
        while self._keyword("elif"):
            self.k += 1
            bodies.append((self.commands(frozenset({"then"})), True))
            self._expect("then")
            bodies.append((self.commands(frozenset({"elif", "else", "fi"})), True))
        if self._keyword("else"):
            self.k += 1
            bodies.append((self.commands(frozenset({"fi"})), True))
        self._expect("fi")
        return Compound("if", [], bodies, self._redirects())

    def _for(self, keyword: str) -> Compound:
        self.k += 1
        head: list[Word] = []
        if isinstance(self._peek(), Word):
            head.append(self._peek())
            self.k += 1
        self._newlines()
        if self._keyword("in"):
            self.k += 1
            while isinstance(self._peek(), Word):
                head.append(self._peek())
                self.k += 1
        self._separators()
        if self._keyword("{"):  # for x in a; { ...; }
            self.k += 1
            body = self.commands(frozenset({"}"}))
            self._expect("}")
        else:
            self._expect("do")
            body = self.commands(frozenset({"done"}))
            self._expect("done")
        return Compound(keyword, head, [(body, True)], self._redirects())

    def _case(self) -> Compound:
        self.k += 1
        head: list[Word] = []
        if isinstance(self._peek(), Word):
            head.append(self._peek())
            self.k += 1
        self._newlines()
        self._expect("in")
        bodies: list[tuple[list, bool]] = []
        while self.k < len(self.tokens):
            self._separators()
            if self._keyword("esac") or self.k >= len(self.tokens):
                break
            if self._op("("):
                self.k += 1
            while self.k < len(self.tokens) and not self._op(")"):
                token = self._peek()
                if isinstance(token, Word):
                    head.append(token)  # a pattern: text, not a command
                elif isinstance(token, Op) and token.op not in {"|"}:
                    break
                self.k += 1
            self._expect(")")
            bodies.append((self.commands(frozenset(_CASE_ENDS | {"esac"})), True))
            if self._op(*_CASE_ENDS):
                self.k += 1
        self._expect("esac")
        return Compound("case", head, bodies, self._redirects())

    def _test(self) -> Simple:
        """`[[ ... ]]`: its `&&`, `||`, `<` and `(` are part of the test."""
        words: list[Word] = []
        while self.k < len(self.tokens):
            token = self._peek()
            self.k += 1
            if isinstance(token, Word):
                words.append(token)
                if token.reserved == "]]":
                    break
            elif isinstance(token, Redirect) and token.target is not None:
                words.append(token.target)
            elif isinstance(token, Op) and token.op in {NEWLINE, ";", "&"}:
                self.k -= 1
                break
        return Simple(words, self._redirects())

    def _simple(self, stops: frozenset[str]) -> Simple:
        words: list[Word] = []
        redirects: list[Redirect] = []
        while self.k < len(self.tokens):
            token = self.tokens[self.k]
            if isinstance(token, Redirect):
                redirects.append(token)
            elif isinstance(token, Op) or (not words and token.reserved in stops):
                break
            else:
                words.append(token)
            self.k += 1
        return Simple(words, redirects)


_NAME_WORD = re.compile(r"[A-Za-z_][\w.:+-]*")


def parse(tokens: list) -> list[AndOr]:
    """The commands in `tokens` (from `lex`)."""
    return _Parser(tokens).commands()


def nodes_of(part: Part) -> list[AndOr]:
    """The commands inside a substitution or array part, parsed once."""
    if part.nodes is None:
        part.nodes = parse(part.tokens or [])
    return part.nodes


def walk_words(nodes: list[AndOr]):
    """Every word in a parsed text, in order, nested substitutions' words not included."""
    for and_or in nodes:
        for pipeline in and_or.pipelines:
            for command in pipeline.commands:
                yield from _command_words(command)


def command_words(command):
    """Every word of one command (a function body, a loop...), nested substitutions' not."""
    yield from _command_words(command)


def _command_words(command):
    if isinstance(command, Simple):
        yield from command.words
        for redirect in command.redirects:
            if redirect.target is not None:
                yield redirect.target
    elif isinstance(command, Compound):
        yield from command.head
        for body, _ in command.bodies:
            yield from walk_words(body)
        for redirect in command.redirects:
            if redirect.target is not None:
                yield redirect.target
    elif isinstance(command, Function):
        yield command.name
        yield from _command_words(command.body)
