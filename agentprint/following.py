"""Follow the files a command hands its work to: shell scripts, Makefiles, justfiles, Taskfiles,
and scripts in other languages.

`commands` reads the command line; for a command that runs a file it asks this module, which
reads the file (inside the folder of the agent's command, 64 KB at most, text only) and sends
shell text back through `commands.detect`. The two call each other, so this module looks
`commands` names up when it runs, never at import.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import commands as c
from . import recipes

MAX_FILE_BYTES = 64 * 1024  # each script, Makefile, justfile or Taskfile is read up to this
MAX_STEPS = 400  # file commands followed per command line; later ones are only word-scanned
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


def _cannot(file: _File, cwd: Path, runner: str) -> list[c.Risky]:
    return [c.Risky(c.Action.publish, runner, cwd, unsure=file.problem)]


def _mentions(text: str, name: str, cwd: Path, deep: bool = False) -> list[c.Risky]:
    """A publish or push named in text agentprint does not read as commands: never OK."""
    where = f"{name}, deeper in its scripts than agentprint follows" if deep else name
    found: list[c.Risky] = []
    for match in _NAMED_PUBLISH.finditer(text):
        tool = next(group for group in match.groups() if group)
        action = c.Action.push if tool == "git" else c.Action.publish
        verb = "pushes" if action is c.Action.push else "publishes"
        risky = c.Risky(action, tool, cwd, unsure=f"{verb} from inside {where}; could not check")
        if risky not in found:
            found.append(risky)
    return found


def _step(step: recipes.Step, name: str, cwd: Path, depth: int) -> list[c.Risky]:
    """One script or recipe line from file `name`. Shell is read as commands; other languages
    are only scanned for a publish or push they name."""
    if Path(step.program).name not in c.SHELLS:
        return _mentions(step.text, name, cwd)
    text, other = recipes.shell_text(step.text)
    found = [risky for body in other for risky in _mentions(body, name, cwd)]
    reading = _reading(cwd)
    if depth >= c.MAX_DEPTH or reading.steps >= MAX_STEPS:  # stop following: never silently OK
        found += _mentions(text, name, cwd, deep=True)
        if not found and c._RUNS_A_FILE.search(text):
            why = f"{name} runs more scripts than agentprint follows, so it could not check them"
            found.append(c.Risky(c.Action.publish, "sh", cwd, unsure=why))
        return found
    reading.steps += 1
    return found + c.detect(text, cwd, depth + 1)


def _script(arg: str, cwd: Path, depth: int, runner: str | None = None) -> list[c.Risky]:
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
    return bool(c._PYTHON.match(program)) or program in INTERPRETERS or program == "nodejs"


def _interpreted(program: str, args: list[str], cwd: Path, depth: int) -> list[c.Risky]:
    """`python scripts/release.py`, `node -e '...'`, `python -u -m twine upload`."""
    key = "python" if c._PYTHON.match(program) else "node" if program == "nodejs" else program
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
            return c.detect(shlex.join(args[i + 1 :]), cwd, depth + 1)
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
    found: list[c.Risky] = []
    if missing and (incomplete or queue):
        why = f"`make {' '.join(missing)}` may run a recipe from a makefile agentprint did not read"
        found.append(c.Risky(c.Action.publish, "make", cwd, unsure=why))
    for text in steps:
        found += _step(recipes.Step(text), name, cwd, depth)
    return found


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
    found: list[c.Risky] = []
    if missing and (incomplete or queue or jf.modules):
        why = f"`just {' '.join(missing)}` may run a recipe from a just module or import"
        why += " agentprint did not read"
        found.append(c.Risky(c.Action.publish, "just", cwd, unsure=why))
    home = cwd if main.path.parent == _real(cwd) else main.path.parent  # where just runs them
    folder = home / jf.workdir if jf.workdir else home
    folder = cwd / workdir if workdir else folder
    for step in steps:
        found += _step(step, main.name, cwd if step.here else folder, depth)
    if "$" in str(folder):
        why = "the folder its recipes run in is only known when just runs"
        found = [replace(r, unsure=r.unsure or why) for r in found]
    return found


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
        return _cannot(file, cwd, "task")
    return _mentions(recipes.taskfile_text(file.text, names), file.name, cwd)
