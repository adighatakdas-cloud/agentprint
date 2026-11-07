"""Read PowerShell and cmd.exe command lines the way those shells split them.

On Windows, agents hand their commands to PowerShell (Codex, Cursor, Gemini CLI, Claude Code
without Git Bash) or to cmd.exe. There a backslash separates folders instead of escaping the next
character, so `cd C:\\pkg` goes to `C:\\pkg`, not to `C:pkg` as a POSIX reader would have it.
`commands` reads POSIX shell text with `shlex`; this module splits PowerShell and cmd text into
words and separators, and reads the Windows forms the guard has to follow:

- quoting: PowerShell `'...'` (literal, `''` for a quote), `"..."` (backtick escapes, `""`),
  here-strings, `#` and `<# #>` comments, backtick line ends; cmd `"..."`, `^` escapes, `rem`
  and `::` comments;
- separators `;` `&&` `||` `|` `&`, brackets `( )` `{ }`, redirections (`2>&1`, `*> $null`);
- program names as Windows finds them: `npm.cmd`, `"C:\\Program Files\\nodejs\\npm.cmd"`,
  `git.exe` and `NPM` are `npm` or `git` (`program_name`);
- `cmd /c "..."`, `powershell -Command "..."` (also `-EncodedCommand`, `-File`),
  `Start-Process` and `start`, and `cd /d`, `Set-Location -Path`/`-LiteralPath`, `pushd`;
- Windows markers (`looks_windows`): a command typed for PowerShell is read as one even when
  agentprint was not told that the agent runs on Windows.

Pure text functions: nothing here runs a program or reads a file.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

SH, POWERSHELL, CMD = "sh", "powershell", "cmd"  # the shells a command line can be read for

_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")  # what Windows adds to a program's name

# Text only a Windows shell is given. POSIX command lines have none of these, so they keep the
# POSIX reader byte for byte.
_MARKER = re.compile(
    r"(?<![\w/.-])[a-z]:[\\/]"  # C:\pkg, D:/pkg (a URL's s:// has a letter before it)
    r"|(?<![\w\\])\.{1,2}\\[\w.$%-]"  # .\release.ps1, ..\pkg
    r"|(?<![\w\\])\\\\\w"  # \\server\share
    r"|\$env:\w|%~dp0"  # $env:NPM_TOKEN, a batch file's own folder
    r"|(?<![\w-])(?:set-location|push-location|pop-location|start-process|invoke-expression"
    r"|invoke-command|get-childitem|foreach-object|write-host|write-output|test-path|join-path"
    r"|split-path|remove-item|copy-item|new-item)(?![\w-])"
    r"|(?<![\w.-])[\w.-]+\.ps1(?![\w.-])"  # a PowerShell script
    r"|(?:^|[;&|(){}\n]|\bcall\s|\bstart\s)\s*[\"']?(?:[^\s\"';&|(){}]*[\\/])?[\w.-]+"
    r"\.(?:exe|cmd|bat|com)(?=[\s\"';&|()]|$)"  # npm.cmd, git.exe where a command starts
    r"|(?<![\w.-])cmd(?:\.exe)?\s+/[a-z]"  # cmd /c
    r"|(?<![\w.-])(?:powershell|pwsh)(?:\.exe)?(?![\w.-])"
    r"|(?<![\w-])(?:cd|chdir)\s+/d(?!\w)",  # cmd's cd /d
    re.I,
)
_UNQUOTED_BACKSLASH = re.compile(r"[\w.~*-]\\[\w.*$%-]")  # dist\*, packages\web outside quotes
_POSIX_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_PROGRAM_SUFFIX = re.compile(r"(?<=\w)\.(?:exe|cmd|bat|com|ps1)(?![\w.-])", re.I)


def looks_windows(text: str) -> bool:
    """True when `text` has something only a Windows shell is given: a drive letter or `.\\`
    path, a backslash path outside quotes, `$env:`, a PowerShell cmdlet such as `Set-Location`,
    a `.ps1` script, a program such as `npm.cmd` or `git.exe`, `cmd /c`, `powershell`/`pwsh`,
    or `cd /d`."""
    if _MARKER.search(text):
        return True
    return bool(_UNQUOTED_BACKSLASH.search(_POSIX_QUOTED.sub(" ", text)))


def program_name(word: str) -> str:
    """The program a command word runs, as Windows finds it: its file name in lower case without
    `.exe`, `.cmd`, `.bat`, `.com` or `.ps1`. `C:\\x\\npm.cmd`, `NPM` and `npm.ps1` are `npm`."""
    name = re.split(r"[\\/]", word)[-1].lower()
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def slashes(word: str) -> str:
    """A Windows path with forward slashes, which Windows and pathlib read the same way."""
    return word.replace("\\", "/")


def plain(text: str) -> str:
    """Text for a word scan: lower case, forward slashes, `npm.cmd` written `npm`."""
    return _PROGRAM_SUFFIX.sub("", text).replace("\\", "/").lower()


# ---- splitting into words -------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    text: str
    op: bool = False  # a separator or bracket (; && || | & ( ) { }) or a line end ("\n")


@dataclass
class Lexed:
    tokens: list[Token] = field(default_factory=list)
    inner: list[str] = field(default_factory=list)  # the code of each "...$(...)..." (it runs)
    complete: bool = True  # False when a quote, comment or here-string is never closed


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "a": "\a", "b": "\b", "f": "\f",
            "v": "\v", "e": "\x1b"}  # fmt: skip
_STREAMS = {"1", "2", "3", "4", "5", "6", "*"}  # 2> and *> redirect a stream
_BLANK = " \t\r\f\v"
_HERE = re.compile(r"@(['\"])[ \t]*\r?\n")  # a here-string starts: @' or @" and a line end
_REM = re.compile(r"@?rem(?=\s|$)|:", re.I)  # cmd: a comment or a label line


def lex(text: str, shell: str) -> Lexed:
    """`text` split into words and separators as PowerShell (`shell` is POWERSHELL) or cmd.exe
    splits it. Quotes are taken off; redirections and comments are dropped."""
    return _Lexer(text, shell == POWERSHELL).run()


class _Lexer:
    def __init__(self, text: str, ps: bool) -> None:
        self.text, self.ps = text, ps
        self.out = Lexed()
        self.i = 0
        self.word: list[str] = []
        self.in_word = False
        self.target = False  # the next word is a redirection's file, not an argument

    def run(self) -> Lexed:
        text, n = self.text, len(self.text)
        while self.i < n:
            ch = text[self.i]
            here = _HERE.match(text, self.i) if self.ps and ch == "@" and not self.in_word else None
            if ch in _BLANK:
                self._end()
                self.i += 1
            elif ch == "\n":
                self._op("\n", 1)
            elif self._comment():
                pass
            elif ch == ("`" if self.ps else "^") or (
                self.ps and ch == "\\" and self._line_end(self.i + 1) is not None
            ):  # an escape, or a line that goes on (a POSIX `\` too, as Git Bash users type it)
                self._escape()
            elif ch == "'" and self.ps:
                self._single()
            elif ch == '"':
                self._double()
            elif here is not None:
                self._here(here)
            elif ch in "&|":
                double = text.startswith(ch * 2, self.i)
                self._op(ch * 2 if double else ch, 2 if double else 1)
            elif ch == ";" and self.ps:
                self._op(";", 1)
            elif ch == ";":  # cmd: ; separates arguments, like a space
                self._end()
                self.i += 1
            elif ch in "()" or (ch in "{}" and self.ps and not self._braced_variable()):
                self._op(ch, 1)
            elif ch in "<>":
                self._redirect()
            elif ch == "{" and self.ps:
                self._braced()
            else:
                self._char(ch)
                self.i += 1
        self._end()
        return self.out

    def _char(self, ch: str) -> None:
        self.word.append(ch)
        self.in_word = True

    def _end(self) -> None:
        if self.in_word:
            if self.target:
                self.target = False
            else:
                self.out.tokens.append(Token("".join(self.word)))
        self.word, self.in_word = [], False

    def _op(self, op: str, width: int) -> None:
        self._end()
        self.target = False
        self.out.tokens.append(Token(op, True))
        self.i += width

    def _line_end(self, j: int) -> int | None:
        """The index after a line end at `j` (\\n or \\r\\n); None when there is none."""
        if self.text.startswith("\r\n", j):
            return j + 2
        if self.text.startswith("\n", j):
            return j + 1
        return None

    def _eol(self, j: int) -> int:
        end = self.text.find("\n", j)
        return len(self.text) if end < 0 else end

    def _comment(self) -> bool:
        """Skip a comment that starts here; True when there is one."""
        if self.in_word:
            return False
        text, i = self.text, self.i
        if self.ps:
            if text.startswith("<#", i):
                end = text.find("#>", i + 2)
                if end < 0:
                    self.out.complete = False
                self.i = len(text) if end < 0 else end + 2
                return True
            if text[i] == "#":
                self.i = self._eol(i)
                return True
            return False
        tokens = self.out.tokens
        if (not tokens or tokens[-1].op) and _REM.match(text, i):
            self.i = self._eol(i)
            return True
        return False

    def _escape(self) -> None:
        after = self._line_end(self.i + 1)
        if after is not None:  # the command goes on on the next line
            self.i = after
        elif self.i + 1 < len(self.text):
            self._char(self.text[self.i + 1])  # outside quotes, `n is just n
            self.i += 2
        else:
            self.i += 1

    def _single(self) -> None:
        text, n = self.text, len(self.text)
        j = self.i + 1
        self.in_word = True
        while j < n:
            if text[j] == "'":
                if not text.startswith("''", j):
                    self.i = j + 1
                    return
                self.word.append("'")
                j += 2
            else:
                self.word.append(text[j])
                j += 1
        self.out.complete = False
        self.i = n

    def _double(self) -> None:
        text, n = self.text, len(self.text)
        j = self.i + 1
        self.in_word = True
        while j < n:
            ch = text[j]
            if ch == '"':
                if not (self.ps and text.startswith('""', j)):
                    self.i = j + 1
                    return
                self.word.append('"')
                j += 2
            elif self.ps and ch == "`" and j + 1 < n:
                self.word.append(_ESCAPES.get(text[j + 1], text[j + 1]))
                j += 2
            elif self.ps and text.startswith("$(", j):
                end = _close(text, j + 1)
                if end is None:
                    break
                self.out.inner.append(text[j + 2 : end - 1])
                self.word.append(text[j:end])
                j = end
            else:
                self.word.append(ch)
                j += 1
        self.out.complete = False
        self.i = n

    def _here(self, start: re.Match[str]) -> None:
        """@'...'@ or @"..."@: the text between the lines that open and close it."""
        quote = start.group(1)
        close = re.compile(r"\r?\n" + quote + "@").search(self.text, start.end() - 1)
        if close is None:
            body, self.i = self.text[start.end() :], len(self.text)
            self.out.complete = False
        else:
            body, self.i = self.text[start.end() : max(start.end(), close.start())], close.end()
        if quote == '"':
            self.out.inner += _subexpressions(body)
        self.word.append(body)
        self.in_word = True

    def _braced_variable(self) -> bool:
        return self.text[self.i] == "{" and self.in_word and self.word[-1:] == ["$"]

    def _braced(self) -> None:
        """${name}: one word, braces and all."""
        end = self.text.find("}", self.i)
        if end < 0:
            self.out.complete = False
            end = len(self.text) - 1
        self.word.append(self.text[self.i : end + 1])
        self.in_word = True
        self.i = end + 1

    def _redirect(self) -> None:
        """> file, >> file, 2> file, 2>&1, *> $null, < file: dropped, with the file."""
        text = self.text
        if self.in_word and "".join(self.word) in _STREAMS:
            self.word, self.in_word = [], False  # 2> or *>: the number belongs to the operator
        self._end()
        j = self.i + 1
        if text[self.i] == ">" and text.startswith(">", j):
            j += 1
        if text.startswith("&", j):  # into another stream: no file follows
            j += 1
            while j < len(text) and text[j].isdigit():
                j += 1
        else:
            self.target = True
        self.i = j


def _close(text: str, start: int) -> int | None:
    """The index after the `)` that closes the `(` at `start`, skipping quoted text."""
    depth, j, n = 0, start, len(text)
    while j < n:
        ch = text[j]
        if ch == "`":
            j += 2
            continue
        if ch == "'":
            end = text.find("'", j + 1)
            if end < 0:
                return None
            j = end + 1
            continue
        if ch == '"':
            j += 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "`" else 1
            if j >= n:
                return None
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


def _subexpressions(text: str) -> list[str]:
    """The code of each $(...) in an expanding string."""
    found: list[str] = []
    j = text.find("$(")
    while j >= 0:
        end = _close(text, j + 1) if j == 0 or text[j - 1] != "`" else None
        if end is not None:
            found.append(text[j + 2 : end - 1])
        j = text.find("$(", end or j + 2)
    return found


def unquoted(text: str, shell: str) -> str:
    """`text` with its quoted strings and comments each turned into a space: what a word scan
    may look at. An escaped character becomes `_`, so it is never a separator."""
    ps = shell == POWERSHELL
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        starts_word = i == 0 or text[i - 1] in " \t\r\n;&|(){}"
        here = _HERE.match(text, i) if ps and ch == "@" else None
        if ch == ("`" if ps else "^"):
            line_end = text.startswith("\n", i + 1) or text.startswith("\r\n", i + 1)
            out.append(" " if line_end else "_")
            i += 3 if text.startswith("\r\n", i + 1) else 2
        elif ps and starts_word and text.startswith("<#", i):
            end = text.find("#>", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif ps and starts_word and ch == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif here is not None:
            close = re.compile(r"\r?\n" + here.group(1) + "@").search(text, here.end() - 1)
            i = n if close is None else close.end()
            out.append(" ")
        elif ps and ch == "'":
            j = i + 1
            while j < n and (text[j] != "'" or text.startswith("''", j)):
                j += 2 if text[j] == "'" else 1
            i = j + 1
            out.append(" ")
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if ps and text[j] == "`" else 1
            i = j + 1
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---- what Windows programs run --------------------------------------------------------------


def _param(name: str, names: tuple[str, ...], aliases: dict[str, str]) -> str | None:
    """A PowerShell parameter as typed (`co`, `LiteralPath`) to its full name: an alias, the
    name itself, or the one name it starts. None when unknown or ambiguous."""
    name = name.lower()
    if name in aliases:
        return aliases[name]
    if name in names:
        return name
    matches = [full for full in names if full.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def cmd_script(args: list[str]) -> str | None:
    """The command in `cmd [/d /s /q ...] /c <command>` (or /k, /r; Git Bash users type //c).
    None when cmd starts without one."""
    strip = False
    for i, arg in enumerate(args):
        if not arg.startswith("/"):
            return None
        body = arg.lstrip("/")
        if body.lower() == "s":
            strip = True
        elif body[:1].lower() in ("c", "k", "r"):
            rest = [body[1:]] if body[1:] else []
            return _cmd_line([*rest, *args[i + 1 :]], strip)
    return None


def _cmd_line(words: list[str], strip: bool) -> str:
    """The command line cmd /c runs, from its words: quoted again as PowerShell passes them
    (`"My title"`, `""`), then the outer quotes taken off as cmd does, unless (without /s) the
    only two quotes wrap a program's path: `"C:\\Program Files\\nodejs\\npm.cmd" publish`."""
    line = " ".join(f'"{w}"' if not w or any(ch.isspace() for ch in w) else w for w in words)
    if not line.startswith('"'):
        return line
    last = line.rfind('"')
    inside = line[1:last]
    kept = (
        not strip
        and line.count('"') == 2
        and not any(ch in inside for ch in "&<>()@^|")
        and any(ch.isspace() for ch in inside)
        and inside.lower().endswith((".exe", ".cmd", ".bat", ".com"))
    )
    return line if kept else line[1:last] + line[last + 1 :]


_PS_VALUES = ("command", "commandwithargs", "configurationfile", "configurationname",
              "custompipename", "encodedarguments", "encodedcommand", "executionpolicy", "file",
              "inputformat", "outputformat", "psconsolefile", "settingsfile", "version",
              "windowstyle", "workingdirectory")  # fmt: skip
_PS_FLAGS = ("help", "interactive", "login", "mta", "namedpipeservermode", "noexit", "nologo",
             "noninteractive", "noprofile", "noprofileloadtime", "sta")  # fmt: skip
_PS_ALIASES = {"c": "command", "cwa": "commandwithargs", "e": "encodedcommand",
               "ec": "encodedcommand", "ea": "encodedarguments", "ep": "executionpolicy",
               "ex": "executionpolicy", "f": "file", "if": "inputformat", "i": "interactive",
               "l": "login", "nol": "nologo", "noni": "noninteractive", "nop": "noprofile",
               "noe": "noexit", "o": "outputformat", "of": "outputformat", "v": "version",
               "w": "windowstyle", "wd": "workingdirectory", "?": "help", "h": "help"}  # fmt: skip


@dataclass(frozen=True)
class PowerShellRun:
    """What `powershell` or `pwsh` runs, given its arguments."""

    code: str | None = None  # -Command, or what -EncodedCommand decodes to
    file: str | None = None  # -File (pwsh's first plain argument too)
    folder: str = ""  # -WorkingDirectory
    exact: bool = True  # False for -CommandWithArgs: its arguments fill the code in


def _powershell_param(arg: str) -> str | None:
    """A parameter's full name; "?" for one agentprint does not know; None for a plain word."""
    if len(arg) < 2 or arg[0] not in "-/" or arg == "--":
        return None
    name = arg.lstrip("-/").lower()  # PowerShell takes -Command, --Command and /Command
    found = _param(name, (*_PS_VALUES, *_PS_FLAGS), _PS_ALIASES)
    for runs_code in ("command", "encodedcommand"):  # -co, -enc: read as the one that runs code
        if found is None and runs_code.startswith(name):
            found = runs_code
    if found is None and arg[0] == "/":
        return None  # a path such as /home/me/release.ps1
    return found or "?"


def powershell_run(program: str, args: list[str]) -> PowerShellRun:
    """What `powershell.exe` (`program` "powershell") or `pwsh` runs with these arguments."""
    folder = ""
    i = 0
    while i < len(args):
        arg = args[i]
        name = _powershell_param(arg)
        value = args[i + 1] if i + 1 < len(args) else ""
        if name is None:  # the first plain word: code for powershell.exe, a script for pwsh
            if program == "pwsh":
                return PowerShellRun(file=arg, folder=folder)
            return PowerShellRun(code=" ".join(args[i:]), folder=folder)
        if name in ("command", "commandwithargs"):
            return PowerShellRun(" ".join(args[i + 1 :]), folder=folder, exact=name == "command")
        if name == "file":
            return PowerShellRun(file=value or None, folder=folder)
        if name == "encodedcommand":
            return PowerShellRun(_decode(value), folder=folder)
        if name == "workingdirectory":
            folder = value
        i += 2 if name in _PS_VALUES else 1
    return PowerShellRun(folder=folder)  # an interactive shell, or code from stdin


def _decode(value: str) -> str | None:
    """-EncodedCommand: base64 of UTF-16LE text. None when it isn't (PowerShell refuses it)."""
    try:
        return base64.b64decode(value, validate=True).decode("utf-16-le")
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
        return None


_START_VALUES = ("argumentlist", "credential", "environment", "filepath", "redirectstandarderror",
                 "redirectstandardinput", "redirectstandardoutput", "verb", "windowstyle",
                 "workingdirectory")  # fmt: skip
_START_FLAGS = ("loaduserprofile", "nonewwindow", "passthru", "usenewenvironment", "wait")
_START_ALIASES = {"args": "argumentlist", "lup": "loaduserprofile", "nnw": "nonewwindow",
                  "path": "filepath", "pspath": "filepath", "rse": "redirectstandarderror",
                  "rsi": "redirectstandardinput", "rso": "redirectstandardoutput",
                  "runas": "credential"}  # fmt: skip


def _split_list(values: list[str]) -> list[str]:
    """A PowerShell list of arguments ('publish','--tag','next') or one string of them."""
    return [part for value in values for part in re.split(r"[,\s]+", value) if part]


def start_process(args: list[str], shell: str) -> tuple[list[str], str] | None:
    """`Start-Process` (PowerShell; `start` and `saps` too) or cmd's `start`: the program and
    its arguments, and the folder it starts in. None when agentprint can't tell (a parameter
    it does not know, or no program)."""
    if shell == CMD:
        return _cmd_start(args)
    program, argv, folder, plain_words = "", [], "", []
    i = 0
    while i < len(args):
        arg = args[i]
        listed = bool(plain_words) and plain_words[-1].endswith(",")  # 'publish', '--tag'
        if len(arg) < 2 or not arg.startswith("-") or listed:
            plain_words.append(arg)
            i += 1
            continue
        name = _param(arg[1:], (*_START_VALUES, *_START_FLAGS), _START_ALIASES)
        if name is None:
            return None
        value = args[i + 1] if i + 1 < len(args) else ""
        if name == "argumentlist":
            values = args[i + 1 : i + 2]
            i += 2
            while values and values[-1].endswith(",") and i < len(args):
                values.append(args[i])
                i += 1
            argv = _split_list(values)
            continue
        if name == "filepath":
            program = value
        elif name == "workingdirectory":
            folder = value
        i += 2 if name in _START_VALUES else 1
    if not program and plain_words:
        program = plain_words.pop(0)
    if not argv and plain_words:
        argv = _split_list(plain_words)
    return ([program, *argv], folder) if program else None


def _cmd_start(args: list[str]) -> tuple[list[str], str] | None:
    """cmd's `start ["title"] [/d folder] [/wait /b /min ...] program [args]`."""
    folder, i = "", 0
    if args and (not args[0] or any(ch.isspace() for ch in args[0])):
        i = 1  # a quoted first word is the window's title: start "" npm publish
    while i < len(args) and args[i].startswith("/"):
        flag = args[i][1:].lower()
        if flag == "d":
            folder = args[i + 1] if i + 1 < len(args) else ""
            i += 1
        elif flag.startswith("d"):
            folder = args[i][2:]  # /Dfolder
        elif flag in ("node", "affinity"):
            i += 1
        i += 1
    return (args[i:], folder) if i < len(args) else None


@dataclass(frozen=True)
class Cd:
    """Where a `cd`, `Set-Location` or `pushd` goes, as typed."""

    folder: str | None  # None when no folder is named
    drive: bool = True  # it can change drive (cmd's cd only with /d)
    exact: bool = True  # False when PowerShell stops with an error (`cd /d D:\\x`, two folders)


_CD_NAMES = ("literalpath", "passthru", "path", "stackname")
_CD_ALIASES = {"lp": "literalpath", "pspath": "literalpath"}


def cd_args(args: list[str], shell: str, program: str) -> Cd:
    """The folder in `cd [/d] folder` (cmd, which takes spaces unquoted), or PowerShell's
    `Set-Location [-Path|-LiteralPath] folder` and its aliases (cd, sl, chdir, pushd)."""
    if shell == CMD:
        rest = [arg for arg in args if arg.lower() != "/d"]
        return Cd(" ".join(rest) or None, program == "pushd" or len(rest) < len(args))
    if args[:1] and args[0].lower() == "/d":  # cmd's cd /d, which PowerShell rejects
        return Cd(" ".join(args[1:]) or None, exact=False)
    folder: str | None = None
    exact = True
    i = 0
    while i < len(args):
        arg = args[i]
        if len(arg) > 1 and arg.startswith("-"):
            name = _param(arg[1:], _CD_NAMES, _CD_ALIASES)
            if name in ("path", "literalpath"):
                folder = args[i + 1] if i + 1 < len(args) else None
                i += 2
                continue
            if name == "stackname":
                i += 2
                continue
            exact = exact and name == "passthru"
        elif folder is None:
            folder = arg
        else:
            exact = False  # a second folder: PowerShell stops with an error
        i += 1
    return Cd(folder, exact=exact)


_PS_ROOT = re.compile(r"\$(?:\{PSScriptRoot\}|PSScriptRoot\b)", re.I)
_CMD_ROOT = re.compile(r"%~dp0", re.I)
_PS_SPECIAL = re.compile(r"([\s`$\"'(){};,&|@#<>\[\]])")


def own_folder(text: str, folder: str, shell: str) -> str:
    """A script with its own folder written in: PowerShell's `$PSScriptRoot`, cmd's `%~dp0`
    (which ends in a separator). `folder` has forward slashes; for PowerShell its spaces and
    special characters are backtick-escaped, so it stays one word in or out of quotes."""
    if shell == POWERSHELL:
        escaped = _PS_SPECIAL.sub(r"`\1", folder)
        return _PS_ROOT.sub(lambda _: escaped, text)
    return _CMD_ROOT.sub(lambda _: folder + "/", text)
