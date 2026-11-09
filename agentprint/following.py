"""Follow the files a command hands its work to: shell scripts, Makefiles, justfiles, Taskfiles,
and scripts in other languages.

`commands` reads the command line; for a command that runs a file it asks this module, which
reads the file (inside the folder of the agent's command, text only) and sends shell text of up
to 64 KB back through `commands.detect`. The two call each other, so this module looks
`commands` names up when it runs, never at import.

Never cry wolf, never a silent OK. A file over 64 KB is scanned in chunks (up to 8 MB) for a
publish or push word, and only a match means "could not check"; so does a file over 8 MB or one
that can't be opened, but only when the command or the file name looks like a release. Where
following stops (`commands.MAX_DEPTH` levels, `MAX_STEPS` commands or `MAX_BYTES` of text per
command line) the text is read once more without following anything: its own commands are
checked, and the scripts, recipes and package scripts it runs are only scanned for a publish or
push.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import stat
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePath

from . import commands as c
from . import recipes

MAX_FILE_BYTES = 64 * 1024  # a file up to this size is read whole and its commands followed
MAX_SCAN_BYTES = 8 * 1024 * 1024  # a larger one is only scanned for publish words, up to this
MAX_STEPS = 400  # file commands followed per command line; past it they are looked at once more
MAX_BYTES = 1024 * 1024  # text from files followed per command line; past it, only word-scanned
MAKES = {"make", "gmake"}
TASKS = {"task", "go-task"}
# Programs that run a script file in another language: (options that take inline code,
# options that take some other value). Their scripts are scanned for publish words, not run.
INTERPRETERS: dict[str, tuple[set[str], set[str]]] = {
    "python": ({"-c"}, {"-W", "-X", "-Q"}),
    "node": ({"-e", "--eval", "-p", "--print"}, {"-r", "--require", "--import", "--loader",
             "--experimental-loader", "--input-type", "-C", "--conditions", "--title"}),
    "tsx": ({"-e", "--eval", "-p", "--print"}, {"--tsconfig", "-r", "--require", "--import"}),
    "ts-node": ({"-e", "--eval", "-p", "--print"}, {"-P", "--project", "-r", "--require", "-C",
                "--compiler", "-O", "--compiler-options", "--cwd"}),
    "deno": (set(), {"-c", "--config", "--import-map", "--lock", "--location", "--seed",
             "--cert", "--env-file"}),  # `deno eval <code>` and `deno run <file>`
    "ruby": ({"-e"}, {"-r", "-I", "-C", "-E"}),
    "perl": ({"-e", "-E"}, {"-I", "-M", "-m"}),
}  # fmt: skip
_MAKE_VALUES = {"-C", "--directory", "-f", "--file", "--makefile", "-I", "--include-dir", "-o",
                "--old-file", "--assume-old", "-W", "--what-if", "--new-file", "--assume-new",
                "-E", "--eval"}  # fmt: skip
_MAKE_NO_RUN = {"-n", "--just-print", "--dry-run", "--recon", "-q", "--question", "-t",
                "--touch", "-v", "--version", "-h", "--help"}  # fmt: skip
_MAKE_COUNTS = {"-j", "--jobs", "-l", "--load-average", "--max-load"}  # an optional number
_JUST_VALUES = {"-f", "--justfile", "-d", "--working-directory", "--shell", "--shell-arg",
                "--color", "--dotenv-filename", "--dotenv-path", "--chooser", "--command-color",
                "--list-heading", "--list-prefix", "--timestamp-format", "--tempdir",
                "--alias-style", "--ceiling"}  # fmt: skip
_JUST_NO_RUN = {"-l", "--list", "--summary", "-s", "--show", "--dump", "--evaluate",
                "--variables", "--fmt", "-e", "--edit", "--init", "--completions", "--groups",
                "--man", "-h", "--help", "-V", "--version", "--changelog", "--choose", "-n",
                "--dry-run", "--check"}  # fmt: skip
_TASK_VALUES = {"-t", "--taskfile", "-d", "--dir", "-o", "--output", "-C", "--concurrency",
                "--interval", "--sort", "--output-group-begin", "--output-group-end"}  # fmt: skip
_TASK_NO_RUN = {"-n", "--dry", "-l", "--list", "-a", "--list-all", "--summary", "-i", "--init",
                "--status", "-h", "--help", "--version", "--completion"}  # fmt: skip
# Looked for in the command's folder and the folders above it (up to the agent's folder), any
# case, first name first: the way just and task look for them.
JUSTFILES = ("justfile", ".justfile")
TASKFILES = ("taskfile.yml", "taskfile.yaml", "taskfile.dist.yml", "taskfile.dist.yaml")
MAKEFILES = ("GNUmakefile", "makefile", "Makefile")  # the order make looks for them
MAX_INCLUDES = 16  # makefiles or justfiles read for one make or just command
_SHELL_VALUES = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
# A publish or push named in a file agentprint does not read as commands (Python, JS, a
# Taskfile). Tool word, then its verb within a few characters: `["npm", "publish"]`,
# `execSync('git push --tags')`, `git.push()`, `twine.commands.upload`. Bare words like `push`
# or `np` are too common in ordinary code (`list.push(x)`, `import numpy as np`) to count by
# themselves, and so is a method call near the tool word (`git ... summary.push(x)`).
_NAMED_PUBLISH = re.compile(
    r"(?<![\w.-])(?:(npm|pnpm|yarn|bun|uv|poetry|hatch|flit|changeset|lerna)"
    r"(?:\.|(?![\w.-]).{0,40}?(?<![\w./-]))publish(?![\w:-])"
    r"|(twine)(?![\w-]).{0,40}?(?<![\w-])upload(?![\w-])"
    r"|(git)(?:\.|(?![\w./-]).{0,40}?(?<![\w./-]))push(?![\w-])"
    r"|(semantic-release|release-it)(?![\w.-])"
    r"|npx\s+(np)(?![\w.-]))",
    re.S,
)


# In a file too large to follow: a publish, upload or push word on its own (`"$NPM" publish`).
_BARE_PUBLISH = re.compile(r"(?<![\w./$-])(publish|upload|push)(?![\w.-])")
# Does a command line or file name look like a release? Then a file agentprint could not open
# (or one over 8 MB) means "could not check"; otherwise it is left alone.
_RELEASE_WORD = re.compile(
    r"(?<![a-z])(?:publish|push|upload|release|deploy|ship|twine|changesets?|lerna|release-it"
    r"|semantic-release)(?![a-z])",
    re.I,
)
# A script's own folder, as scripts spell it: $(dirname "$0"), ${BASH_SOURCE%/*},
# $(dirname -- "$(realpath "$0")"), `dirname $0`, and the folder made absolute:
# $(cd "$(dirname "$0")" && pwd), and
# $( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd ).
_SELF = r"\$(?:0|\{0\}|\{BASH_SOURCE(?:\[0\])?\}|BASH_SOURCE(?:\[0\])?)"
_REAL = rf"\$\(\s*(?:realpath|g?readlink\s+-[fe])\s+(?:--\s+)?(?:\"{_SELF}\"|{_SELF})\s*\)"
_ME = rf"(?:\"{_SELF}\"|{_SELF}|\"{_REAL}\"|{_REAL})"
_DIRNAME = rf"\$\(\s*dirname\s+(?:--\s+)?{_ME}\s*\)|`\s*dirname\s+(?:--\s+)?\"?{_SELF}\"?\s*`"
_STRIP = r"\$\{(?:0|BASH_SOURCE(?:\[0\])?)%/\*\}"
_QUIET = r"(?:\s*(?:&>|[12]?>>?)\s*/dev/null|\s*2>&1)*"  # &> /dev/null, >/dev/null 2>&1
_OWN_FOLDER = re.compile(
    rf"\$\(\s*cd\s+(?:-[PL]\s+)?(?:--\s+)?(?:\"(?:{_DIRNAME}|{_STRIP})\"|{_DIRNAME}|{_STRIP})"
    rf"{_QUIET}\s*(?:&&|;)\s*pwd(?:\s+-P)?\s*\)"
    rf"|{_DIRNAME}|{_STRIP}"
)

# Why agentprint could not check something, after "could not check `<label>`: ".
_CODE = "it runs `{what}` from code agentprint does not read as commands"
_TASK = "it runs `{what}` from a Taskfile task, which agentprint does not read as commands"
_DEEP = "it runs `{what}` deeper in its scripts than agentprint follows"
_LARGE = "it is larger than 64 KB and names `{what}`, so agentprint did not follow it"
_NESTED = "it is nested deeper in scripts than agentprint follows"
_HUGE = "it is larger than 8 MB, so agentprint did not read it"
_UNREADABLE = "agentprint could not open it"
_UNREAD_INCLUDE = "it may run a recipe from a {kind} agentprint did not read"

Hit = tuple["c.Action", str]  # a publish or push named in text: (action, tool)


@dataclass(frozen=True)
class _File:
    path: Path  # symlinks resolved
    name: str  # as shown to the agent: relative to the folder its command runs in
    text: str | None  # the whole text when it is at most 64 KB; else None, and `size` says why
    size: str = ""  # "large" (scanned in chunks), "huge" (over 8 MB) or "unreadable"
    named: tuple[Hit, ...] = ()  # a large file: the publishes and pushes it names
    bare: str = ""  # a large file: the first publish/upload/push word on its own
    first_line: str = ""  # a large file: its shebang, if any


@dataclass
class _Reading:
    """The files one command line runs: the folder they must stay inside, budgets, caches."""

    root: Path
    command: str = ""  # the agent's command line
    files: dict[Path, _File | None] = field(default_factory=dict)
    steps: int = 0
    bytes: int = 0
    peeking: bool = False  # reading once more without following anything
    results: dict[tuple, list] = field(default_factory=dict)  # a script's result, per call
    parsed: dict[tuple, object] = field(default_factory=dict)  # makefiles and justfiles


_READING: ContextVar[_Reading | None] = ContextVar("agentprint_reading", default=None)


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _reading(cwd: Path) -> _Reading:
    reading = _READING.get()
    return reading if reading is not None else _Reading(_real(cwd))


def _is_shell(program: str) -> bool:
    return Path(program).name in c.SHELLS


def _looks_like_release(*texts: str) -> bool:
    return any(_RELEASE_WORD.search(text) for text in texts)


# ---- reading files --------------------------------------------------------------------------


def _read(arg: str, cwd: Path) -> _File | None:
    """A file the command runs, read when it is inside the folder the agent's command runs in.

    None when it is missing, not a regular file, outside that folder (symlinks resolved), named
    through a variable, or not text (a compiled program): none of those are followed."""
    if not arg or c._UNKNOWABLE.search(arg):
        return None
    reading = _reading(cwd)
    try:
        real = (cwd / os.path.expanduser(arg)).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real != reading.root and reading.root not in real.parents:
        return None
    if real not in reading.files:
        reading.files[real] = _load(real, real.relative_to(reading.root).as_posix())
    return reading.files[real]


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:  # a folder that can be listed but not entered
        return False


def _load(path: Path, name: str) -> _File | None:
    if not _is_file(path):
        return None
    try:  # O_NONBLOCK and fstat: a FIFO swapped in after the check can't hang the read
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return _File(path, name, None, "unreadable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # the file object closes it now
            head = handle.read(MAX_FILE_BYTES + 1)
            if b"\0" in head:
                return None  # a compiled program, not a script
            if len(head) <= MAX_FILE_BYTES:
                return _File(path, name, _text(head))
            return _scan_large(handle, head, path, name)
    except OSError:
        return _File(path, name, None, "unreadable")
    finally:
        if fd >= 0:
            os.close(fd)


def _text(data: bytes) -> str:
    """File bytes as text: UTF-8 (bad bytes replaced), no BOM, and Windows CRLF line ends as LF,
    since everything that reads it goes line by line."""
    return data.decode("utf-8", "replace").removeprefix("\ufeff").replace("\r\n", "\n")


def _scan_large(handle, head: bytes, path: Path, name: str) -> _File:
    """Scan a file too large to follow, 64 KB at a time with an overlap, up to 8 MB."""
    named: list[Hit] = []
    bare = ""
    first_line = _text(head[:4096]).split("\n", 1)[0]
    total, chunk, tail = len(head), head, ""
    while chunk:
        text = tail + chunk.decode("utf-8", "replace")
        for hit in _named(text):
            if hit not in named:
                named.append(hit)
        bare = bare or next((m.group(1) for m in _BARE_PUBLISH.finditer(text)), "")
        tail = text[-128:]
        if total >= MAX_SCAN_BYTES:
            if handle.read(1):
                return _File(path, name, None, "huge", first_line=first_line)
            break
        chunk = handle.read(min(MAX_FILE_BYTES, MAX_SCAN_BYTES - total))
        total += len(chunk)
    return _File(path, name, None, "large", tuple(named), bare, first_line)


# ---- what text names ------------------------------------------------------------------------


_PARTS = re.compile(r"(\n|;|&&|\|\|?)")  # where one command ends in scanned text
_DRY_RUN = re.compile(r"(?<![\w-])(?:--dry-run|--preview)(?![\w-])")
_GIT_PUSH_N = re.compile(r"(?<![\w.-])git(?![\w-]).*(?<![\w-])push(?![\w-]).*(?<![\w-])-n(?![\w-])")


# A dry-run flag that depends on something: ${DRY:+--dry-run}, `dry ? '--dry-run' : ''`,
# `+ (['--dry-run'] if '-n' in sys.argv else [])`. Such a command may well publish.
_CONDITIONAL = re.compile(r"\$\{|\?|\bif\b|\bunless\b|argv|environ|getenv|process\.env|ENV\[")


def _without_dry_runs(text: str) -> str:
    """Scanned text without the commands that are dry runs (`npm publish --dry-run`, `git push
    -n`): those upload nothing, so naming one is not a reason to say "could not check". Only a
    literal, unconditional dry run counts; one that depends on a variable or a test does not."""

    def dry(part: str) -> bool:
        if _PARTS.fullmatch(part) or _CONDITIONAL.search(part):
            return False
        return bool(_DRY_RUN.search(part) or _GIT_PUSH_N.search(part))

    return "".join(" " if dry(part) else part for part in _PARTS.split(text))


def _named(text: str) -> list[Hit]:
    """The publishes and pushes `_NAMED_PUBLISH` finds in text, in order, once each (dry runs
    left out)."""
    hits: list[Hit] = []
    text = _without_dry_runs(text)
    for match in _NAMED_PUBLISH.finditer(text):
        tool = next(group for group in match.groups() if group)
        hit = (c.Action.push if tool == "git" else c.Action.publish, tool)
        if hit not in hits:
            hits.append(hit)
    return hits


def _hits(text: str, cwd: Path) -> list[Hit]:
    """Shell text only scanned, not read as commands: named publishes and backstop hits."""
    hits = _named(text)
    for risky in c._backstop(_without_dry_runs(text), cwd):
        if (risky.action, risky.tool) not in hits:
            hits.append((risky.action, risky.tool))
    return hits


def _what(action: c.Action, tool: str) -> str:
    if action is c.Action.push:
        return "git push"
    if tool == "twine":
        return "twine upload"
    return tool if tool in c.ROOT_RELEASERS else f"{tool} publish"


def _flag(hits: list[Hit], label: str, cwd: Path, why: str) -> list[c.Risky]:
    """A publish or push agentprint saw but could not check: never OK."""
    return [
        c.Risky(action, tool, cwd, unsure=why.format(what=_what(action, tool)), label=label)
        for action, tool in hits
    ]


def _unread(file: _File, cwd: Path, runner: str, shell: bool) -> list[c.Risky]:
    """A file agentprint could not read whole: "could not check" only on evidence."""
    if file.size == "large":
        hits = list(file.named)
        if shell and not hits and file.bare:
            hits = [(c.Action.push if file.bare == "push" else c.Action.publish, runner)]
            return [c.Risky(*hits[0], cwd, unsure=_LARGE.format(what=file.bare), label=file.name)]
        return _flag(hits, file.name, cwd, _LARGE)
    if _looks_like_release(file.name, _reading(cwd).command):
        why = _HUGE if file.size == "huge" else _UNREADABLE
        return [c.Risky(c.Action.publish, runner, cwd, unsure=why, label=file.name)]
    return []


# ---- following ------------------------------------------------------------------------------


def _step(step: recipes.Step, name: str, cwd: Path, depth: int) -> list[c.Risky]:
    """One script or recipe line from file `name`. Shell is read as commands; other languages
    are only scanned for a publish or push they name."""
    if not _is_shell(step.program):
        return _flag(_named(step.text), name, cwd, _CODE)
    text, other = recipes.shell_text(step.text)
    text = recipes.called_only(text)  # a function's body runs only when it is called
    found = [risky for body in other for risky in _flag(_named(body), name, cwd, _CODE)]
    reading = _reading(cwd)
    if reading.peeking or reading.bytes + len(text) > MAX_BYTES:  # charged before it is read
        return found + _flag(_hits(text, cwd), name, cwd, _DEEP)
    if depth >= c.MAX_DEPTH or reading.steps >= MAX_STEPS:
        return found + _peek(text, cwd, min(depth, c.MAX_DEPTH))
    reading.steps += 1
    reading.bytes += len(text)
    return found + c.detect(text, cwd, depth + 1)


def _peek(text: str, cwd: Path, depth: int) -> list[c.Risky]:
    """Read `text` once more without following anything it runs: its own commands are checked;
    the scripts, recipes and package scripts it runs are only scanned for a publish or push."""
    reading = _reading(cwd)
    reading.peeking = True
    reading.bytes += len(text)
    try:
        return c._detect(text, cwd, depth)
    finally:
        reading.peeking = False


def too_deep(text: str, cwd: Path) -> list[c.Risky]:
    """`commands.detect` past `MAX_DEPTH` (a `bash -c` or package script at the limit)."""
    reading = _reading(cwd)
    if reading.peeking or reading.bytes + len(text) > MAX_BYTES:
        # Labelled with what it runs, never with the text: a line can hold a key (NPM_TOKEN=...).
        return [
            c.Risky(action, tool, cwd, unsure=_NESTED, label=_what(action, tool))
            for action, tool in _hits(text, cwd)
        ]
    return _peek(text, cwd, c.MAX_DEPTH)


def _own_folder(text: str, folder: PurePath) -> str:
    """A shell script with `$(dirname "$0")` and friends replaced by its own folder, and the
    variables set to that folder (`ROOT="$(cd "$(dirname "$0")/.." && pwd)"`) filled in.

    The folder is written with forward slashes (`C:/Users/me/proj` on Windows, which Git Bash
    takes too): a backslash in shell text is an escape, so `cd $(dirname $0)` would lose them."""
    where = folder.as_posix()
    text = _OWN_FOLDER.sub(lambda _: where, text)
    assigned = re.findall(
        r"(?m)^[ \t]*(?:export[ \t]+|readonly[ \t]+|local[ \t]+|declare[ \t]+)?([A-Za-z_]\w*)="
        r"(\"?)(" + re.escape(where) + r"[^\"\s;&|]*)\2[ \t]*(?:;|$)",
        text,
    )
    for variable, _, value in assigned:
        text = re.sub(rf"\$\{{{variable}\}}|\${variable}\b", lambda _, v=value: v, text)
    return text


def _script(
    arg: str,
    cwd: Path,
    depth: int,
    runner: str | None = None,
    unknown: str = "",
    argv: list[str] | None = None,
) -> list[c.Risky]:
    """`./release.sh`, `bash scripts/release.sh`, `python release.py`: what the file runs.

    `unknown` is set when an earlier `cd` went somewhere only known at run time; `argv` is what
    the script is given, filled in for "$@", $1... (a wrapper: `./with-env.sh npm publish`)."""
    file = _read(arg, cwd)
    if file is None:
        return _lost(arg, cwd, depth, unknown)
    program = runner or recipes.interpreter(
        file.text if file.text is not None else file.first_line, file.name
    )
    if file.text is None:
        return _unread(file, cwd, program, _is_shell(program))
    reading = _reading(cwd)
    argv = argv or []
    key = (file.path, cwd, program, depth, reading.peeking, tuple(argv))
    if key not in reading.results:
        text = file.text
        if _is_shell(program):
            text = _with_args(_own_folder(text, file.path.parent), argv)
        reading.results[key] = _step(recipes.Step(text, program), file.name, cwd, depth)
    return list(reading.results[key])


# A positional parameter that is a whole word: "$@", "${1}", $*, $2. Only these are filled in;
# inside a quoted string ("Releasing $1") an argument could open or close a quote.
_ARG_WORD = re.compile(r'"\$(?:\{([@*1-9])\}|([@*1-9]))"|\$(?:\{([@*1-9])\}|([@*1-9]))')
_SHIFT = re.compile(r"shift(?:[ \t]+([0-9]+))?(?=[ \t]*(?:$|[;&|\n]))")
_STARTS_WORD = set(" \t\n;&|(=")
_ENDS_WORD = set(" \t\n;&|)")
MAX_ARGS_TEXT = 4096  # what a wrapper's "$@" may grow into, per use


def _with_args(text: str, argv: list[str]) -> str:
    """A shell script with "$@", "$*", $1..$9 and $# filled in from the arguments it was run
    with, so a wrapper that forwards them is read as what it runs.

    Only whole words outside quotes are filled in (`npm "$@"`, `cd $1`, `cmd=$1`), never text
    inside a quoted string, where an argument holding a quote could open a fake `cd` or hide a
    publish. `shift [N]` drops arguments for what follows. In all, at most 64 KB is added.
    Left alone with no arguments (inside a function they mean the function's, a best effort)."""
    if not argv or len(shlex.join(argv)) > MAX_ARGS_TEXT:
        return text
    out: list[str] = []
    quote, added, i = "", 0, 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\" and quote == '"':
                out.append(text[i : i + 2])
                i += 2
                continue
            quote = "" if char == quote else quote
        elif char == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif text.startswith("$#", i):
            out.append(str(len(argv)))
            i += 2
            continue
        elif char in '"$' and (i == 0 or text[i - 1] in _STARTS_WORD):
            match = _ARG_WORD.match(text, i)
            if match and (match.end() == len(text) or text[match.end()] in _ENDS_WORD):
                which = next(group for group in match.groups() if group)
                if which in "@*":
                    value = shlex.join(argv)
                else:
                    index = int(which) - 1
                    value = shlex.quote(argv[index]) if index < len(argv) else "''"
                if added + len(value) <= MAX_FILE_BYTES:
                    out.append(value)
                    added += len(value)
                    i = match.end()
                    continue
            quote = '"' if char == '"' else ""
        elif char == "'" or char == '"':
            quote = char
        elif char == "s" and (shift := _SHIFT.match(text, i)) and _at_command_start(text, i):
            argv = argv[int(shift.group(1) or 1) :]
        out.append(char)
        i += 1
    return "".join(out)


def _at_command_start(text: str, at: int) -> bool:
    """Is `at` where a command starts: the line's start, or after `;`, `&&` or `|`?"""
    before = text[text.rfind("\n", 0, at) + 1 : at].rstrip(" \t")
    return before == "" or before[-1] in ";&|"


def _lost(arg: str, cwd: Path, depth: int, unknown: str) -> list[c.Risky]:
    """A script run after a `cd` to a folder only known at run time (on the command line or in
    a followed file), which isn't where agentprint looked: "could not check" when it looks like
    a release."""
    if not unknown or c._UNKNOWABLE.search(arg) or os.path.lexists(cwd / arg):
        return []
    if not _looks_like_release(arg):
        return []
    return [c.Risky(c.Action.publish, "sh", cwd, unsure=unknown, label=arg)]


def _shell_file_arg(args: list[str]) -> str | None:
    """The script in `bash [options] script [args]`; None when the shell reads stdin."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if arg in _SHELL_VALUES:
            i += 2
        elif arg.startswith(("-", "+")):
            if arg.startswith("-") and not arg.startswith("--") and "s" in arg[1:]:
                return None  # bash -s: commands come from stdin
            i += 1
        else:
            return arg
    return None


def _interpreter(program: str) -> bool:
    return bool(c._PYTHON.match(program)) or program in INTERPRETERS or program == "nodejs"


def _interpreted(program: str, args: list[str], cwd: Path, depth: int) -> list[c.Risky]:
    """`python scripts/release.py`, `node -e '...'`, `python -u -m twine upload`."""
    key = "python" if c._PYTHON.match(program) else "node" if program == "nodejs" else program
    code, values = INTERPRETERS[key]
    if key == "deno":
        if args[:1] == ["eval"]:
            return _flag(_named(" ".join(args[1:])), "deno eval", cwd, _CODE)
        if args[:1] != ["run"]:
            return []
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        flag, eq, inline = arg.partition("=")
        if key == "python" and arg == "-m":
            return c.detect(shlex.join(args[i + 1 :]), cwd, depth + 1)
        if flag in code:
            text = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            return _flag(_named(text), f"{program} {flag}", cwd, _CODE)
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        i += 2 if flag in values and not eq else 1
    if i >= len(args) or args[i] == "-":
        return []  # a prompt, or code from stdin
    return _script(args[i], cwd, depth, runner=program)


def _find_up(start: Path, wanted: tuple[str, ...]) -> Path | None:
    """The file named one of `wanted` (any case, first name first) in `start` or a folder above
    it, up to the folder the agent's command runs in. Nothing above that folder is looked at."""
    root = _reading(start).root
    here = _real(start)
    while True:
        try:
            entries = {entry.lower(): entry for entry in sorted(os.listdir(here), reverse=True)}
        except OSError:
            entries = {}
        for name in wanted:
            if name in entries and _is_file(here / entries[name]):
                return here / entries[name]
        if here == root or root not in here.parents:
            return None
        here = here.parent


# ---- make, just, task -----------------------------------------------------------------------


def _make(args: list[str], cwd: Path, depth: int) -> list[c.Risky]:
    """`make [target ...]`: the targets' recipes and their prerequisites', from the makefile."""
    dirs: list[str] = []
    files: list[str] = []
    goals: list[str] = []
    overrides: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg, nxt = args[i], (args[i + 1] if i + 1 < len(args) else "")
        i += 1
        flag, eq, inline = arg.partition("=")
        if arg in _MAKE_NO_RUN:
            return []  # a dry run, a question, a touch, or help: no recipe runs
        valued: tuple[str, str] | None = None
        if arg.startswith("--"):
            if flag in _MAKE_VALUES:
                valued = (flag, inline if eq else nxt)
                i += 0 if eq else 1
            elif flag in _MAKE_COUNTS and not eq and nxt.isdigit():
                i += 1
        elif arg.startswith("-") and len(arg) > 1:
            for k, letter in enumerate(arg[1:], start=2):
                rest = arg[k:]
                if f"-{letter}" in _MAKE_VALUES:
                    valued = (f"-{letter}", rest or nxt)
                    i += 0 if rest else 1
                    break
                if f"-{letter}" in _MAKE_COUNTS or letter == "O":
                    i += 1 if f"-{letter}" in _MAKE_COUNTS and not rest and nxt.isdigit() else 0
                    break
                if letter in "nqtvh":
                    return []
        elif eq and c._ASSIGN.match(arg):
            overrides[flag] = inline
        else:
            goals.append(arg)
        if valued and valued[0] in {"-C", "--directory"}:
            dirs.append(valued[1])
        elif valued and valued[0] in {"-f", "--file", "--makefile"}:
            files.append(valued[1])
    for folder in dirs:
        if not folder or c._UNKNOWABLE.search(folder):
            return []
        cwd = cwd / os.path.expanduser(folder)
    if not files:
        try:
            present = set(os.listdir(cwd))
        except OSError:
            return []
        files = [name for name in MAKEFILES if name in present][:1]
    loaded = _makefile(cwd, files)
    if not isinstance(loaded, tuple):
        return loaded
    mf, name, incomplete, scanned = loaded
    plan = recipes.make_plan(mf, goals, overrides)
    found: list[c.Risky] = list(scanned)
    unknown = plan.missing + plan.absent  # may be defined in a makefile agentprint did not read
    asked = goals or [mf.default]
    if incomplete and unknown and _looks_like_release(*unknown, *asked):
        why = _UNREAD_INCLUDE.format(kind="makefile")
        label = " ".join(["make", *goals])
        found.append(c.Risky(c.Action.publish, "make", cwd, unsure=why, label=label))
    reading = _reading(cwd)
    for raw in plan.raw:
        left = MAX_BYTES - reading.bytes
        if reading.peeking or left <= 0:  # scan only, with a small expansion
            found += _flag(
                _hits(plan.text(raw, limit=recipes.SMALL_EXPANSION), cwd), name, cwd, _DEEP
            )
        elif text := plan.text(raw, total=left):  # a .ONESHELL step stops growing at the budget
            found += _step(recipes.Step(text), name, cwd, depth)
    return found


_Loaded = tuple[recipes.Makefile, str, bool, list["c.Risky"]]


def _makefile(cwd: Path, files: list[str]) -> _Loaded | list[c.Risky]:
    """The makefile(s) and their includes, read once per command line: the rules, the name, whether
    some include was not read, and what a scan of an include too large to follow found. A list
    when the makefile itself can't be followed: empty (missing, outside, not text) or a scan."""
    reading = _reading(cwd)
    key = ("make", _real(cwd), tuple(files))
    cached = reading.parsed.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    mf: recipes.Makefile | None = None
    name = ""
    incomplete = False
    queue = [(path, False) for path in files]
    read = 0
    scanned: list[c.Risky] = []  # includes too large to follow: what their scan found
    result: _Loaded | list[c.Risky] = []
    while queue and read < MAX_INCLUDES:
        path, optional = queue.pop(0)
        file = _read(path, cwd)
        if file is None:
            if mf is None:
                break  # the makefile is missing, outside the folder, or not text
            incomplete = incomplete or not optional
            continue
        if file.text is None:
            if mf is None:  # the makefile itself: only its scan
                result = _unread(file, cwd, "make", True)
                break
            incomplete = True  # an include (or a second -f) too large to follow
            scanned += _unread(file, cwd, "make", True)
            continue
        before = len(mf.includes) if mf else 0
        mf = recipes.parse_make(file.text, mf)
        name = name or file.name
        for line, optional in mf.includes[before:]:  # include lines, variables filled in
            for word in recipes.expand_make(line, mf.variables).split():
                if "$" in word:
                    incomplete = True  # only make knows which file this is
                elif "*" in word or "?" in word or "[" in word:
                    queue += [(found, True) for found in sorted(glob.glob(str(cwd / word)))]
                else:
                    queue.append((word, optional))
        read += 1
    if mf is not None:
        result = (mf, name, incomplete or bool(queue), scanned)
    reading.parsed[key] = result
    return result


def _just(args: list[str], cwd: Path, depth: int) -> list[c.Risky]:
    """`just [recipe ...]`: the recipes and their dependencies, from the justfile."""
    justfile = workdir = ""
    names: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        flag, eq, inline = arg.partition("=")
        if flag in _JUST_NO_RUN:
            return []
        if flag in {"-c", "--command"}:  # just --command <program> [args]
            return c.detect(shlex.join(args[i:]), cwd, depth + 1)
        if flag == "--set":
            i += 2
        elif flag in _JUST_VALUES:
            value = inline if eq else (args[i] if i < len(args) else "")
            i += 0 if eq else 1
            if flag in {"-f", "--justfile"}:
                justfile = value
            elif flag in {"-d", "--working-directory"}:
                workdir = value
        elif not arg.startswith("-") and not c._ASSIGN.match(arg):
            names.append(arg)
    path = cwd / os.path.expanduser(justfile) if justfile else _find_up(cwd, JUSTFILES)
    if path is None:
        return []
    loaded = _justfile(path, cwd)
    if not isinstance(loaded, tuple):
        return loaded
    jf, main, incomplete = loaded
    names = names[:1] + [n for n in names[1:] if n in jf.recipes or n in jf.aliases]  # args
    steps, missing = recipes.just_steps(jf, names)
    found: list[c.Risky] = []
    if missing and (incomplete or jf.modules) and _looks_like_release(*missing):
        why = _UNREAD_INCLUDE.format(kind="just module or import")
        label = " ".join(["just", *names])
        found.append(c.Risky(c.Action.publish, "just", cwd, unsure=why, label=label))
    home = cwd if main.path.parent == _real(cwd) else main.path.parent  # where just runs them
    folder = home / jf.workdir if jf.workdir else home
    folder = cwd / workdir if workdir else folder
    for step in steps:
        found += _step(step, main.name, cwd if step.here else folder, depth)
    if "$" in str(folder):
        why = "the folder its recipes run in is only known when just runs"
        found = [replace(r, unsure=r.unsure or why) for r in found]
    return found


def _justfile(path: Path, cwd: Path) -> tuple[recipes.Justfile, _File, bool] | list[c.Risky]:
    """The justfile and its imports, read once per command line (a list when not followed)."""
    reading = _reading(cwd)
    key = ("just", str(path), _real(cwd))
    cached = reading.parsed.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    main = _read(str(path), cwd)
    result: tuple[recipes.Justfile, _File, bool] | list[c.Risky] = []
    if main is not None and main.text is None:
        result = _unread(main, cwd, "just", True)
    elif main is not None and main.text is not None:
        jf = recipes.parse_just(main.text)
        incomplete = False
        queue = [(p, optional, main.path.parent) for p, optional in jf.imports]
        read = 1
        while queue and read < MAX_INCLUDES:
            target, optional, base = queue.pop(0)
            file = _read(target, base)
            if file is None:
                incomplete = incomplete or not optional
                continue
            if file.text is None:
                incomplete = True  # an import too large to follow
                continue
            before = len(jf.imports)
            jf = recipes.parse_just(file.text, jf)
            queue += [(p, optional, file.path.parent) for p, optional in jf.imports[before:]]
            read += 1
        result = (jf, main, incomplete or bool(queue))
    reading.parsed[key] = result
    return result


def _task(args: list[str], cwd: Path) -> list[c.Risky]:
    """`task <name>` (go-task): Taskfile commands are not read as commands; a publish or push
    named in the tasks it runs means "could not check"."""
    taskfile = ""
    folder = cwd
    names: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        flag, eq, inline = arg.partition("=")
        if arg == "--":
            break
        if flag in _TASK_NO_RUN:
            return []
        if flag in _TASK_VALUES:
            value = inline if eq else (args[i] if i < len(args) else "")
            i += 0 if eq else 1
            if flag in {"-t", "--taskfile"}:
                taskfile = value
            elif flag in {"-d", "--dir"}:
                folder = cwd / os.path.expanduser(value)
        elif not arg.startswith("-") and not c._ASSIGN.match(arg):
            names.append(arg)
    path = folder / taskfile if taskfile else _find_up(folder, TASKFILES)
    if path is None:
        return []
    file = _read(str(path), cwd)
    if file is None:
        return []
    if file.text is None:
        return _unread(file, cwd, "task", True)
    return _flag(_named(recipes.taskfile_text(file.text, names)), file.name, cwd, _TASK)
