"""Spot the risky moments in a shell command line: publishing a package, or `git push`.

This is a best-effort reader of what an agent is about to run, not a shell. It follows `cd`,
`git -C`, env-var prefixes, wrappers (`env`, `sudo`, `timeout`, `xargs`, `uvx`, `npx`,
`corepack`, `python -m`, `uv run`, `poetry run`, `bash -c`, `eval`), shell keywords
(`if/then/else`, `for/while ... do`, `{ ...; }`, `!`), chains (`&&`, `||`, `;`, `|`, newlines,
subshells), command substitutions (`$(...)`, backticks), package.json scripts (`npm run release`,
and the `preversion`/`version`/`postversion` scripts `npm version` runs), workspace publishes
(`npm publish -w pkg`, `pnpm -r publish`) and release tools (changesets, lerna, np,
semantic-release, release-it). A word scan over the unquoted text runs as a backstop.

It also reads the files a command hands its work to, and reads them only inside the folder the
agent's command runs in (symlinks resolved), 64 KB at most each, text only: shell scripts
(`./release.sh`, `bash scripts/release.sh`, `source x.sh`, an executable `scripts/x` with a shell
shebang or none), Makefile targets with their prerequisites and `$(MAKE) <target>` calls, and
justfile recipes with their dependencies. Their commands go back through this reader, up to
`MAX_DEPTH` levels deep and `MAX_STEPS` commands per command line. A script in another language
(`python scripts/release.py`, `node release.js`, inline `python -c` code, a Taskfile task) is not
read as commands: if its text names a publish or push, the result is `unsure`. So is a file too
large or unreadable, and a file where the reader stops following that names a publish or runs
yet another file.

When it can't tell what a publish would upload (a `cd "$DIR"`, a `--filter` selector, a
target built from a variable) the result says so in `unsure`; the guard then reports
"could not check" instead of "OK". Here-documents on the command line itself are not understood
yet (their bodies are read as commands). Inside a script, a here-document fed to a shell is read
as commands, one fed to another language is word-scanned, and one fed to anything else is data.
Not followed: files outside the folder, compiled programs, script paths built from a variable,
and PowerShell.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from . import recipes


class Action(StrEnum):
    publish = "publish"
    push = "push"


@dataclass(frozen=True)
class Risky:
    action: Action
    tool: str  # npm, pnpm, yarn, bun, uv, twine, poetry, hatch, flit, git, changeset, lerna, ...
    cwd: Path  # directory the command runs in, after any `cd`
    targets: tuple[str, ...] = ()  # explicit files/folders given to the publish command
    workspaces: tuple[str, ...] = ()  # named workspace packages (npm -w pkg, yarn workspace pkg)
    all_workspaces: bool = False  # every workspace package (--workspaces, pnpm -r, lerna, ...)
    unsure: str = ""  # why agentprint can't tell what this would publish; empty when it can


NODE = {"npm", "pnpm", "yarn", "bun"}
PYTHON_PUBLISH = {"uv", "poetry", "hatch", "flit"}
WORKSPACE_RELEASERS = {"changeset", "lerna"}  # `<tool> publish` publishes workspace packages
ROOT_RELEASERS = {"np", "semantic-release", "release-it"}  # publish the package they run in
MAX_DEPTH = 4  # nested bash -c / eval / package scripts / script files
MAX_FILE_BYTES = 64 * 1024  # each script, Makefile, justfile or Taskfile is read up to this
MAX_STEPS = 400  # file commands followed per command line; later ones are only word-scanned

# Options whose next word is a value, per tool, so the value isn't taken for a subcommand.
_NODE_VALUES = {
    "--tag",
    "--access",
    "--otp",
    "--registry",
    "--userconfig",
    "--scope",
    "--loglevel",
    "--provenance-file",
    "--publish-branch",
    "--cache",
    "--globalconfig",
    "--new-version",
    "--message",
    "--auth-type",
    "--gzip-level",
}
VALUE_OPTS: dict[str, set[str]] = {
    "npm": _NODE_VALUES,
    "pnpm": _NODE_VALUES,
    "yarn": _NODE_VALUES,
    "bun": _NODE_VALUES,
    "uv": {"--index", "--publish-url", "--username", "-u", "--password", "-p", "--token", "-t",
           "--trusted-publishing", "--keyring-provider", "--check-url", "--project",
           "--cache-dir", "--config-file", "--python", "--index-url"},
    "twine": {"-r", "--repository", "--repository-url", "-u", "--username", "-p", "--password",
              "-c", "--comment", "--config-file", "--cert", "--client-cert", "--identity",
              "--sign-with"},
    "poetry": {"-r", "--repository", "-u", "--username", "-p", "--password", "--cert",
               "--client-cert", "-P", "--project"},
    "hatch": {"-r", "--repo", "-u", "--user", "-a", "--auth", "--ca-cert", "--client-cert",
              "--client-key", "-e", "--env"},
    "flit": {"--repository", "--pypirc", "--format"},
    "git": {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"},
}  # fmt: skip
CWD_OPTS: dict[str, set[str]] = {
    "npm": {"--prefix", "-C"},
    "pnpm": {"--dir", "-C", "--prefix"},
    "yarn": {"--cwd"},
    "bun": {"--cwd"},
    "uv": {"--directory"},
    "poetry": {"-C", "--directory"},
    "git": {"-C"},
}
NPM_WORKSPACE = {"--workspace", "-w"}  # npm: a named workspace. (pnpm -w = workspace root.)
ALL_FLAGS = {"--workspaces", "-ws", "--recursive", "-r"}
DRY_RUN = {"--dry-run", "--preview"}
NOT_PUBLISH = {"install", "i", "add", "pack", "remove", "uninstall", "update", "ci", "init"}
RUN_VERBS = {"run", "run-script", "rum", "urn"}
NPM_SCRIPT_ALIASES = {"test": "test", "t": "test", "tst": "test", "start": "start",
                      "stop": "stop", "restart": "restart"}  # fmt: skip
EXEC_VERBS = {"exec", "x", "dlx"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
LAUNCHERS = {"uvx", "npx", "pnpx", "bunx", "corepack"}  # run the next word as a program
LAUNCHER_VALUES = {"--from", "--with", "-p", "--package", "--python", "-c", "--call"}
PREFIXES = {"time", "nohup", "command", "exec", "builtin", "nice", "caffeinate", "stdbuf",
            "ionice", "chronic", "unbuffer", "do", "then", "else", "elif", "if", "while",
            "until", "!", "{"}  # fmt: skip
PREFIX_VALUES = {"-n", "-t", "-c", "-p", "-w", "-I", "-L", "-P", "-d", "-s", "-E", "-a", "-k"}
SKIP_SEGMENT = {"for", "case", "select", "function", "}", "fi", "done", "esac", "in", ";;"}
RUNNERS = {"uv", "poetry", "pdm", "pipenv", "rye", "hatch"}  # `<runner> run X` runs X
RUNNER_VALUES = {"--with", "-w", "--with-requirements", "--with-editable", "--python", "-p",
                 "--package", "--extra", "--group", "--only-group", "--env-file", "--index",
                 "--index-url", "--extra-index-url", "-e", "--env"}  # fmt: skip
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
_JS_FILES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")
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
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^(python[0-9.]*|py|pypy[0-9.]*)$")
_SEPARATOR = re.compile(r"^[;&|()]+$")
_REDIRECT = re.compile(r"^[0-9]*(?:>>?|<<?<?)$")
_REDIRECT_ATTACHED = re.compile(r"^[0-9]*(?:>>?|<<?<?)\S")
_FD_DUP = re.compile(r"[0-9]*[<>]&[0-9-]+")  # 2>&1, >&2: the '&' is not a separator here
_AMP_REDIRECT = re.compile(r"&>>?")  # &>file sends both streams to file
_UNKNOWABLE = re.compile(r"[$`*?]")  # variables, substitutions and globs: known only at run time
_BACKTICK = re.compile(r"`([^`]*)`")
_DOLLAR_PAREN = re.compile(r"\$\(([^()]*)\)")
# Backstop: a publishing word at a command position of the unquoted text. Linear time even on
# a 64 KB script: words stop at separators (each separator is a command position of its own),
# an assignment's value crosses at most four `$(...)`/`${...}`, and nothing backtracks into
# whitespace; the last newline of a run of blank lines is the command position.
_WORD = r"[^\s;&|(){}!]"
_VALUE_CHAR = r"(?:[^\s;&|(){}!$]|\$(?![({]))"
_SUBSTITUTION = r"(?:\$\([^()\s]{0,64}\)|\$\{[^{}\s]{0,64}\})"
_PROGRAM_POSITION = (  # where a program name starts, after any VAR=value prefixes
    r"(?:^|[;&|(){}!]|\n(?![ \t]*\n)|\b(?:do|then|else|elif|if|while|until|exec|xargs|time|nohup"
    r"|sudo|env|command|eval|watch|corepack|npx|uvx|bunx|pnpx)\s|\btimeout\s+\S+\s)\s*+"
    rf"(?:[A-Za-z_][A-Za-z0-9_]*={_VALUE_CHAR}*+(?:{_SUBSTITUTION}{_VALUE_CHAR}*+){{0,4}}+\s++)*+"
)
_COMMAND_POSITION = _PROGRAM_POSITION + rf"(?:{_WORD}*/)?"
_BACKSTOP = re.compile(
    _COMMAND_POSITION + r"(?:(npm|pnpm|yarn|bun|uv|poetry|hatch|flit|changeset|lerna)"
    r"(?:\s+-\S+)*\s+publish\b|(twine)(?:\s+-\S+)*\s+upload\b|(git)(?:\s+-\S+(?:\s+[^-\s]\S*)?)*"
    r"\s+push\b)"
)
# A file that runs another file: a path, make/just/task, `source`, or a script handed to an
# interpreter. Used only where agentprint stops following (too deep, or too many commands).
_RUNS_A_FILE = re.compile(
    _PROGRAM_POSITION + rf"(?:{_WORD}*/{_WORD}|(?:make|gmake|just|task|source|\.)(?![\w./-])"
    r"|(?:sh|bash|zsh|dash|ksh|fish|python[0-9.]*|node|tsx|ruby|perl)[ \t]+[^-\s])"
)


@dataclass(frozen=True)
class _File:
    path: Path  # symlinks resolved
    name: str  # as shown to the agent: relative to the folder its command runs in
    text: str | None  # None when it could not be read; `problem` says why
    problem: str = ""


@dataclass
class _Reading:
    """The files one command line runs: the folder they must stay inside, and a budget."""

    root: Path
    files: dict[Path, _File | None] = field(default_factory=dict)
    steps: int = 0


_READING: ContextVar[_Reading | None] = ContextVar("agentprint_reading", default=None)


def detect(command: str, cwd: Path, _depth: int = 0) -> list[Risky]:
    """Every publish or push in `command`, in order, with the directory each runs in."""
    if _READING.get() is None:  # the agent's own command line: files are read only inside cwd
        token = _READING.set(_Reading(_real(cwd)))
        try:
            return _detect(command, cwd, _depth)
        finally:
            _READING.reset(token)
    return _detect(command, cwd, _depth)


def _detect(command: str, cwd: Path, depth: int) -> list[Risky]:
    if depth > MAX_DEPTH:
        return []
    seen: set[tuple[Action, str]] = set()  # also counts dry runs, so the backstop skips them
    try:
        found = _parse(command, cwd, depth, seen)
    except ValueError:  # unbalanced quotes and similar
        found = []
    for inner in [*_BACKTICK.findall(command), *_DOLLAR_PAREN.findall(command)]:
        found += detect(inner, cwd, depth + 1)
    known = seen | {(r.action, r.tool) for r in found}
    for risky in _backstop(command, cwd):
        if (risky.action, risky.tool) not in known:
            found.append(risky)
    unique: list[Risky] = []
    for risky in found:
        if risky not in unique:
            unique.append(risky)
    return unique


def _backstop(command: str, cwd: Path) -> list[Risky]:
    """A plain word scan, for shapes the parser doesn't know. Quoted text is ignored."""
    text = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", " ", command)
    found = []
    for match in _BACKSTOP.finditer(text):
        node_or_py, twine, git = match.groups()
        tool = node_or_py or twine or git
        action = Action.push if git else Action.publish
        why = "agentprint could not follow this command line exactly"
        found.append(Risky(action, tool, cwd, unsure=why))
    return found


# ---- parsing --------------------------------------------------------------------------------


def _parse(command: str, cwd: Path, depth: int, seen: set) -> list[Risky]:
    text = _FD_DUP.sub(" ", command.replace("\n", " ; "))
    text = _AMP_REDIRECT.sub(" > ", text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    tokens = list(lexer)
    found: list[Risky] = []
    state = _Where(cwd)
    stack: list[_Where] = []
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if not _SEPARATOR.match(token):
            segment.append(token)
            continue
        state = _segment(segment, state, found, depth, seen)
        segment = []
        if "(" in token:
            stack.extend([state] * token.count("("))
        if ")" in token:
            for _ in range(token.count(")")):
                if stack:
                    state = stack.pop()
    return found


@dataclass(frozen=True)
class _Where:
    cwd: Path
    unknown: str = ""  # set once a `cd` goes somewhere only known at run time


def _strip_redirects(words: list[str]) -> list[str]:
    """Drop `> file`, `2>/dev/null`, `<<EOF` and friends so their targets aren't arguments."""
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif _REDIRECT.match(word):
            skip = True  # bare operator: the next word is its target
        elif not _REDIRECT_ATTACHED.match(word):
            out.append(word)
    return out


def _segment(words: list[str], where: _Where, found: list[Risky], depth: int, seen) -> _Where:
    """Analyse one simple command. Returns where the shell is after it."""
    words = _unwrap(_strip_redirects(words))
    if not words or words[0] in SKIP_SEGMENT:
        return where
    program, args = Path(words[0]).name, words[1:]
    if program in {"cd", "pushd"}:
        return _chdir(where, args[0] if args else "~")
    before = len(found)
    if program in SHELLS:
        script = _shell_script(args)
        if script is not None:
            found.extend(detect(script, where.cwd, depth + 1))
        elif (file := _shell_file_arg(args)) is not None:
            found.extend(_script(file, where.cwd, depth, runner=program))
    elif words[0] in {"source", "."}:
        if args:
            found.extend(_script(args[0], where.cwd, depth, runner="sh"))
    elif program == "eval":
        found.extend(detect(" ".join(args), where.cwd, depth + 1))
    elif program == "git":
        _git(args, where.cwd, found, seen)
    elif program in NODE:
        _node(program, args, where.cwd, found, depth, seen)
    elif program in PYTHON_PUBLISH or program == "twine":
        _python(program, args, where.cwd, found, seen)
    elif program in WORKSPACE_RELEASERS or program in ROOT_RELEASERS:
        _releaser(program, args, where.cwd, found, seen)
    elif program in MAKES:
        found.extend(_make(args, where.cwd, depth))
    elif program == "just":
        found.extend(_just(args, where.cwd, depth))
    elif program in TASKS:
        found.extend(_task(args, where.cwd))
    elif _interpreter(program):
        found.extend(_interpreted(program, args, where.cwd, depth))
    elif "/" in words[0]:  # ./release.sh, scripts/release, /abs/path/in/project/x
        found.extend(_script(words[0], where.cwd, depth))
    if where.unknown:
        found[before:] = [replace(r, unsure=r.unsure or where.unknown) for r in found[before:]]
    return where


def _unwrap(words: list[str]) -> list[str]:
    """Drop env-var prefixes, wrappers and shell keywords until the real program is first."""
    while words:
        head = Path(words[0]).name
        if _ASSIGN.match(words[0]):
            words = words[1:]
        elif head in {"sudo", "doas", "env"}:
            words = words[1:]
            while words and (words[0].startswith("-") or _ASSIGN.match(words[0])):
                flag = words.pop(0)
                sudo_value = head != "env" and flag in {"-u", "-g", "-p", "-C", "-h", "-U", "-r"}
                if sudo_value and words:
                    words.pop(0)
        elif head in {"timeout", "gtimeout"}:
            words = _drop_options(words[1:], {"-s", "-k", "--signal", "--kill-after"})
            words = words[1:]  # the duration
        elif head in {"watch", "xargs"} or head in PREFIXES:
            words = _drop_options(words[1:], PREFIX_VALUES)
        elif head in LAUNCHERS:
            words = _drop_options(words[1:], LAUNCHER_VALUES)
        elif _two_word_launcher(head, words):  # pipx run X, python -m X
            words = words[2:]
        elif head in RUNNERS and len(words) > 2 and words[1] == "run":  # uv run X
            words = _drop_options(words[2:], RUNNER_VALUES)
            if words and words[0].endswith(".py"):  # uv run scripts/release.py
                words = ["python", *words]
        else:
            return words
    return words


def _drop_options(words: list[str], with_value: set[str]) -> list[str]:
    while words and words[0].startswith("-") and words[0] not in {"-", "--"}:
        flag = words.pop(0)
        if flag in with_value and words:
            words.pop(0)
    if words and words[0] == "--":
        words = words[1:]
    return words


def _two_word_launcher(head: str, words: list[str]) -> bool:
    if head == "pipx":
        return len(words) > 2 and words[1] == "run"
    return bool(_PYTHON.match(head)) and len(words) > 2 and words[1] == "-m"


def _chdir(where: _Where, target: str) -> _Where:
    other_users_home = target.startswith("~") and target != "~" and not target.startswith("~/")
    if target == "-" or _UNKNOWABLE.search(target) or other_users_home:
        why = f"its folder ({target!r}) is only known when the command runs"
        return _Where(where.cwd, where.unknown or why)
    return _Where(where.cwd / os.path.expanduser(target), where.unknown)


def _shell_script(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return args[i + 1] if i + 1 < len(args) else None
    return None


@dataclass
class _Options:
    words: list[str]
    cwd: Path
    flags: set[str]
    workspaces: list[str]
    filters: list[str]


def _options(tool: str, args: list[str], cwd: Path) -> _Options:
    """Positional words and the options that matter, for one tool."""
    out = _Options([], cwd, set(), [], [])
    values, cwd_opts = VALUE_OPTS.get(tool, set()), CWD_OPTS.get(tool, set())
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            out.words.extend(args[i + 1 :])
            break
        name, eq, inline = arg.partition("=")
        has_value = bool(eq) and name.startswith("-")
        value = inline if has_value else (args[i + 1] if i + 1 < len(args) else "")
        step = 1 if has_value else 2
        if name in cwd_opts:
            out.cwd = out.cwd / os.path.expanduser(value)
        elif tool == "npm" and name in NPM_WORKSPACE:
            out.workspaces.append(value)
        elif tool == "pnpm" and name in {"--filter", "-F"}:
            out.filters.append(value)
        elif name in values:
            pass
        elif arg.startswith("-"):
            out.flags.add(name)
            step = 1
        else:
            out.words.append(arg)
            step = 1
        i += step
    return out


def _node(tool: str, args: list[str], cwd: Path, found: list[Risky], depth: int, seen) -> None:
    opts = _options(tool, args, cwd)
    words = opts.words
    if not words or words[0] in NOT_PUBLISH:
        return
    if tool == "yarn" and words[0] == "workspace" and len(words) > 2:
        inner: list[Risky] = []
        _node("yarn", words[2:], opts.cwd, inner, depth, seen)
        found.extend(replace(r, workspaces=(words[1],)) for r in inner)
        return
    if tool == "yarn" and words[:2] == ["workspaces", "foreach"]:
        rest = _drop_options(args[args.index("foreach") + 1 :], {"--include", "--exclude"})
        inner = []
        _node("yarn", rest, opts.cwd, inner, depth, seen)  # foreach runs a yarn command
        found.extend(replace(r, all_workspaces=True) for r in inner)
        return
    if tool == "yarn" and words[:2] == ["npm", "publish"]:
        words = words[1:]
    if words[0] == "publish":
        _node_publish(tool, words[1:], opts, found, seen)
        return
    if words[0] == "version" and tool != "bun":  # runs preversion, version and postversion
        if "--ignore-scripts" not in opts.flags and (len(words) > 1 or tool == "yarn"):
            found.extend(_run_script("version", opts.cwd, depth))
        return
    if tool == "bun":  # bun [run] scripts/release.ts runs a file
        file = words[1] if words[0] in RUN_VERBS and len(words) > 1 else words[0]
        if file.endswith(_JS_FILES) and file not in _scripts(opts.cwd):
            found.extend(_script(file, opts.cwd, depth, runner="bun"))
            return
    script = _script_name(tool, words, opts.cwd)
    if script is not None:
        found.extend(_run_script(script, opts.cwd, depth))
    elif words[0] in EXEC_VERBS or tool != "npm":
        rest = args[args.index(words[0]) + 1 :] if words[0] in EXEC_VERBS else words
        found.extend(detect(shlex.join(_drop_options(list(rest), set())), opts.cwd, depth + 1))


def _node_publish(tool: str, targets: list[str], opts: _Options, found: list[Risky], seen) -> None:
    seen.add((Action.publish, tool))
    if opts.flags & DRY_RUN:
        return
    unsure = ""
    if opts.filters:
        unsure = "it publishes the packages a --filter selects, which agentprint can't resolve"
    elif any(_UNKNOWABLE.search(t) for t in targets):
        unsure = "what it publishes is only known when the command runs"
    every = bool(opts.flags & ALL_FLAGS) and not (tool == "pnpm" and "-w" in opts.flags)
    found.append(
        Risky(
            Action.publish,
            tool,
            opts.cwd,
            tuple(targets),
            tuple(opts.workspaces),
            every,
            unsure,
        )
    )


def _script_name(tool: str, words: list[str], cwd: Path) -> str | None:
    if words[0] in RUN_VERBS:
        return words[1] if len(words) > 1 else None
    if tool == "npm":
        return NPM_SCRIPT_ALIASES.get(words[0])
    return words[0] if words[0] in _scripts(cwd) else None


def _scripts(cwd: Path) -> dict[str, str]:
    try:
        data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)}


def _run_script(name: str, cwd: Path, depth: int) -> list[Risky]:
    """What `npm run <name>` would publish: its pre/post scripts and itself, read from
    package.json (nothing is run)."""
    scripts = _scripts(cwd)
    found: list[Risky] = []
    for key in (f"pre{name}", name, f"post{name}"):
        if key in scripts:
            found.extend(detect(scripts[key], cwd, depth + 1))
    return found


def _releaser(tool: str, args: list[str], cwd: Path, found: list[Risky], seen) -> None:
    opts = _options(tool, args, cwd)
    if tool in WORKSPACE_RELEASERS and (not opts.words or opts.words[0] != "publish"):
        return
    seen.add((Action.publish, tool))
    if opts.flags & (DRY_RUN | {"-d"}):
        return
    found.append(Risky(Action.publish, tool, cwd, all_workspaces=tool in WORKSPACE_RELEASERS))


def _python(tool: str, args: list[str], cwd: Path, found: list[Risky], seen) -> None:
    opts = _options(tool, args, cwd)
    verb = "upload" if tool == "twine" else "publish"
    if not opts.words or opts.words[0] != verb:
        return
    seen.add((Action.publish, tool))
    if opts.flags & DRY_RUN:
        return
    targets = tuple(opts.words[1:]) if tool in {"uv", "twine"} else ()
    unsure = (
        "what it uploads is only known when the command runs"
        if any("$" in t or "`" in t for t in targets)
        else ""
    )
    found.append(Risky(Action.publish, tool, opts.cwd, targets, unsure=unsure))


def _git(args: list[str], cwd: Path, found: list[Risky], seen) -> None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        name, eq, value = args[i].partition("=")
        if name == "-C" and not eq and i + 1 < len(args):
            cwd = cwd / os.path.expanduser(args[i + 1])
            i += 2
        elif name in VALUE_OPTS["git"] and not eq:
            i += 2
        else:
            i += 1
    if i < len(args) and args[i] == "push":
        seen.add((Action.push, "git"))
        if {"--dry-run", "-n"} & set(args[i + 1 :]):
            return
        found.append(Risky(Action.push, "git", cwd))


# ---- files a command runs: scripts, Makefiles, justfiles, Taskfiles -------------------------

_SHELL_VALUES = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
MAKEFILES = ("GNUmakefile", "makefile", "Makefile")  # the order make looks for them
MAX_INCLUDES = 16  # makefiles or justfiles read for one make or just command


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _reading(cwd: Path) -> _Reading:
    reading = _READING.get()
    return reading if reading is not None else _Reading(_real(cwd))


def _read(arg: str, cwd: Path) -> _File | None:
    """A file the command runs, read when it is inside the folder the agent's command runs in.

    None when it is missing, not a regular file, outside that folder (symlinks resolved), named
    through a variable, or not text (a compiled program): none of those are followed."""
    if not arg or _UNKNOWABLE.search(arg):
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
    if not _is_file(path):  # folders, and FIFOs or devices that could block a read
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        why = f"agentprint could not read {name} to see whether it publishes or pushes"
        return _File(path, name, None, why)
    if b"\0" in data:
        return None  # a compiled program, not a script
    if len(data) > MAX_FILE_BYTES:
        why = (
            f"{name} is larger than 64 KB, so agentprint did not read it to see whether it "
            "publishes or pushes"
        )
        return _File(path, name, None, why)
    return _File(path, name, data.decode("utf-8", "replace").removeprefix("\ufeff"))


def _cannot(file: _File, cwd: Path, runner: str) -> list[Risky]:
    return [Risky(Action.publish, runner, cwd, unsure=file.problem)]


def _mentions(text: str, name: str, cwd: Path, deep: bool = False) -> list[Risky]:
    """A publish or push named in text agentprint does not read as commands: never OK."""
    where = f"{name}, deeper in its scripts than agentprint follows" if deep else name
    found: list[Risky] = []
    for match in _NAMED_PUBLISH.finditer(text):
        tool = next(group for group in match.groups() if group)
        action = Action.push if tool == "git" else Action.publish
        verb = "pushes" if action is Action.push else "publishes"
        risky = Risky(action, tool, cwd, unsure=f"{verb} from inside {where}; could not check")
        if risky not in found:
            found.append(risky)
    return found


def _step(step: recipes.Step, name: str, cwd: Path, depth: int) -> list[Risky]:
    """One script or recipe line from file `name`. Shell is read as commands; other languages
    are only scanned for a publish or push they name."""
    if Path(step.program).name not in SHELLS:
        return _mentions(step.text, name, cwd)
    text, other = recipes.shell_text(step.text)
    found = [risky for body in other for risky in _mentions(body, name, cwd)]
    reading = _reading(cwd)
    if depth >= MAX_DEPTH or reading.steps >= MAX_STEPS:  # stop following: never silently OK
        found += _mentions(text, name, cwd, deep=True)
        if not found and _RUNS_A_FILE.search(text):
            why = f"{name} runs more scripts than agentprint follows, so it could not check them"
            found.append(Risky(Action.publish, "sh", cwd, unsure=why))
        return found
    reading.steps += 1
    return found + detect(text, cwd, depth + 1)


def _script(arg: str, cwd: Path, depth: int, runner: str | None = None) -> list[Risky]:
    """`./release.sh`, `bash scripts/release.sh`, `python release.py`: what the file runs."""
    file = _read(arg, cwd)
    if file is None:
        return []
    if file.text is None:
        return _cannot(file, cwd, runner or "sh")
    program = runner or recipes.interpreter(file.text, file.name)
    return _step(recipes.Step(file.text, program), file.name, cwd, depth)


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
    return bool(_PYTHON.match(program)) or program in INTERPRETERS or program == "nodejs"


def _interpreted(program: str, args: list[str], cwd: Path, depth: int) -> list[Risky]:
    """`python scripts/release.py`, `node -e '...'`, `python -u -m twine upload`."""
    key = "python" if _PYTHON.match(program) else "node" if program == "nodejs" else program
    code, values = INTERPRETERS[key]
    if key == "deno":
        if args[:1] == ["eval"]:
            return _mentions(" ".join(args[1:]), "the deno eval code", cwd)
        if args[:1] != ["run"]:
            return []
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        flag, eq, inline = arg.partition("=")
        if key == "python" and arg == "-m":
            return detect(shlex.join(args[i + 1 :]), cwd, depth + 1)
        if flag in code:
            text = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            return _mentions(text, f"the {program} {flag} code", cwd)
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


def _make(args: list[str], cwd: Path, depth: int) -> list[Risky]:
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
        elif eq and _ASSIGN.match(arg):
            overrides[flag] = inline
        else:
            goals.append(arg)
        if valued and valued[0] in {"-C", "--directory"}:
            dirs.append(valued[1])
        elif valued and valued[0] in {"-f", "--file", "--makefile"}:
            files.append(valued[1])
    for folder in dirs:
        if not folder or _UNKNOWABLE.search(folder):
            return []
        cwd = cwd / os.path.expanduser(folder)
    if not files:
        try:
            present = set(os.listdir(cwd))
        except OSError:
            return []
        files = [name for name in MAKEFILES if name in present][:1]
    mf: recipes.Makefile | None = None
    name = ""
    incomplete = False
    queue = [(path, False) for path in files]
    read = 0
    while queue and read < MAX_INCLUDES:
        path, optional = queue.pop(0)
        file = _read(path, cwd)
        if file is None:
            if mf is None:
                return []  # the makefile is missing, outside the folder, or not text
            incomplete = incomplete or not optional
            continue
        if file.text is None:
            return _cannot(file, cwd, "make")
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
    if mf is None:
        return []
    steps, missing = recipes.make_steps(mf, goals, overrides)
    found: list[Risky] = []
    if missing and (incomplete or queue):
        why = f"`make {' '.join(missing)}` may run a recipe from a makefile agentprint did not read"
        found.append(Risky(Action.publish, "make", cwd, unsure=why))
    for text in steps:
        found += _step(recipes.Step(text), name, cwd, depth)
    return found


def _just(args: list[str], cwd: Path, depth: int) -> list[Risky]:
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
            return detect(shlex.join(args[i:]), cwd, depth + 1)
        if flag == "--set":
            i += 2
        elif flag in _JUST_VALUES:
            value = inline if eq else (args[i] if i < len(args) else "")
            i += 0 if eq else 1
            if flag in {"-f", "--justfile"}:
                justfile = value
            elif flag in {"-d", "--working-directory"}:
                workdir = value
        elif not arg.startswith("-") and not _ASSIGN.match(arg):
            names.append(arg)
    path = cwd / os.path.expanduser(justfile) if justfile else _find_up(cwd, JUSTFILES)
    if path is None:
        return []
    main = _read(str(path), cwd)
    if main is None:
        return []
    if main.text is None:
        return _cannot(main, cwd, "just")
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
            return _cannot(file, cwd, "just")
        before = len(jf.imports)
        jf = recipes.parse_just(file.text, jf)
        queue += [(p, optional, file.path.parent) for p, optional in jf.imports[before:]]
        read += 1
    names = names[:1] + [n for n in names[1:] if n in jf.recipes or n in jf.aliases]  # args
    steps, missing = recipes.just_steps(jf, names)
    found: list[Risky] = []
    if missing and (incomplete or queue or jf.modules):
        why = f"`just {' '.join(missing)}` may run a recipe from a just module or import"
        found.append(Risky(Action.publish, "just", cwd, unsure=why + " agentprint did not read"))
    home = cwd if main.path.parent == _real(cwd) else main.path.parent  # where just runs them
    folder = home / jf.workdir if jf.workdir else home
    folder = cwd / workdir if workdir else folder
    for step in steps:
        found += _step(step, main.name, cwd if step.here else folder, depth)
    if "$" in str(folder):
        why = "the folder its recipes run in is only known when just runs"
        found = [replace(r, unsure=r.unsure or why) for r in found]
    return found


def _task(args: list[str], cwd: Path) -> list[Risky]:
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
        elif not arg.startswith("-") and not _ASSIGN.match(arg):
            names.append(arg)
    path = folder / taskfile if taskfile else _find_up(folder, TASKFILES)
    if path is None:
        return []
    file = _read(str(path), cwd)
    if file is None:
        return []
    if file.text is None:
        return _cannot(file, cwd, "task")
    return _mentions(recipes.taskfile_text(file.text, names), file.name, cwd)
