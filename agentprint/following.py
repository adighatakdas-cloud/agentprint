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

import base64
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
    "php": ({"-r"}, {"-c", "-d", "-z"}),
    "bun": ({"-e", "--eval", "-p", "--print"}, {"-r", "--preload", "--cwd", "--config"}),
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
    r"(?=[npybuhfcltgsr])(?<![\w.$-])(?:(npm|pnpm|yarn|bun|uv|poetry|hatch|flit|changeset|lerna)"
    r"(?:\.|(?![\w.-]).{0,40}?(?<![\w./-]))publish(?![\w:-])"
    r"|(twine)(?![\w-]).{0,40}?(?<![\w-])upload(?![\w-])"
    r"|(git)(?:\.|(?![\w./-]).{0,40}?(?<![\w./-]))push(?![\w-])"
    r"|(semantic-release|release-it)(?![\w.-])"
    r"|npx\s+(np)(?![\w.-]))",
    re.S | re.I,  # code can build `NPM PUBLISH`.lower(); a shell on macOS finds `NPM` too
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
    includes: tuple[tuple[str, bool], ...] = ()  # a large file: its make include lines


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
    texts: list = field(default_factory=list)  # every text read: the backstop scans them all
    funcs: list = field(default_factory=list)  # every shell function defined
    calls: set = field(default_factory=set)  # function calls followed
    dynamic: list = field(default_factory=list)  # (prefix, suffix) of commands named at run time
    virtual: dict = field(default_factory=dict)  # files the command line writes: path -> value


_READING: ContextVar[_Reading | None] = ContextVar("agentprint_reading", default=None)
_NAMING: ContextVar[str] = ContextVar("agentprint_naming", default="")  # the file being read


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
    if reading.virtual and (virtual := _virtual_path(arg, cwd)) in reading.virtual:
        return _virtual_file(reading, virtual)  # written earlier by this same command line
    try:
        real = (cwd / os.path.expanduser(arg)).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real != reading.root and reading.root not in real.parents:
        return None
    if real not in reading.files:
        reading.files[real] = _load(real, real.relative_to(reading.root).as_posix())
    return reading.files[real]


def _virtual_path(arg: str, cwd: Path) -> Path:
    return Path(os.path.normpath(_real(cwd) / os.path.expanduser(arg)))


def _virtual_file(reading: _Reading, path: Path) -> _File | None:
    value = reading.virtual[path]
    for origin in value.origins:  # what was written is read again: the backstop counts it there
        origin.delegated = True
    if reading.root not in path.parents:
        return None
    name = path.relative_to(reading.root).as_posix()
    if value.text is None:
        return _File(path, name, None, "unreadable")
    return _File(path, name, value.text)


def write_virtual(arg: str, cwd: Path, value, append: bool = False) -> None:
    """A file the command line writes (`cat > x.sh <<EOF`, `echo ... > x`, `tee x`): what a later
    command in the same line reads or runs from it is this text, not what is on disk now."""
    reading = _READING.get()
    if reading is None or not arg or c._UNKNOWABLE.search(arg):
        return
    path = _virtual_path(arg, cwd)
    if append:
        old = reading.virtual.get(path)
        before = old.text if old is not None else file_text(arg, cwd)
        if before is None or value.text is None:
            value = type(value)(None, (*(old.origins if old else ()), *value.origins))
        else:
            value = type(value)(
                before + value.text, (*(old.origins if old else ()), *value.origins)
            )
    reading.virtual[path] = value


def file_text(arg: str, cwd: Path) -> str | None:
    """A file's text when it is inside the folder and at most 64 KB (`cat file`, `< file`)."""
    file = _read(arg, cwd)
    return file.text if file is not None else None


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
    includes: list[tuple[str, bool]] = []
    bare = ""
    first_line = _text(head[:4096]).split("\n", 1)[0]
    total, chunk, tail, partial = len(head), head, "", ""
    while chunk:
        decoded = chunk.decode("utf-8", "replace")
        text = tail + decoded
        for hit in _named(text):
            if hit not in named:
                named.append(hit)
        bare = bare or next((m.group(1) for m in _BARE_PUBLISH.finditer(text)), "")
        tail = text[-128:]
        whole, _, partial = (partial + decoded).rpartition("\n")  # include lines, whole lines
        includes += _include_lines(whole, len(includes))
        if total >= MAX_SCAN_BYTES:
            if handle.read(1):
                return _File(path, name, None, "huge", first_line=first_line)
            break
        chunk = handle.read(min(MAX_FILE_BYTES, MAX_SCAN_BYTES - total))
        total += len(chunk)
    includes += _include_lines(partial, len(includes))
    return _File(path, name, None, "large", tuple(named), bare, first_line, tuple(includes))


_INCLUDE_LINE = re.compile(r"(?m)^[ \t]*(-include|sinclude|include)[ \t]+([^#\n]+)")


def _include_lines(text: str, have: int) -> list[tuple[str, bool]]:
    """The make include lines in text (a cheap line scan of a makefile too large to parse)."""
    if have >= MAX_INCLUDES * 4 or "include" not in text:
        return []
    return [(m.group(2).strip(), m.group(1) != "include") for m in _INCLUDE_LINE.finditer(text)]


# ---- what text names ------------------------------------------------------------------------


_PARTS = re.compile(r"(\n|;|&&|\|\|?)")  # where one command ends in scanned text
_DRY_RUN = re.compile(r"(?<![\w-])(?:--dry-run|--preview)(?:=true)?(?![\w=-])", re.I)
_GIT_PUSH_N = re.compile(
    r"(?<![\w.-])git(?![\w-])[^\n]*?(?<![\w-])push(?![\w-])[^\n]*?(?<![\w-])-n(?![\w-])"
)


# A dry-run flag that depends on something: ${DRY:+--dry-run}, `dry ? '--dry-run' : ''`,
# `+ (['--dry-run'] if '-n' in sys.argv else [])`. Such a command may well publish.
_CONDITIONAL = re.compile(
    r"\$\{|\$\(|`|\?|\bif\b|\bunless\b|\band\b|\bor\b|argv|environ|getenv|process\.env|ENV\["
    r"|\*|\[\s*-?\d*\s*:|--no-dry-run|(?:dry-run|preview)['\"]?\s*[=,]?\s*['\"]?(?:false|0|no|off)\b",
    re.I,
)


def _without_dry_runs(text: str) -> str:
    """Scanned text without the commands that are dry runs (`npm publish --dry-run`, `git push
    -n`): those upload nothing, so naming one is not a reason to say "could not check". Only a
    literal, unconditional dry run counts; one that depends on a variable or a test does not."""

    def dry(part: str) -> bool:
        if _PARTS.fullmatch(part) or not (_DRY_RUN.search(part) or _GIT_PUSH_N.search(part)):
            return False
        return not _CONDITIONAL.search(part)

    return "".join(" " if dry(part) else part for part in _PARTS.split(text))


def _named(text: str) -> list[Hit]:
    """The publishes and pushes `_NAMED_PUBLISH` finds in text, in order, once each (dry runs
    left out)."""
    hits: list[Hit] = []
    text = _without_dry_runs(text)
    for match in _NAMED_PUBLISH.finditer(text):
        tool = next(group for group in match.groups() if group)
        tool = tool.lower()
        hit = (c.Action.push if tool == "git" else c.Action.publish, tool)
        if hit not in hits:
            hits.append(hit)
    return hits


# ---- code in other languages ----------------------------------------------------------------

_STRING = r"""(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\$])*`)"""
_STRING_ANY = r"""(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)"""
_JOINED = re.compile(rf"({_STRING})(?:\s*(?:\+|\.\.?)\s*|[ \t]+)({_STRING})")  # "a" + 'b', "a" "b"
_LIST = rf"{_STRING}(?:\s*,\s*{_STRING})*\s*,?"
_PY_JOIN = re.compile(rf"({_STRING})\.join\(\s*[\[(]\s*({_LIST})\s*[\])]\s*\)")  # "".join([...])
_JS_JOIN = re.compile(rf"\[\s*({_LIST})\s*\]\.join\(\s*({_STRING})?\s*\)")  # [...].join("")
_LITERAL_PART = re.compile(rf"\$?\{{\s*({_STRING})\s*\}}")  # `${"lish"}`, f"{'lish'}"
# Code that can start a process, anywhere in the file (aliases like `from subprocess import run`,
# `import subprocess as sp` or `const {execSync: x} = require("child_process")` included). JS
# `exec`/`spawn` count only with `child_process` (a RegExp's .exec() is not a process).
_STARTS_PROCESS = re.compile(
    r"\bsubprocess\b|\bos\.(?:system|popen|exec\w*|spawn\w*|posix_spawn\w*)|\bpty\.spawn|"
    r"\bpexpect\b|\bplumbum\b|\bimport\s+sh\b|\bfrom\s+sh\s+import\b|create_subprocess_|"
    r"child_process|\bexeca\b|\bzx\b|\$`|Deno\.(?:Command|run)|Bun\.(?:spawn\w*|\$)|"
    r"\bshelljs\b|cross-spawn|\bsystem\s*\(|(?<!['\"])\bexec(?:\s+|\s*\(\s*)[\"'`]|"
    r"%x[{(\[]|\bqx[{(/]|shell_exec|passthru|proc_open|\bpopen\s*\(|IO\.popen|Open3\.|"
    r"Process\.spawn|\bspawn\s*\(\s*[\"']"
)
# A publish or push word anywhere in code (or in what it is given on its command line), any case:
# with a process start in the same code, "could not check" (`run([t, "PUBLISH".lower()])`).
# A method call (`list.push(x)`, `client.publish(...)`), a path or a script name is not one.
_CODE_WORD = re.compile(r"(?<![\w.$:/-])(publish|push)(?![\w:./-])", re.I)
_UPLOAD_WORD = re.compile(r"(?<![\w.$:/-])upload(?![\w:./-])", re.I)
_TOOL = r"(?:npm|pnpm|yarn|bun|twine|uv|poetry|hatch|flit|git|gh)"
# A publishing tool as a string of its own (["npm", ...]) and what comes after it.
_TOOL_ITEM = re.compile(rf"""(["'])({_TOOL})\1\s*([,\])+])""", re.I)
# A command line written as one string: "npm " + verb, `npm ${v}`, f"git log {n}", "npm %s".
_TOOL_TEXT = re.compile(rf"""(["'`])({_TOOL})(\s(?:(?!\1)[^\\\n]|\\.)*)(\1)?""", re.I)
_ITEM = re.compile(rf"""\s*(?:({_STRING_ANY})|(\*|\.\.\.)|(\[)|(\]|\)))""")
_PLACEHOLDER = re.compile(r"\$\{|\{|%[sdr]|#\{|^\$\w")
_CODE_VALUES = {"--filter", "-F", "--dir", "-C", "--prefix", "--cwd", "-w", "--workspace",
                "--tag", "--registry", "--loglevel", "-c", "--git-dir", "--work-tree", "-n",
                "--format"}  # fmt: skip
_BUILT = "it builds a `{tool}` command at run time, which agentprint can't read"
MAX_NORMALISE = 8  # passes joining string literals


def _content(literal: str) -> str:
    return literal[1:-1]


def _normalised(code: str) -> str:
    """Code with strings built from literals joined: "pub" + "lish", 'npm ' . 'publish',
    "".join(["npm ", "publish"]), ["pub", "lish"].join(""), `npm ${"publish"}`."""
    for _ in range(MAX_NORMALISE):
        before = code
        code = _LITERAL_PART.sub(lambda m: _content(m.group(1)), code)
        code = _PY_JOIN.sub(
            lambda m: (
                '"'
                + _content(m.group(1)).join(_content(x) for x in re.findall(_STRING, m.group(2)))
                + '"'
            ),
            code,
        )
        code = _JS_JOIN.sub(
            lambda m: (
                '"'
                + (_content(m.group(2)) if m.group(2) else ",").join(
                    _content(x) for x in re.findall(_STRING, m.group(1))
                )
                + '"'
            ),
            code,
        )
        code = _JOINED.sub(
            lambda m: m.group(1)[0] + _content(m.group(1)) + _content(m.group(2)) + m.group(1)[0],
            code,
        )
        if code == before:
            break
    return code


_RUNTIME_WORD = "it starts a process and names `{verb}`, so it may build a publish at run time"


def _code_flags(
    code: str, name: str, cwd: Path, program: str = "", argv: list[str] | tuple = ()
) -> list[c.Risky]:
    """Code in another language, which agentprint does not read as commands: "could not check"
    when it names a publish or push once strings built from literals are joined (and literal
    base64, hex and character codes decoded), when it starts a process for a publishing tool
    whose subcommand is not written out, or when it starts a process and names a publish or push
    (any case) in its text or in what it is given on its command line."""
    plain = _normalised(code)
    if '\\"' in plain or "\\'" in plain:  # exec("os.system(\"npm pub\" + \"lish\")")
        plain += "\n" + _normalised(plain.replace('\\"', '"').replace("\\'", "'"))
    decoded = _decoded(plain)
    if decoded:
        plain += "\n" + decoded
    hits = _named(code) + [hit for hit in _named(plain) if hit not in _named(code)]
    if hits:
        return _flag(hits, name, cwd, _CODE)
    if not _starts_process(plain, program):
        return []
    given = " ".join(argv)
    plain = _without_dry_runs(plain)
    evidence = _CODE_WORD.search(plain) or _CODE_WORD.search(given)
    if not evidence and re.search(r"twine", plain + given, re.I):
        evidence = _UPLOAD_WORD.search(plain) or _UPLOAD_WORD.search(given)
    for tool, kind in _built_commands(plain):
        if kind == "splat" and not evidence:
            continue  # def git(*args): run(["git", *args]): a helper, its calls written out
        tool = tool.lower()
        action = c.Action.push if tool in {"git", "gh"} else c.Action.publish
        return [c.Risky(action, tool, cwd, unsure=_BUILT.format(tool=tool), label=name)]
    if evidence:
        verb = evidence.group(1).lower()
        action = c.Action.push if verb == "push" else c.Action.publish
        why = _RUNTIME_WORD.format(verb=verb)
        return [c.Risky(action, Path(program).name or "code", cwd, unsure=why, label=name)]
    return []


def _starts_process(code: str, program: str) -> bool:
    if _STARTS_PROCESS.search(code):
        return True
    return Path(program).name in {"ruby", "perl", "php"} and "`" in code


def _built_commands(code: str) -> list[tuple[str, str]]:
    """Publishing tools the code runs with a subcommand that is not written out: ("npm",
    "dynamic") for ["npm", verb] or `npm ${v}`, ("npm", "changed") for ["npm", "X".lower()],
    ("git", "splat") for ["git", *args]. A written-out subcommand (`pnpm --filter ${p} build`,
    f"npm install {pkg}", "git rev-parse " + ref) is not listed."""
    out: list[tuple[str, str]] = []
    for match in _TOOL_ITEM.finditer(code):
        kind = _list_verb(code, match.end(3) - 1)
        if kind != "literal":
            out.append((match.group(2), kind))
    for match in _TOOL_TEXT.finditer(code):
        kind = _text_verb(match.group(3), code[match.end() : match.end() + 3], bool(match.group(4)))
        if kind != "literal":
            out.append((match.group(2), kind))
    return out


def _list_verb(code: str, at: int) -> str:
    """The subcommand after a tool written as one item of a list: what kind it is."""
    after = code[at]
    if after in "])":
        rest = code[at + 1 : at + 8].lstrip()
        return "dynamic" if rest[:1] in {"+", "*"} or rest[:6] == ".concat" else "literal"
    if after == "+":
        return "dynamic"
    i = at + 1
    takes_value = False
    for _ in range(16):
        item = _ITEM.match(code, i)
        if item is None:  # an expression: a variable, sys.argv[1], a call...
            if not takes_value:
                return "dynamic"
            takes_value = False  # ["--filter", process.argv[1], "build"]: an option's value
            i = _skip_expression(code, i)
            comma = re.match(r"\s*,", code[i:])
            if not comma:
                return "literal"
            i += comma.end()
            continue
        literal, splat, opening, closing = item.groups()
        i = item.end()
        if opening:
            continue
        if closing:
            return "literal"  # no subcommand at all
        if splat:
            return "splat"
        assert literal is not None
        tail = code[i : i + 3].lstrip()
        if tail[:1] not in {",", "]", ")", ""}:
            return "changed"  # "PUBLISH".lower(), "hsilbup"[::-1], "pub%s" % x
        text = literal[1:-1]
        if takes_value:
            takes_value = False
        elif text.startswith("-"):
            takes_value = text.partition("=")[0] in _CODE_VALUES and "=" not in text
        else:
            return "literal" if not _PLACEHOLDER.search(text) else "dynamic"
        comma = re.match(r"\s*,", code[i:])
        if not comma:
            return "literal"
        i += comma.end()
    return "dynamic"


def _skip_expression(code: str, i: int) -> int:
    """Where an expression in a list ends: the next `,` (or closing bracket) outside brackets."""
    depth = 0
    end = min(len(code), i + 400)
    while i < end:
        char = code[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif char == "," and depth == 0:
            return i
        elif char in "\"'`":
            close = code.find(char, i + 1)
            i = end if close < 0 else close
        i += 1
    return i


def _text_verb(rest: str, after: str, closed: bool) -> str:
    """The subcommand inside a command written as one string ("npm publish x", `npm ${v}`)."""
    words = rest.split()
    takes_value = False
    for k, word in enumerate(words):
        if _PLACEHOLDER.search(word):
            if takes_value:
                takes_value = False
                continue
            return "dynamic"
        if takes_value:
            takes_value = False
        elif word.startswith("-"):
            takes_value = word.partition("=")[0] in _CODE_VALUES and "=" not in word
        else:
            last = k == len(words) - 1 and not rest[-1:].isspace()
            if last and closed and after.lstrip()[:1] in {"+", ".", "%"}:
                return "changed"  # "npm pub" + "lish": the word goes on past the string
            return "literal"
    if not closed:
        return "literal"
    return "dynamic" if after.lstrip()[:1] in {"+", ".", "%", ","} else "literal"


_B64_LITERAL = re.compile(r"""["']([A-Za-z0-9+/]{8,}={0,2})["']""")
_HEX_LITERAL = re.compile(r"""["']((?:[0-9a-fA-F]{2}){4,})["']""")
_CHAR_CODES = re.compile(r"(?<![\w.])\d{2,3}(?:\s*,\s*\d{2,3}){3,}(?![\w.])")


def _decoded(code: str) -> str:
    """Text written in code as base64, hex or character codes, decoded (so a publish hidden
    that way is still named)."""
    out: list[str] = []
    for match in _B64_LITERAL.finditer(code):
        literal = match.group(1)
        if len(literal) % 4:
            continue
        try:
            text = base64.b64decode(literal, validate=True).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            continue
        if text.isprintable():
            out.append(text)
    for match in _HEX_LITERAL.finditer(code):
        try:
            text = bytes.fromhex(match.group(1)).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            continue
        if text.isprintable():
            out.append(text)
    for match in _CHAR_CODES.finditer(code):
        codes = [int(n) for n in re.findall(r"\d+", match.group())]
        if all(32 <= n < 127 for n in codes):
            out.append("".join(map(chr, codes)))
    return "\n".join(out)


def _hits(text: str, cwd: Path) -> list[Hit]:
    """Shell text only scanned, not read as commands: named publishes and the publish and push
    commands written in it."""
    hits = _named(text)
    for hit in c.scan_hits(text):
        if hit not in hits:
            hits.append(hit)
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


def _step(
    step: recipes.Step,
    name: str,
    cwd: Path,
    depth: int,
    args: list[str] | None = None,
    env: dict | None = None,
) -> list[c.Risky]:
    """One script or recipe line from file `name`. Shell is read as commands (with the arguments
    and exported variables it is given); other languages are only scanned for a publish or push
    they name or build."""
    if not _is_shell(step.program):
        return _code_flags(step.text, name, cwd, step.program, args or [])
    text = step.text
    reading = _reading(cwd)
    if reading.peeking or reading.bytes + len(text) > MAX_BYTES:  # charged before it is read
        return _flag(_hits(text, cwd), name, cwd, _DEEP)
    if depth >= c.MAX_DEPTH or reading.steps >= MAX_STEPS:
        return _peek(text, cwd, min(depth, c.MAX_DEPTH))
    reading.steps += 1
    reading.bytes += len(text)
    token = _NAMING.set(name)
    try:
        return c.read_script(text, cwd, depth + 1, args, env)
    finally:
        _NAMING.reset(token)


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
    env: dict | None = None,
) -> list[c.Risky]:
    """`./release.sh`, `bash scripts/release.sh`, `python release.py`: what the file runs.

    `unknown` is set when an earlier `cd` went somewhere only known at run time; `argv` is what
    the script is given: its "$@", $1... (a wrapper: `./with-env.sh npm publish`); `env` the
    variables and functions it inherits."""
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
    key = (file.path, cwd, program, depth, reading.peeking, tuple(argv), _env_key(env))
    if key not in reading.results:
        text = file.text
        if _is_shell(program):
            text = _own_folder(text, file.path.parent)
        step = recipes.Step(text, program)
        reading.results[key] = _step(step, file.name, cwd, depth, argv, env)
    return list(reading.results[key])


def _env_key(env: dict | None) -> tuple:
    if not env:
        return ()
    values = tuple(sorted((k, v.value) for k, v in env.get("vars", {}).items()))
    return values, tuple(sorted(env.get("funcs", {})))


def sourced(arg: str, cwd: Path, depth: int, unknown: str = "") -> str | list[c.Risky]:
    """`source file`: its text (read in the caller's shell), or what to report instead when it
    can't be read whole."""
    file = _read(arg, cwd)
    if file is None:
        return _lost(arg, cwd, depth, unknown)
    if file.text is None:
        return _unread(file, cwd, "sh", True)
    reading = _reading(cwd)
    text = _own_folder(file.text, file.path.parent)
    if reading.peeking or reading.bytes + len(text) > MAX_BYTES:
        return _flag(_hits(text, cwd), file.name, cwd, _DEEP)
    if depth >= c.MAX_DEPTH or reading.steps >= MAX_STEPS:
        return _peek(text, cwd, min(depth, c.MAX_DEPTH))
    reading.steps += 1
    reading.bytes += len(text)
    return text


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


def interpreter_input(program: str, args: list[str]) -> tuple[str, int | None]:
    """What `python ...`, `node ...` runs: ("code", i) inline code at args[i], ("module", i) a
    `python -m` module, ("file", i) a script, ("stdin", i) code from stdin (`python3 -`, no
    script), or ("none", None)."""
    key = "python" if c._PYTHON.match(program) else "node" if program == "nodejs" else program
    code, values = INTERPRETERS[key]
    start = 0
    if key == "deno":
        if args[:1] == ["eval"]:
            return ("code", 1) if len(args) > 1 else ("none", None)
        if args[:1] != ["run"]:
            return "none", None
        start = 1
    i = start
    while i < len(args):
        arg = args[i]
        flag, eq, _ = arg.partition("=")
        if key == "python" and arg == "-m":
            return "module", i + 1
        if flag in code:
            return ("code", i) if eq else ("code", i + 1)  # the word that holds the code
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        i += 2 if flag in values and not eq else 1
    if i >= len(args) or args[i] == "-":
        return "stdin", i
    return "file", i


def _interpreted(
    program: str, args: list[str], cwd: Path, depth: int, stdin: str | None = None
) -> list[c.Risky]:
    """`python scripts/release.py`, `node -e '...'`, `python -u -m twine upload`, and code fed
    on stdin (`python3 - <<EOF`). What the code is given on its command line counts too."""
    kind, i = interpreter_input(program, args)
    if kind == "module" and i is not None:
        return c.detect(shlex.join(args[i:]), cwd, depth + 1)
    if kind == "code" and i is not None:
        word = args[i] if i < len(args) else ""
        if word.startswith("-") and "=" in word:  # --eval=CODE
            flag, _, text = word.partition("=")
        else:
            flag, text = (args[i - 1] if i else ""), word
        return _code_flags(text, f"{program} {flag}".strip(), cwd, program, args[i + 1 :])
    if kind == "stdin":
        if stdin is None:
            return []  # a prompt, or code from somewhere agentprint can't see
        rest = args[(i or 0) + 1 :] if i is not None and i < len(args) else []
        return _code_flags(stdin, _stdin_label(), cwd, program, rest)
    if kind == "file" and i is not None:
        return _script(args[i], cwd, depth, runner=program, argv=args[i + 1 :])
    return []


def _stdin_label() -> str:
    return _NAMING.get() or "here-document"


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


def _make(args: list[str], cwd: Path, depth: int, stdin: str | None = None) -> list[c.Risky]:
    """`make [target ...]`: the targets' recipes and their prerequisites', from the makefile
    (`make -f -` reads it from stdin: a here-document typed on the command line)."""
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
    if "-" in files and stdin is not None:
        write_virtual("-", cwd, c._Value(stdin))
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
        before = len(found)
        if reading.peeking or left <= 0:  # scan only, with a small expansion
            text = plan.text(raw, limit=recipes.SMALL_EXPANSION)
            found += _flag(_hits(text, cwd), name, cwd, _DEEP)
        elif text := plan.text(raw, total=left):  # a .ONESHELL step stops growing at the budget
            found += _step(recipes.Step(text), name, cwd, depth)
        if len(found) == before and "${" in text:  # filled in only when make runs
            found += _filled_by_make(plan.evidence(raw), name, cwd)
    return found


_MAKE_FILLS = "it runs a recipe line make fills in at run time, from text that names `{verb}`"


def _filled_by_make(evidence: str, name: str, cwd: Path) -> list[c.Risky]:
    """A recipe line with a value only make knows (`$(call pub,npm)`, `$(shell ...)`): "could not
    check" when the line or the variables it uses name a publish or push."""
    match = c.EVIDENCE.search(evidence)
    if match is None:
        return []
    verb = match.group(1).lower()
    action = c.Action.push if verb == "push" else c.Action.publish
    return [c.Risky(action, "make", cwd, unsure=_MAKE_FILLS.format(verb=verb), label=name)]


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
    read = tries = 0
    scanned: list[c.Risky] = []  # includes too large to follow: what their scan found
    result: _Loaded | list[c.Risky] = []
    first = True
    while queue and read < MAX_INCLUDES and tries < MAX_INCLUDES * 4:
        path, optional = queue.pop(0)
        tries += 1
        file = _read(path, cwd)
        if file is None:
            if first:
                break  # the makefile is missing, outside the folder, or not text
            incomplete = incomplete or not optional
            continue
        first, read = False, read + 1
        name = name or file.name
        if file.text is None:  # too large to follow (or unreadable): scanned, includes read
            incomplete = True
            scanned += _unread(file, cwd, "make", True)
            lines = list(file.includes)
        else:
            before = len(mf.includes) if mf else 0
            mf = recipes.parse_make(file.text, mf)
            lines = mf.includes[before:]
        for line, optional in lines:  # include lines, variables filled in
            for word in recipes.expand_make(line, mf.variables if mf else {}).split():
                if "$" in word:
                    incomplete = True  # only make knows which file this is
                elif "*" in word or "?" in word or "[" in word:
                    queue += [(found, True) for found in sorted(glob.glob(str(cwd / word)))]
                else:
                    queue.append((word, optional))
    result = (mf, name, incomplete or bool(queue), scanned) if mf is not None else scanned
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
