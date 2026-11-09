"""Spot the risky moments in a shell command line: publishing a package, or `git push`.

This is a best-effort reader of what an agent is about to run, not a shell. It follows `cd`,
`git -C`, env-var prefixes, wrappers (`env`, `sudo`, `timeout`, `xargs`, `uvx`, `npx`,
`corepack`, `python -m`, `uv run`, `poetry run`, `bash -c`, `eval`), shell keywords
(`if/then/else`, `for/while ... do`, `{ ...; }`, `!`), chains (`&&`, `||`, `;`, `|`, newlines,
subshells), command substitutions (`$(...)`, backticks), package.json scripts (`npm run release`,
and the `preversion`/`version`/`postversion` scripts `npm version` runs), workspace publishes
(`npm publish -w pkg`, `pnpm -r publish`) and release tools (changesets, lerna, np,
semantic-release, release-it). A word scan over the unquoted text runs as a backstop.

It also follows the files a command hands its work to (`following`): shell scripts, Makefile
targets, justfile recipes, and scripts in other languages, read only inside the folder the
agent's command runs in. Where following stops (`MAX_DEPTH` levels here, budgets there) the text
is read once more without following anything: its own commands are checked, and the files it
runs are only scanned for a publish or push ("could not check" when one is named).

When it can't tell what a publish would upload (a `cd "$DIR"`, a `--filter` selector, a
target built from a variable) the result says so in `unsure`; the guard then reports
"could not check" instead of "OK". A script shlex can't read as a whole is read line by line.
Here-documents on the command line itself are not understood yet (their bodies are read as
commands).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from . import following


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
    label: str = ""  # the file or command `unsure` is about, when it is one (`configure`)


NODE = {"npm", "pnpm", "yarn", "bun"}
PYTHON_PUBLISH = {"uv", "poetry", "hatch", "flit"}
WORKSPACE_RELEASERS = {"changeset", "lerna"}  # `<tool> publish` publishes workspace packages
ROOT_RELEASERS = {"np", "semantic-release", "release-it"}  # publish the package they run in
MAX_DEPTH = 4  # nested bash -c / eval / package scripts / script files

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
_JS_FILES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^(python[0-9.]*|py|pypy[0-9.]*)$")
_SEPARATOR = re.compile(r"^[;&|()]+$")
_REDIRECT = re.compile(r"^[0-9]*(?:>>?|<<?<?)$")
_REDIRECT_ATTACHED = re.compile(r"^[0-9]*(?:>>?|<<?<?)\S")
_FD_DUP = re.compile(r"[0-9]*[<>]&[0-9-]+")  # 2>&1, >&2: the '&' is not a separator here
_AMP_REDIRECT = re.compile(r"&>>?")  # &>file sends both streams to file
_UNKNOWABLE = re.compile(r"[$`*?]")  # variables, substitutions and globs: known only at run time
_VARIABLE = re.compile(r"[$`]")  # a word only the shell can fill in
_DEFAULTED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([A-Za-z0-9_.+-]+)\}")  # ${NPM:-npm}
PUBLISH_VERBS = {"publish": Action.publish, "upload": Action.publish, "push": Action.push}
# Subcommands that show a variable before them only held options (`git $FLAGS commit`).
_OTHER_VERBS: dict[str, set[str]] = {
    "git": {
        "status",
        "commit",
        "log",
        "diff",
        "add",
        "fetch",
        "pull",
        "checkout",
        "switch",
        "branch",
        "merge",
        "rebase",
        "tag",
        "show",
        "clone",
        "init",
        "stash",
        "reset",
        "restore",
        "remote",
        "rm",
        "mv",
        "config",
        "rev-parse",
        "describe",
        "blame",
        "grep",
        "worktree",
        "submodule",
        "cherry-pick",
        "revert",
        "bisect",
        "apply",
        "am",
        "clean",
        "ls-files",
        "ls-remote",
    },
    **{
        tool: NOT_PUBLISH
        | RUN_VERBS
        | {
            "test",
            "t",
            "start",
            "build",
            "exec",
            "x",
            "dlx",
            "info",
            "view",
            "ls",
            "list",
            "outdated",
            "audit",
            "whoami",
            "login",
            "link",
            "prune",
            "why",
            "create",
            "config",
            "version",
        }
        for tool in ("npm", "pnpm", "yarn", "bun")
    },
    **{
        tool: {
            "build",
            "sync",
            "run",
            "lock",
            "add",
            "remove",
            "pip",
            "venv",
            "tool",
            "install",
            "test",
            "env",
            "version",
            "shell",
            "new",
            "init",
            "check",
        }
        for tool in ("uv", "poetry", "hatch", "flit", "twine")
    },
}
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
_OPTION = rf"\s+-{_WORD}+"  # an option; like every word, it stops at a separator
_OPTION_VALUE = rf"\s+[^-\s;&|(){{}}!]{_WORD}*"
_BACKSTOP = re.compile(
    _COMMAND_POSITION + r"(?:(npm|pnpm|yarn|bun|uv|poetry|hatch|flit|changeset|lerna)"
    rf"(?:{_OPTION})*\s+publish\b|(twine)(?:{_OPTION})*\s+upload\b"
    rf"|(git)(?:{_OPTION}(?:{_OPTION_VALUE})?)*\s+push\b)"
)


def detect(command: str, cwd: Path, _depth: int = 0) -> list[Risky]:
    """Every publish or push in `command`, in order, with the directory each runs in."""
    if following._READING.get() is None:  # the agent's own command line: files stay in cwd
        token = following._READING.set(following._Reading(following._real(cwd), command))
        try:
            return _detect(command, cwd, _depth)
        finally:
            following._READING.reset(token)
    return _detect(command, cwd, _depth)


def _detect(command: str, cwd: Path, depth: int) -> list[Risky]:
    if depth > MAX_DEPTH:  # never a silent OK: look once more without following anything
        return following.too_deep(command, cwd)
    seen: set[tuple[Action, str]] = set()  # also counts dry runs, so the backstop skips them
    try:
        found = _parse(command, cwd, depth, seen)
    except ValueError:  # unbalanced quotes and similar: read a script line by line instead
        found = _parse_lines(command, cwd, depth, seen) if "\n" in command.strip() else []
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
    found: list[Risky] = []
    _parse_into(command, _Where(cwd), depth, seen, found)
    return found


def _parse_lines(command: str, cwd: Path, depth: int, seen: set) -> list[Risky]:
    """A script one line at a time, so one line shlex can't read hides only itself (the
    backstop still scans it). A `cd` carries on to the next line, as in the script."""
    found: list[Risky] = []
    where = _Where(cwd)
    for line in command.split("\n"):
        try:
            where = _parse_into(line, where, depth, seen, found)
        except ValueError:
            continue
    return found


def _parse_into(command: str, where: _Where, depth: int, seen: set, found: list[Risky]) -> _Where:
    """Add what `command` publishes to `found`; returns where the shell is after it."""
    tokens = _tokens(_without_ansi_c(command))  # quote-aware: a 'v1$' regex stays as it is
    state = where
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
    return state


def _tokens(command: str) -> list[str]:
    text = _FD_DUP.sub(" ", command.replace("\n", " ; "))
    text = _AMP_REDIRECT.sub(" > ", text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    return list(lexer)


def _without_ansi_c(command: str) -> str:
    """`command` as shlex can read it the way bash does: each ANSI-C `$'...'` string outside
    quotes (shlex can't read its `\\'`) made `''`, and inside double quotes `\\$` and `\\``
    made `$` and `` ` `` (shlex keeps the backslash; bash drops it, so `bash -c "\\$'x'"` runs
    `$'x'`). Only a `$'` that is not inside a quote starts one: in `grep 'v1$' x` the `$'` is a
    regex end and a closing quote."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        char = command[i]
        if quote:
            if char == "\\" and quote == '"':
                pair = command[i : i + 2]
                out.append(pair[1:] if pair[1:] in {"$", "`"} else pair)
                i += 2
                continue
            quote = "" if char == quote else quote
        elif char == "\\":
            out.append(command[i : i + 2])
            i += 2
            continue
        elif command.startswith("$'", i):
            i += 2
            while i < len(command) and command[i] != "'":
                i += 2 if command[i] == "\\" else 1
            out.append("''")
            i += 1
            continue
        elif char in "'\"":
            quote = char
        out.append(char)
        i += 1
    return "".join(out)


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


def _positional(program: str, args: list[str]) -> list[str]:
    """The words that are not options (nor an option's value)."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("-"):
            name, eq, _ = arg.partition("=")
            takes = VALUE_OPTS.get(program, set()) | ({"-C", "-c"} if program == "git" else set())
            skip = name in takes and not eq
        else:
            out.append(arg)
    return out


def _named_by_variable(first: str, program: str, args: list[str], cwd: Path) -> Risky | None:
    """A publish or push whose program or subcommand is a variable (`"$NPM" publish`,
    `npm $CMD`): what runs is known only when the command runs, so it is "could not check",
    never nothing. A variable followed by an ordinary subcommand only held options."""
    plain = _positional(program, args)
    if _VARIABLE.search(first):
        verb = plain[0] if plain else ""
        if verb not in PUBLISH_VERBS:
            return None
        why = "the program is a variable, known only when the command runs"
        return Risky(PUBLISH_VERBS[verb], "sh", cwd, unsure=why, label=f"{first} {verb}")
    others = _OTHER_VERBS.get(program)
    if others is None or not plain or not _VARIABLE.search(plain[0]):
        return None
    later = plain[1:]
    if not any(word in PUBLISH_VERBS for word in later) and any(word in others for word in later):
        return None  # `git $FLAGS commit`: the variable held options
    action = Action.push if program == "git" else Action.publish
    why = "its subcommand is a variable, known only when the command runs"
    return Risky(action, program, cwd, unsure=why, label=f"{program} {plain[0]}")


def _segment(words: list[str], where: _Where, found: list[Risky], depth: int, seen) -> _Where:
    """Analyse one simple command. Returns where the shell is after it."""
    words = _unwrap(_strip_redirects(words))
    if not words or words[0] in SKIP_SEGMENT:
        return where
    if (default := _DEFAULTED.fullmatch(words[0])) is not None:
        words = [default.group(1), *words[1:]]  # ${NPM:-npm} publish: check it as npm publish
    program, args = Path(words[0]).name, words[1:]
    if program in {"cd", "pushd"}:
        return _chdir(where, _cd_target(args))
    before = len(found)
    if program in SHELLS:
        script = _shell_script(args)
        if script is not None:
            found.extend(detect(script, where.cwd, depth + 1))
        elif (file := following._shell_file_arg(args)) is not None:
            rest = args[args.index(file) + 1 :]  # what the script gets as "$@"
            found.extend(following._script(file, where.cwd, depth, program, where.unknown, rest))
    elif words[0] in {"source", "."}:
        if args:
            found.extend(
                following._script(args[0], where.cwd, depth, "sh", where.unknown, args[1:])
            )
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
    elif program in following.MAKES:
        found.extend(following._make(args, where.cwd, depth))
    elif program == "just":
        found.extend(following._just(args, where.cwd, depth))
    elif program in following.TASKS:
        found.extend(following._task(args, where.cwd))
    elif following._interpreter(program):
        found.extend(following._interpreted(program, args, where.cwd, depth))
    elif "/" in words[0]:  # ./release.sh, scripts/release, /abs/path/in/project/x
        found.extend(following._script(words[0], where.cwd, depth, None, where.unknown, args))
    if len(found) == before and (named := _named_by_variable(words[0], program, args, where.cwd)):
        found.append(named)
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


def _cd_target(args: list[str]) -> str:
    """The folder in `cd [-L|-P|-e|-@] [--] DIR` (home when none)."""
    for i, arg in enumerate(args):
        if arg == "--":
            return args[i + 1] if i + 1 < len(args) else "~"
        if arg == "-" or not arg.startswith("-"):
            return arg
    return "~"


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
            found.extend(following._script(file, opts.cwd, depth, runner="bun"))
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
