"""Spot the risky moments in a shell command line: publishing a package, or `git push`.

This is a best-effort reader of what an agent is about to run, not a shell. The text is read by a
real tokenizer (`shell`): quotes, `$'...'` strings, here-documents, comments and substitutions are
read the way the shell reads them, and nothing is cut out of the text before it is read.

The reader follows `cd`, `git -C`, env-var prefixes, wrappers (`env`, `sudo`, `timeout`, `xargs`,
`uvx`, `npx`, `corepack`, `python -m`, `uv run`, `poetry run`), `bash -c`, `eval`, `source`,
functions, aliases, shell variables (filled in when they hold a literal; set inside an `if`, a
loop, a function or after `&&`/`||` they are only known at run time), pipes into a shell, `xargs`,
here-documents and files written by the command line and run later in it, package.json scripts,
workspace publishes and release tools. It also follows the files a command hands its work to
(`following`): shell scripts with their arguments, Makefile targets, justfile recipes, and code in
other languages, read only inside the folder the agent's command runs in.

The reader can only add risk. When it is done, a backstop scans every text it read, as written,
for a publish or push command (npm/pnpm/yarn/bun/uv/poetry/hatch/flit/cargo publish, twine upload,
git/docker push, gh release create/upload, the release tools). One the reader did not account for
is "could not check", never nothing. Only provably inert text is left out: comments, a function
nobody calls, the message of `git commit -m`/`git tag -m`, the body or title of `gh pr create`,
what `echo`/`printf` print when it goes nowhere that runs it, grep patterns, here-documents that
are only data, and variables nothing runs. Where the reader stops (`MAX_DEPTH` levels, budgets in
`following`, `MAX_VALUES` variables...) the text is still scanned, so a limit means "could not
check", never a silent OK.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from . import following, shell
from .shell import AndOr, Compound, Function, Heredoc, Part, Pipeline, Simple, Word


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
# npm takes any unambiguous start of a command or alias name (npm 11, lib/utils/cmd-list.js):
# `npm pu`, `npm publ` and `npm publis` all publish. pnpm, yarn and bun take no abbreviations.
_NPM_COMMANDS = (
    "access", "adduser", "audit", "bugs", "cache", "ci", "completion", "config", "dedupe",
    "deprecate", "diff", "dist-tag", "docs", "doctor", "edit", "exec", "explain", "explore",
    "find-dupes", "fund", "get", "help", "help-search", "init", "install", "install-ci-test",
    "install-test", "link", "ll", "login", "logout", "ls", "org", "outdated", "owner", "pack",
    "ping", "pkg", "prefix", "profile", "prune", "publish", "query", "rebuild", "repo", "restart",
    "root", "run", "run-script", "sbom", "search", "set", "shrinkwrap", "star", "stars", "start",
    "stop", "team", "test", "token", "trust", "undeprecate", "uninstall", "unpublish", "unstar",
    "update", "version", "view", "whoami",
)  # fmt: skip
_NPM_ALIASES = {
    "author": "owner", "home": "docs", "issues": "bugs", "info": "view", "show": "view",
    "find": "search", "add": "install", "unlink": "uninstall", "remove": "uninstall",
    "rm": "uninstall", "r": "uninstall", "un": "uninstall", "rb": "rebuild", "list": "ls",
    "ln": "link", "create": "init", "i": "install", "it": "install-test",
    "cit": "install-ci-test", "up": "update", "c": "config", "s": "search", "se": "search",
    "tst": "test", "t": "test", "ddp": "dedupe", "v": "view", "clean-install": "ci",
    "clean-install-test": "install-ci-test", "x": "exec", "why": "explain", "la": "ll",
    "verison": "version", "ic": "ci", "innit": "init", "in": "install", "ins": "install",
    "inst": "install", "insta": "install", "instal": "install", "isnt": "install",
    "isnta": "install", "isntal": "install", "isntall": "install", "install-clean": "ci",
    "isntall-clean": "ci", "hlep": "help", "dist-tags": "dist-tag", "upgrade": "update",
    "udpate": "update", "rum": "run-script", "sit": "install-ci-test", "urn": "run-script",
    "ogr": "org", "add-user": "adduser",
}  # fmt: skip


def _abbrev(names: list[str]) -> dict[str, str]:
    """Every unambiguous start of each name -> the name (npm's `abbrev` package)."""
    out: dict[str, str] = {}
    ordered = sorted(set(names))
    for k, name in enumerate(ordered):
        before = ordered[k - 1] if k else ""
        after = ordered[k + 1] if k + 1 < len(ordered) else ""
        shared = 0
        while shared < len(name) and (
            name[: shared + 1] == before[: shared + 1] or name[: shared + 1] == after[: shared + 1]
        ):
            shared += 1
        for end in range(min(shared + 1, len(name)), len(name) + 1):
            out[name[:end]] = name
        out[name] = name
    return out


_NPM_ABBREV = _abbrev([*_NPM_COMMANDS, *_NPM_ALIASES])


def npm_command(word: str) -> str:
    """The npm command a word names, the way npm reads it (`pub` -> publish, `t` -> test,
    `runScript` -> run-script); the word itself when npm knows no such command."""
    if any(char.isupper() for char in word):
        word = re.sub(r"[A-Z]", lambda m: "-" + m.group().lower(), word)
    if word in _NPM_COMMANDS:
        return word
    name = _NPM_ALIASES.get(word) or _NPM_ABBREV.get(word, word)
    for _ in range(4):
        name = _NPM_ALIASES.get(name, name)
    return name


NPM_PUBLISH_WORDS = frozenset(w for w in _NPM_ABBREV if npm_command(w) == "publish")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "mksh", "yash", "posh", "oksh",
          "rbash", "tcsh", "csh"}  # fmt: skip
LAUNCHERS = {"uvx", "npx", "pnpx", "bunx", "corepack"}  # run the next word as a program
LAUNCHER_VALUES = {"--from", "--with", "-p", "--package", "--python", "-c", "--call"}
# Programs that run the rest of the line, and their options that take a value.
PREFIXES: dict[str, set[str]] = {
    "time": {"-f", "--format", "-o", "--output"},
    "nohup": set(),
    "command": set(),
    "exec": {"-a"},
    "builtin": set(),
    "nice": {"-n", "--adjustment"},
    "caffeinate": {"-t", "-w"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"},
    "chronic": set(),
    "unbuffer": set(),
    "watch": {"-n", "--interval", "-d", "-q", "--equexit", "--color"},
    "doas": {"-u", "-C"},
    "flock": {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
    "setsid": set(),
    "taskset": set(),
}
SUDO_VALUES = {"-u", "-g", "-p", "-C", "-h", "-U", "-r", "-t", "-T", "-D", "--user", "--group",
               "--prompt", "--close-from", "--host", "--other-user", "--role", "--type",
               "--command-timeout", "--chdir"}  # fmt: skip
ENV_VALUES = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-a", "--argv0"}
RUNNERS = {"uv", "poetry", "pdm", "pipenv", "rye", "hatch"}  # `<runner> run X` runs X
RUNNER_VALUES = {"--with", "-w", "--with-requirements", "--with-editable", "--python", "-p",
                 "--package", "--extra", "--group", "--only-group", "--env-file", "--index",
                 "--index-url", "--extra-index-url", "-e", "--env"}  # fmt: skip
_JS_FILES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^(python[0-9.]*|py|pypy[0-9.]*)$")
_UNKNOWABLE = re.compile(r"[$`*?]")  # variables, substitutions and globs: known only at run time
_VARIABLE = re.compile(r"[$`]")  # a word only the shell can fill in
_DEFAULTED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([A-Za-z0-9_.+-]+)\}")  # ${NPM:-npm}
PUBLISH_VERBS = {"publish": Action.publish, "upload": Action.publish, "push": Action.push}
# A publish, upload or push word: what makes text only known at run time worth a "could not check".
EVIDENCE = re.compile(r"(?=[pu])(?<![\w$-])(publish|upload|push)(?![\w-])", re.I)
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

MAX_VALUES = 64  # shell variables remembered per text; others are only known at run time
MAX_VALUE = 4096  # characters in one remembered value
MAX_FILLED = 16384  # characters filling in may add to one command
MAX_CALLS = 256  # function calls followed per command line
MAX_CALL_DEPTH = 16  # functions calling functions
MAX_EVIDENCE = 4096  # characters of assignments kept per variable, to look for a publish word
MAX_XARGS = 16  # lines of input an `xargs -I` runs a command for, when it is known
MAX_COMMANDS = 8000  # simple commands read per command line; past it, only scanned
MAX_FILLED_TOTAL = 256 * 1024  # characters filling in may add per command line

_UNFOLLOWED = "it is written in a way agentprint could not follow exactly"
_RUNTIME = "it runs `{what}`, known only when it runs, and what it is set from names `{verb}`"
_BUILT_TEXT = "it runs text a pipeline builds at run time"
_FROM_INPUT = "it runs `{tool}` with a subcommand from its input, known only when it runs"
_BRANCH = "it runs after a `cd` that may or may not happen, so its folder is only known then"


# ---- state ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Var:
    value: str | None  # None: only known at run time
    origins: tuple = ()  # the words (and here-documents) the value came from
    evidence: str = ""  # every assignment, as written: does it name a publish?
    exported: bool = False
    unset: bool = False  # `unset X`: known to hold nothing at all (not even an empty value)


@dataclass(frozen=True, slots=True)
class _Value:
    """Text a command prints or reads, when it can be told: echo/printf/cat/here-documents."""

    text: str | None  # None: only known at run time
    origins: tuple = ()
    built: bool = False  # unknown, but made only from literal text (`echo x | rev`)


_UNKNOWN = _Value(None)
_NOTHING = _Value("")


@dataclass(eq=False)
class _Func:
    node: Function
    where: _Where
    reached: bool = False  # called, or maybe called through a variable
    ran: bool = False  # its body was read
    exported: bool = False


@dataclass(frozen=True)
class _Where:
    cwd: Path
    unknown: str = ""  # set once a `cd` goes somewhere only known at run time
    vars: dict = field(default_factory=dict)  # name -> _Var; replaced, never changed in place
    funcs: dict = field(default_factory=dict)  # name -> _Func
    aliases: dict = field(default_factory=dict)  # name -> (text, origins)
    args: tuple[str, ...] | None = ()  # positional parameters; None when only known at run time
    ifs: str | None = " \t\n"
    allexport: bool = False
    alts: tuple[Path, ...] = ()  # other folders it may be in, after a `cd` that may not happen


@dataclass(eq=False)
class _Text:
    """One text the reader read: kept until the end for the backstop."""

    source: str
    nodes: list
    cwd: Path
    sourced: bool = False  # read with `source`: its variables live on in the caller


@dataclass
class _Ctx:
    found: list
    depth: int
    certain: bool = True  # every command so far runs for sure (not after `&&`, in an `if`...)
    label: str = ""  # what an unknown command is reported as ("eval")
    calls: int = 0
    locals: set = field(default_factory=set)
    mark: int = 0  # where the found list stood when the command being memoised started


@dataclass(slots=True)
class _Arg:
    text: str
    word: Word | None = None
    origins: tuple = ()
    known: bool = True


# ---- entry points ---------------------------------------------------------------------------


def detect(command: str, cwd: Path, _depth: int = 0) -> list[Risky]:
    """Every publish or push in `command`, in order, with the directory each runs in."""
    if following._READING.get() is None:  # the agent's own command line: files stay in cwd
        reading = following._Reading(following._real(cwd), command)
        token = following._READING.set(reading)
        try:
            found, _ = _read(command, _Where(cwd), _depth)
            found += _reach_dynamic(reading)
            found += _overflow(reading)
            found += _backstops(reading)
            return _unique(found)
        finally:
            following._READING.reset(token)
    return _unique(_read(command, _Where(cwd), _depth)[0])


def _detect(command: str, cwd: Path, depth: int) -> list[Risky]:
    """`command` read at `depth` (where following stops it is read once more this way)."""
    return _read(command, _Where(cwd), depth)[0]


def read_script(
    text: str, cwd: Path, depth: int, args: list[str] | None, env: dict | None
) -> list[Risky]:
    """A shell script run as a program: its arguments and the exported variables it inherits."""
    where = _child(_Where(cwd), args or [])
    if env:
        where = replace(where, vars=env.get("vars", {}), funcs=env.get("funcs", {}))
    return _read(text, where, depth)[0]


def _base(path: str) -> str:
    """`Path(path).name`, faster (it runs for every command)."""
    return path.rstrip("/").rsplit("/", 1)[-1] if "/" in path else path


def _unique(found: list[Risky]) -> list[Risky]:
    unique: list[Risky] = []
    for risky in found:
        if risky not in unique:
            unique.append(risky)
    return unique


def _read(
    text: str, where: _Where, depth: int, *, sourced: bool = False, label: str = ""
) -> tuple[list[Risky], _Where]:
    if depth > MAX_DEPTH:  # never a silent OK: look once more without following anything
        return following.too_deep(text, where.cwd), where
    record = _parse_text(text, where.cwd, sourced)
    ctx = _Ctx([], depth, label=label)
    where, _ = _list(record.nodes, where, ctx)
    return ctx.found, where


def _parse_text(text: str, cwd: Path, sourced: bool = False) -> _Text:
    """`text` parsed once per command line: read again (an `eval` in a loop, a script run twice),
    it is the same tree, so what the reader marks on it adds up for the backstop."""
    reading = following._READING.get()
    key = (text, cwd, sourced)
    if reading is not None and key in reading.parsed_texts:
        return reading.parsed_texts[key]
    record = _Text(text, shell.parse(shell.lex(text).tokens), cwd, sourced)
    if reading is not None:
        reading.texts.append(record)
        reading.parsed_texts[key] = record
    return record


# ---- reading commands -----------------------------------------------------------------------


def _list(nodes: list[AndOr], where: _Where, ctx: _Ctx) -> tuple[_Where, _Value]:
    if len(nodes) == 1:
        return _and_or(nodes[0], where, ctx)
    outs = []
    for item in nodes:
        where, out = _and_or(item, where, ctx)
        outs.append(out)
    return where, _joined(outs)


def _joined(values: list[_Value]) -> _Value:
    if len(values) == 1:
        return values[0]
    if all(v is _NOTHING for v in values):
        return _NOTHING
    if all(v.text is not None for v in values):
        return _Value("".join(v.text or "" for v in values), _origins(values))
    built = all(v.text is not None or v.built for v in values)
    return _Value(None, _origins(values), built)


def _origins(values) -> tuple:
    return tuple(o for v in values for o in v.origins)


_ALWAYS_OK = {"cd", "pushd", "true", ":", "export", "local", "declare", "typeset", "readonly",
              "set", "shopt", "alias", "umask", "unset", "echo", "printf"}  # fmt: skip
MAX_ALTS = 4  # folders a command may run in, after `cd`s that may or may not happen


def _always_succeeds(pipeline: Pipeline) -> bool:
    """Does this command always succeed (so what follows `&&` runs for sure)? A `cd` is taken to
    work: one that fails is checked as the folder it names, which has no package to publish."""
    if pipeline.bang or len(pipeline.commands) != 1:
        return False
    command = pipeline.commands[0]
    if not isinstance(command, Simple) or not command.words:
        return isinstance(command, Function)
    lead, rest = _leading_assignments(command.words)
    if any(part.kind == "sub" for word in command.words for part in word.parts):
        return False
    return not rest or rest[0].literal in _ALWAYS_OK


def _and_or(item: AndOr, where: _Where, ctx: _Ctx) -> tuple[_Where, _Value]:
    start = where
    since: _Where | None = None  # the state before the first command that may not run
    ok = True
    outs = []
    pipelines = item.pipelines
    if len(pipelines) == 1 and not item.background:
        return _pipeline(pipelines[0], where, ctx)
    for i, pipeline in enumerate(pipelines):
        if i and since is None and (item.ops[i - 1] == "||" or not ok):
            since = where
        inner = ctx if since is None else _uncertain(ctx)
        where, out = _pipeline(pipeline, where, inner)
        outs.append(out)
        ok = ok and _always_succeeds(pipeline)
    if since is not None:
        where = _merge(since, where)
    if item.background:
        where = start  # `cmd &` runs in a subshell
    return where, _joined(outs)


def _pipeline(pipeline: Pipeline, where: _Where, ctx: _Ctx) -> tuple[_Where, _Value]:
    if len(pipeline.commands) == 1:
        return _command(pipeline.commands[0], where, ctx, None)
    stdin: _Value | None = None
    last = where
    for command in pipeline.commands:  # each part runs in a subshell (zsh: not the last one)
        last, stdin = _command(command, where, ctx, stdin)
    return _merge(where, last), stdin or _UNKNOWN


def _uncertain(ctx: _Ctx) -> _Ctx:
    if not ctx.certain:
        return ctx
    return _Ctx(ctx.found, ctx.depth, False, ctx.label, ctx.calls, ctx.locals)


def _merge(first: _Where, *others: _Where) -> _Where:
    """The state after one of several paths ran: what they agree on stays known."""
    if all(w is first for w in others):
        return first
    wheres = (first, *others)
    values = first.vars
    if not all(w.vars is first.vars for w in others):
        values = _merged_vars(wheres)
    last = wheres[-1]
    cwd, unknown = last.cwd, next((w.unknown for w in wheres if w.unknown), "")
    alts = tuple(dict.fromkeys(p for w in wheres for p in (w.cwd, *w.alts) if p != cwd))
    if len(alts) > MAX_ALTS and not unknown:
        unknown, alts = _BRANCH, ()
    args = last.args if all(w.args == last.args for w in wheres) else None
    ifs = last.ifs if all(w.ifs == last.ifs for w in wheres) else None
    funcs = last.funcs
    if not all(w.funcs is funcs for w in wheres):
        funcs = {name: f for w in wheres for name, f in w.funcs.items()}
    aliases = last.aliases
    if not all(w.aliases is aliases for w in wheres):
        aliases = {name: a for w in wheres for name, a in w.aliases.items()}
    return _Where(cwd, unknown, values, funcs, aliases, args, ifs, last.allexport, alts)


def _merged_vars(wheres: tuple[_Where, ...]) -> dict:
    names = {name for w in wheres for name in w.vars}
    values = {}
    for name in names:
        seen = [w.vars.get(name) for w in wheres]
        if all(v is seen[0] for v in seen):
            if seen[0] is not None:
                values[name] = seen[0]
            continue
        real = [v for v in seen if v is not None]
        evidence = _bounded("\n".join(dict.fromkeys(v.evidence for v in real)))
        origins = tuple(dict.fromkeys(o for v in real for o in v.origins))
        values[name] = _Var(None, origins, evidence, any(v.exported for v in real))
    return values


def _command(command, where: _Where, ctx: _Ctx, stdin: _Value | None) -> tuple[_Where, _Value]:
    if isinstance(command, Simple):
        return _simple(command, where, ctx, stdin)
    if isinstance(command, Function):
        return _define(command, where), _NOTHING
    kind = command.kind
    if kind == "group":
        return _list(command.bodies[0][0], where, ctx)
    if kind == "subshell":
        return where, _list(command.bodies[0][0], where, ctx)[1]
    if kind == "arith":
        return where, _NOTHING
    maybe = _uncertain(ctx)
    if kind in {"for", "select"}:
        if command.head and (name := command.head[0].literal):
            items = command.head[1:]
            evidence = _bounded(" ".join(word.value for word in items))
            var = _Var(None, tuple(items), evidence, _exported(where, name))
            where = replace(where, vars={**where.vars, name: var})
        return _branches(where, [body for body, _ in command.bodies], maybe)
    if kind == "case":
        return _branches(where, [body for body, _ in command.bodies], maybe)
    # if / while / until: the first condition runs for sure; the rest may not
    bodies = command.bodies
    if kind in {"while", "until"}:
        _read_loop_input(command, where, ctx)
    where, out = _list(bodies[0][0], where, ctx)
    after, rest = _branches(where, [body for body, _ in bodies[1:]], maybe)
    return after, _joined([out, rest])


def _read_loop_input(command: Compound, where: _Where, ctx: _Ctx) -> None:
    """`while read -r c; do $c; done <<EOF`: the loop runs each line it reads as a command, so
    the here-document (or here-string) is read as commands."""
    names = {
        w.literal
        for item in command.bodies[0][0]
        for p in item.pipelines
        for c in p.commands
        if isinstance(c, Simple) and c.words and c.words[0].literal == "read"
        for w in c.words[1:]
        if w.literal and not w.literal.startswith("-")
    }
    runs = any(
        isinstance(c, Simple)
        and c.words
        and (set(_names(c.words[0])) & names or c.words[0].literal in {"eval", "sh", "bash"})
        for item in command.bodies[1][0]
        if len(command.bodies) > 1
        for p in item.pipelines
        for c in p.commands
    )
    if not (names and runs):
        return
    for redirect in command.redirects:
        doc = redirect.heredoc
        text = doc.body if doc is not None else None
        if redirect.op == "<<<" and redirect.target is not None:
            text = redirect.target.value
        if text is None:
            continue
        if doc is not None:
            doc.delegated = True
        elif redirect.target is not None:
            redirect.target.delegated = True
        ctx.found += _read(text, where, ctx.depth + 1)[0]


def _branches(where: _Where, bodies: list, ctx: _Ctx) -> tuple[_Where, _Value]:
    results = [_list(body, where, ctx) for body in bodies]
    if not results:
        return where, _NOTHING
    return _merge(where, *(w for w, _ in results)), _joined([o for _, o in results])


def _define(node: Function, where: _Where) -> _Where:
    name = node.name.literal
    if not name:
        return where
    if not isinstance(node.record, _Func):
        node.record = _Func(node, where)
        reading = following._READING.get()
        if reading is not None:
            reading.funcs.append(node.record)
    return replace(where, funcs={**where.funcs, name: node.record})


def _call(func: _Func, args: list[str] | None, where: _Where, ctx: _Ctx) -> _Where:
    """A function called: its body read with its arguments as "$@". Its variables and `cd`s
    last; its positional parameters and `local` variables do not.

    A call is read again unless it is the same function with the same arguments in the very same
    state (variables, functions, folder, files written so far): what it did then is done again.
    Past the call budget, a publish written in its body is "could not check"."""
    func.reached = True
    reading = following._READING.get()
    if reading is None:
        return where
    key = (id(func), None if args is None else tuple(args), id(where), ctx.depth, ctx.label,
           reading.writes, reading.peeking)  # fmt: skip
    hit = reading.calls.get(key)
    if hit is not None and hit[0] is where:  # `where` is kept in the value: its id stays its own
        ctx.found += hit[2]
        return hit[1]
    if len(reading.calls) >= MAX_CALLS or ctx.calls >= MAX_CALL_DEPTH:
        return _unfollowed_call(func, where, ctx)
    func.ran = True
    before = len(ctx.found)
    inner = replace(ctx, calls=ctx.calls + 1, locals=set())
    after, _ = _command(func.node.body, replace(where, args=None if args is None else tuple(args)),
                        inner, None)  # fmt: skip
    values = dict(after.vars)
    for name in inner.locals:
        if name in where.vars:
            values[name] = where.vars[name]
        else:
            values.pop(name, None)
    after = replace(after, args=where.args, vars=values)
    reading.calls[key] = (where, after, ctx.found[before:])
    return after


_UNFOLLOWED_CALL = "it calls a function more often or more deeply than agentprint follows"


def _unfollowed_call(func: _Func, where: _Where, ctx: _Ctx) -> _Where:
    """A call past the budget: a publish or push written in the body is "could not check", and
    what the body may set or `cd` to is only known at run time."""
    scan = _Scan()
    scan.command(func.node.body, True)
    for unit in scan.units:
        if scan.wanted(unit, None, True):
            for action, tool in unit.shapes():
                label = _shape_label(action, tool)
                risky = Risky(action, tool, where.cwd, unsure=_UNFOLLOWED_CALL, label=label)
                if risky not in ctx.found:
                    ctx.found.append(risky)
    values = dict(where.vars)
    moves = False
    for word in shell.command_words(func.node.body):
        match = _assignment_name(word)
        if match:
            name = match.group(1)
            old = values.get(name) or _Var(None)
            evidence = _bounded(old.evidence + "\n" + word.value)
            values[name] = _Var(None, (*old.origins, word), evidence, _exported(where, name))
        moves = moves or word.literal in {"cd", "pushd", "popd"}
    unknown = where.unknown or (_UNFOLLOWED_CALL if moves else "")
    return replace(where, vars=values, unknown=unknown)


def _reach_dynamic(reading) -> list[Risky]:
    """Functions a command named only at run time may call (`"cmd_$1"`, `$TASK`): read them."""
    found: list[Risky] = []
    for _ in range(4):
        todo = [
            f
            for f in reading.funcs
            if not f.reached and any(_matches(f, p) for p in reading.dynamic)
        ]
        if not todo:
            break
        for func in todo:
            ctx = _Ctx([], 0)
            _call(func, None, func.where, ctx)
            found += ctx.found
    return found


def _matches(func: _Func, pattern: tuple[str, str]) -> bool:
    name = func.node.name.literal or ""
    prefix, suffix = pattern
    return name.startswith(prefix) and name.endswith(suffix)


# ---- a simple command -----------------------------------------------------------------------

_ASSIGN_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?(\+?)=")
_DECLARERS = {"export", "local", "readonly", "declare", "typeset"}


def _leading_assignments(words: list[Word]) -> tuple[list[Word], list[Word]]:
    for i, word in enumerate(words):
        if not _assignment_name(word):
            return words[:i], words[i:]
    return words, []


def _assignment_name(word: Word) -> re.Match[str] | None:
    first = word.parts[0] if word.parts else None
    if first is None or first.kind != "lit" or first.quoted or "=" not in first.text:
        return None
    return _ASSIGN_WORD.match(first.text)


def _simple(cmd: Simple, where: _Where, ctx: _Ctx, stdin: _Value | None) -> tuple[_Where, _Value]:
    reading = following._READING.get()
    key = None
    if reading is not None:
        key = _memo_key(cmd, where, ctx, stdin, reading)
        hit = reading.memo.get(key) if key is not None else None
        if hit is not None and hit[0] is where:  # the same command, in the same state
            _, after, out, found, actions = hit
            ctx.found += found
            _account(cmd, found, actions, [])
            return after, out
        reading.commands += 1
        if reading.commands > MAX_COMMANDS:  # past the budget: never silent, see _overflow
            if not reading.overflow and _names_publish(cmd, where):
                reading.overflow = where.cwd
            return where, _UNKNOWN
    after, out = _simple_read(cmd, where, ctx, stdin)
    if key is not None and reading is not None:
        reading.memo[key] = (where, after, out, ctx.found[ctx.mark :], cmd.consumed or set())
    return after, out


def _memo_key(cmd: Simple, where: _Where, ctx: _Ctx, stdin: _Value | None, reading) -> tuple | None:
    """Commands written the same way, run in the same state, do the same: a command repeated
    thousands of times is read once. Not one with a substitution or a here-document (their
    insides carry what the backstop needs), nor inside a function call."""
    if ctx.calls or (stdin is not None and stdin is not _UNKNOWN) or not cmd.words:
        return None
    for word in cmd.words:
        for part in word.parts:
            if part.kind in {"sub", "array"} and _TOOLISH.search(part.text):
                return None
    redirects = []
    for redirect in cmd.redirects:
        if redirect.heredoc is not None or redirect.target is None:
            return None
        redirects.append((redirect.op, redirect.fd, redirect.target.value))
    words = tuple(word.value for word in cmd.words)
    ctx.mark = len(ctx.found)
    state = (id(where), stdin is None, ctx.depth, ctx.label, reading.writes)
    return (words, tuple(redirects), *state)


def _simple_read(
    cmd: Simple, where: _Where, ctx: _Ctx, stdin: _Value | None
) -> tuple[_Where, _Value]:
    before = len(ctx.found)
    seen: set[Action] = set()
    subs = _substitutions(cmd, where, ctx)
    redirected = _stdin(cmd, where, subs, ctx)
    stdin = redirected if redirected is not None else stdin
    lead, words = _leading_assignments(cmd.words)
    prefix = [_assignment(word, where, subs) for word in lead]
    used: list = [o for item in prefix for o in item[2].origins]
    if not words:
        after = _assign(where, prefix)
        _account(cmd, ctx.found[before:], seen, used)
        return after, _NOTHING
    child = _assign(where, prefix, exported=True) if prefix else where
    if words[0].literal in _DECLARERS:
        after, out = _declare(words[0].literal or "", words[1:], child, ctx, subs), _NOTHING
    else:
        args = [arg for word in words for arg in _expand(word, child, subs)]
        used += [o for arg in args for o in arg.origins]
        after, out = _segment(args, child, ctx, stdin, cmd, seen)
    if stdin is not None:
        used += stdin.origins
    if prefix:  # VAR=x cmd: the variable is set for that command only
        names = {name for name, *_ in prefix}
        values = {k: v for k, v in after.vars.items() if k not in names}
        values.update({k: v for k, v in where.vars.items() if k in names})
        after = replace(after, vars=values)
    out = _writes(cmd, child, out, subs)
    _account(cmd, ctx.found[before:], seen, used)
    return after, out


_OVERFLOW = "it runs more commands than agentprint follows, and one past that names `{verb}`"


def _names_publish(cmd: Simple, where: _Where) -> bool:
    """A command past the budget: does it, or a variable it uses, name a publish or push?"""
    for word in cmd.words:
        if EVIDENCE.search(word.value) or any(
            EVIDENCE.search(where.vars[n].evidence) for n in _names(word) if n in where.vars
        ):
            return True
        if any(part.kind == "sub" and EVIDENCE.search(part.text) for part in word.parts):
            return True
    return any(r.heredoc is not None and EVIDENCE.search(r.heredoc.body) for r in cmd.redirects)


def _overflow(reading) -> list[Risky]:
    if not reading.overflow:
        return []
    why = _OVERFLOW.format(verb="publish")
    return [Risky(Action.publish, "sh", reading.overflow, unsure=why, label="command line")]


def _account(cmd: Simple, new: list[Risky], seen: set[Action], used: list) -> None:
    """Mark what this command checked on its words and on everything filled into it, for the
    backstop: a publish written there was accounted for."""
    actions = {r.action for r in new} | seen
    if not actions:
        return
    cmd.consumed = (cmd.consumed or set()) | actions
    for origin in used:
        origin.consumed = (origin.consumed or set()) | actions
    for redirect in cmd.redirects:
        if redirect.heredoc is not None:
            doc = redirect.heredoc
            doc.consumed = (doc.consumed or set()) | actions


def _substitutions(cmd: Simple, where: _Where, ctx: _Ctx) -> dict[int, _Value]:
    """Run the `$(...)` and backticks in a command's words (each in a subshell): what they run
    is checked, and what they print is filled in when it can be told."""
    out: dict[int, _Value] = {}
    for word in cmd.words:
        if len(word.parts) == 1 and word.parts[0].kind == "lit":
            continue
        for part in word.parts:
            if part.kind == "sub":
                out[id(part)] = _substitution(part, where, ctx)
    for redirect in cmd.redirects:
        if redirect.target is not None:
            for part in redirect.target.parts:
                if part.kind == "sub":
                    out[id(part)] = _substitution(part, where, ctx)
    return out


def _substitution(part: Part, where: _Where, ctx: _Ctx) -> _Value:
    if ctx.depth + 1 > MAX_DEPTH:
        ctx.found += following.too_deep(part.text, where.cwd)
        _flag_tokens(part.tokens or [])
        return _UNKNOWN
    inner = _Ctx(ctx.found, ctx.depth + 1, ctx.certain, "", ctx.calls, ctx.locals)
    return _list(shell.nodes_of(part), where, inner)[1]


def _flag_tokens(tokens: list) -> None:
    for token in tokens:
        if isinstance(token, Word):
            token.flagged = True
            for part in token.parts:
                if part.tokens:
                    _flag_tokens(part.tokens)


def _stdin(cmd: Simple, where: _Where, subs: dict, ctx: _Ctx) -> _Value | None:
    """What a here-document, here-string or `< file` gives the command to read."""
    stdin = None
    for redirect in cmd.redirects:
        if redirect.fd not in {"", "0"}:
            continue
        if redirect.heredoc is not None:
            doc = redirect.heredoc
            body = doc.body
            if doc.strip:
                body = "\n".join(line.lstrip("\t") for line in body.split("\n"))
            if not doc.quoted and ("$" in body or "`" in body):
                parts = shell.body_parts(doc)
                for part in parts:  # $(...) in an unquoted body runs, in this shell's subshell
                    if part.kind == "sub":
                        subs[id(part)] = _substitution(part, where, ctx)
                body = _expand_text(parts, where, subs)
            stdin = _Value(body, (doc,))
        elif redirect.op == "<<<" and redirect.target is not None:
            args = _expand(redirect.target, where, subs, split=False)
            text = args[0].text if args else ""
            stdin = _Value(text + "\n", (redirect.target, *(o for a in args for o in a.origins)))
        elif redirect.op == "<" and redirect.target is not None:
            name = redirect.target.literal
            text = following.file_text(name, where.cwd) if name else None
            stdin = _Value(text) if text is not None else _UNKNOWN
    return stdin


def _expand_text(parts: list[Part], where: _Where, subs: dict) -> str:
    out = []
    for part in parts:
        if part.kind == "var":
            got = _param(part.text, where)
            out.append(part.text if got is None or isinstance(got[0], list) else got[0])
        elif part.kind == "sub":
            value = subs.get(id(part))
            text = value.text if value is not None else None
            out.append(text.rstrip("\n") if text is not None else "$(" + part.text + ")")
        else:
            out.append(part.text)
    return "".join(out)


_WRITES = {">", ">>", ">|", "&>", "&>>"}


def _writes(cmd: Simple, where: _Where, out: _Value, subs: dict) -> _Value:
    """`> file`: what the command prints goes into that file (read if the command line runs it
    later), not down the pipe."""
    for redirect in cmd.redirects:
        if redirect.op not in _WRITES or redirect.fd not in {"", "1"} or redirect.target is None:
            continue
        name = redirect.target.literal
        if name and name not in {"/dev/null", "/dev/stderr", "/dev/stdout"}:
            following.write_virtual(name, where.cwd, out, append=redirect.op in {">>", "&>>"})
        out = _NOTHING
    return out


def _assignment(word: Word, where: _Where, subs: dict) -> tuple[str, bool, _Value, Word]:
    """NAME=value, NAME+=value or NAME[i]=value: the name, whether it appends, and the value
    (filled in without splitting, as the shell does)."""
    match = _assignment_name(word)
    assert match is not None
    name, index, append = match.group(1), match.group(2), bool(match.group(3))
    first = word.parts[0]
    if len(word.parts) == 1:  # NAME=literal
        return name, append, _Value(first.text[match.end() :], (word,)), word
    rest = [Part("lit", first.text[match.end() :])] + word.parts[1:]
    if index or any(part.kind == "array" for part in word.parts):  # arrays: run time
        return name, append, _Value(None, (word,)), word
    args = _expand(Word(rest), where, subs, split=False)
    arg = args[0] if args else _Arg("")
    text = arg.text if arg.known else None
    return name, append, _Value(text, (word, *arg.origins)), word


def _assign(
    where: _Where, items: list[tuple[str, bool, _Value, Word]], exported: bool = False
) -> _Where:
    if not items:
        return where
    if not exported and _no_change(where, items):
        return where
    values = dict(where.vars)
    ifs = where.ifs
    for name, append, value, word in items:
        old = values.get(name)
        text = value.text
        if append:
            text = old.value + text if old and old.value is not None and text is not None else None
        if text is not None and len(text) > MAX_VALUE:
            text = None
        evidence = word.value
        if len(word.parts) > 1:
            evidence += "".join(
                "\n" + values[n].evidence for n in _names(word) if n in values and n != name
            )
        if append and old:
            evidence = old.evidence + "\n" + evidence
        if name not in values and len(values) >= MAX_VALUES:
            continue  # not remembered: only known at run time wherever it is used
        origins = (*value.origins, *(old.origins if append and old else ()))
        export = exported or where.allexport or bool(old and old.exported)
        values[name] = _Var(text, origins, _bounded(evidence), export)
        if name == "IFS":
            ifs = text
    w = where
    return _Where(w.cwd, w.unknown, values, w.funcs, w.aliases, w.args, ifs, w.allexport, w.alts)


def _bounded(evidence: str) -> str:
    """Evidence kept short (`X+=...` in a loop): when it is cut, what was cut may name a
    publish, so it counts as naming one."""
    if len(evidence) <= MAX_EVIDENCE:
        return evidence
    return evidence[:MAX_EVIDENCE] + "\n(cut: may publish)"


def _no_change(where: _Where, items: list[tuple[str, bool, _Value, Word]]) -> bool:
    """Setting variables to what they already hold, naming no tool (`a=` x 20000)."""
    for name, append, value, word in items:
        old = where.vars.get(name)
        if append or old is None or old.value != value.text:
            return False
        if old.evidence != word.value or _TOOLISH.search(word.value):
            return False
    return True


_NAME_REF = re.compile(r"\$\{?[#!]?([A-Za-z_][A-Za-z0-9_]*)")


def _names(word: Word) -> list[str]:
    return [
        m.group(1)
        for part in word.parts
        if part.kind == "var"
        for m in _NAME_REF.finditer(part.text)
    ]


def _exported(where: _Where, name: str) -> bool:
    var = where.vars.get(name)
    return bool(var and var.exported) or where.allexport


_PARAM = re.compile(r"\$(?:\{([A-Za-z_]\w*|[0-9]+|[@*#])\}|([A-Za-z_]\w*|[0-9]|[@*#]))")
_DEFAULT = re.compile(r"\$\{([A-Za-z_]\w*|[0-9]+|[@*])(:?)([-+])([^\s$`'\"\\{}]*)\}")


def _param(text: str, where: _Where) -> tuple[str | list[str], tuple] | None:
    """The value of a parameter like `$CMD`, `${1}`, `${1:-release}` or `"$@"` (a list), or None
    when it is only known at run time (or is anything fancier)."""
    default = _DEFAULT.fullmatch(text)
    if default:  # ${X:-word} / ${X:+word} with X known
        name, colon, op, word = default.groups()
        got = _param("${" + name + "}", where)
        if got is None:
            return None
        value = got[0]
        var = where.vars.get(name)
        empty = not value if colon else bool(var and var.unset)
        if isinstance(value, list):
            empty = not value
        if op == "-":
            return (word, ()) if empty else got
        return ("" if empty else word), ()
    match = _PARAM.fullmatch(text)
    if not match:
        return None
    name = match.group(1) or match.group(2)
    if name in {"@", "*", "#"} or name.isdigit():
        if where.args is None:
            return None
        if name in {"@", "*"}:
            return list(where.args), ()
        if name == "#":
            return str(len(where.args)), ()
        n = int(name)
        if n == 0:
            return None
        return (where.args[n - 1] if n <= len(where.args) else ""), ()
    var = where.vars.get(name)
    if var is None or var.value is None:
        return None
    return var.value, var.origins


def _expand(word: Word, where: _Where, subs: dict, split: bool = True) -> list[_Arg]:
    """A word as the command gets it: parameters and substitutions known to hold literal text
    filled in (split into words when unquoted, as the shell splits them). What is only known at
    run time stays as written (`$CMD`), so it reads as a variable."""
    fields: list[list[str]] = [[]]
    origins: list = []
    known = True
    quoted = False
    added = 0

    def spread(text: str) -> None:
        if where.ifs is None:
            nonlocal known
            fields[-1].append(text)
            known = False
            return
        if not where.ifs:
            fields[-1].append(text)
            return
        pieces = re.split("[" + re.escape(where.ifs) + "]+", text)
        for i, piece in enumerate(pieces):
            if i:
                fields.append([])
            if piece:
                fields[-1].append(piece)

    parts = word.parts
    if len(parts) == 1 and parts[0].kind == "lit":  # most words: plain text
        return [_Arg(parts[0].text, word)]
    if (
        len(parts) == 1
        and parts[0].quoted
        and parts[0].kind == "var"
        and parts[0].text in {"$@", "${@}"}
        and where.args is not None
        and _budget(_size(where.args))
    ):
        return [_Arg(arg, word) for arg in where.args]  # "$@": each argument one word
    for part in word.parts:
        quoted = quoted or part.quoted
        if part.kind == "lit":
            fields[-1].append(part.text)
            continue
        value: str | list[str] | None = None
        if part.kind == "var":
            got = _param(part.text, where)
            if got is not None:
                value, from_ = got
                origins.extend(from_)
        elif part.kind == "sub":
            out = subs.get(id(part))
            if out is not None and out.text is not None:
                value = out.text.rstrip("\n")
                origins.extend(out.origins)
        size = len(value) if isinstance(value, str) else _size(value or ())
        if size and value is not None and not _budget(size):
            value = None
        if value is None or added + size > MAX_FILLED:
            text = "$(" + part.text + ")" if part.kind == "sub" else part.text
            if part.kind == "array":
                text = "(" + part.text + ")"
            fields[-1].append(text)
            known = False
            continue
        added += size
        if isinstance(value, list):  # $@ / $*
            if part.quoted and part.text in {"$*", "${*}"}:
                fields[-1].append(" ".join(value))
            elif part.quoted:
                for i, arg in enumerate(value):
                    if i:
                        fields.append([])
                    fields[-1].append(arg)
            else:
                for i, arg in enumerate(value):
                    if i:
                        fields.append([])
                    spread(arg)
        elif part.quoted or not split:
            fields[-1].append(value)
        else:
            spread(value)
    out = []
    for pieces in fields:
        text = "".join(pieces)
        if text or quoted or not split:
            out.append(_Arg(text, word, tuple(origins), known))
    if not split and not out:
        out.append(_Arg("", word, tuple(origins), known))
    return out


# ---- what one command does -------------------------------------------------------------------


def _size(args) -> int:
    reading = following._READING.get()
    if reading is not None and reading.filled > MAX_FILLED_TOTAL:
        return MAX_FILLED_TOTAL  # spent already: no need to add it up
    return sum(map(len, args)) + len(args)


def _budget(size: int) -> bool:
    """Charge `size` filled-in characters to this command line; False once it is spent (the
    reference then stays as written: only known at run time)."""
    reading = following._READING.get()
    if reading is None:
        return True
    reading.filled += size
    return reading.filled <= MAX_FILLED_TOTAL


def _segment(
    args: list[_Arg],
    where: _Where,
    ctx: _Ctx,
    stdin: _Value | None,
    cmd: Simple,
    seen: set[Action],
) -> tuple[_Where, _Value]:
    """One simple command, its words filled in. Returns where the shell is after it, and what it
    prints when that can be told."""
    if not args:
        return where, _NOTHING
    found = ctx.found
    first = args[0]
    alias = where.aliases.get(first.text) if first.word is not None and first.known else None
    if alias is not None and first.word is not None and first.word.reserved == first.text:
        text, alias_origins = alias
        for origin in alias_origins:
            origin.delegated = True
        rest = " ".join(shlex.quote(a.text) if a.known else a.text for a in args[1:])
        inner, after = _read(f"{text} {rest}", where, ctx.depth + 1, label=ctx.label)
        found += inner
        return after, _UNKNOWN
    args, chdir = _unwrap(args)
    if not args:
        return where, _NOTHING
    run = _chdir(where, chdir) if chdir is not None else where
    if (default := _DEFAULTED.fullmatch(args[0].text)) is not None:
        args = [replace(args[0], text=default.group(1), known=True), *args[1:]]
    words = [a.text for a in args]
    first = args[0]
    program, rest = _base(words[0]), words[1:]
    cwd = run.cwd
    before = len(found)
    after, out = where, _UNKNOWN
    if first.known and words[0] in where.funcs and "/" not in words[0]:
        after = _call(where.funcs[words[0]], rest if all(a.known for a in args[1:]) else None,
                      run, ctx)  # fmt: skip
    elif program in {"cd", "pushd"} and chdir is None:
        after, out = _chdir(where, _cd_target(rest)), _NOTHING
    elif program == "popd":
        after = replace(where, unknown=where.unknown or _BRANCH)
    elif program in _STATE:
        after, out = _STATE[program](args[1:], where, ctx, stdin), _NOTHING
    elif program == "printf" and rest[:1] == ["-v"]:
        after, out = _printf_v(args[1:], where, ctx, stdin), _NOTHING
    elif program == "eval":
        after = _eval(args[1:], where, ctx)
    elif words[0] in {"source", "."}:
        after = _source(args[1:], where, ctx, stdin)
    elif program in SHELLS:
        _shell(program, args[1:], run, ctx, stdin)
    elif program == "xargs":
        _xargs(args[1:], run, ctx, stdin, cmd, seen)
    elif program == "git":
        _git(rest, cwd, found, seen)
    elif program in NODE:
        _node(program, rest, cwd, found, ctx.depth, seen)
    elif program in PYTHON_PUBLISH or program == "twine":
        _python(program, rest, cwd, found, seen)
    elif program in WORKSPACE_RELEASERS or program in ROOT_RELEASERS:
        _releaser(program, rest, cwd, found, seen)
    elif program in following.MAKES:
        found.extend(following._make(rest, cwd, ctx.depth, _code_stdin(stdin)))
    elif program == "just":
        found.extend(following._just(rest, cwd, ctx.depth))
    elif program in following.TASKS:
        found.extend(following._task(rest, cwd))
    elif following._interpreter(program):
        _interpret(program, args[1:], run, ctx, stdin)
    elif program in _OUT_OF_SCOPE:
        seen |= _OUT_OF_SCOPE[program](rest)
    elif "/" in words[0]:  # ./release.sh, scripts/release, /abs/path/in/project/x
        found.extend(
            following._script(words[0], cwd, ctx.depth, None, where.unknown, rest, _env(where))
        )
    else:
        out = _prints(program, args[1:], stdin, run)
    if not first.known:
        _dispatch(first, where, ctx)
    if len(found) == before and (named := _named_by_variable(words[0], program, rest, cwd)):
        found.append(named)
    if len(found) == before and not first.known:
        _runtime(first, "", where, ctx)
    if run.alts:  # it may run in another folder too: check there as well
        found[before:] = [
            r2
            for r in found[before:]
            for r2 in (r, *(_rebased(r, run.cwd, alt) for alt in run.alts))
        ]
    if where.unknown or run.unknown:
        why = where.unknown or run.unknown
        found[before:] = [replace(r, unsure=r.unsure or why) for r in found[before:]]
    return after, out


def _rebased(risky: Risky, cwd: Path, alt: Path) -> Risky:
    try:
        return replace(risky, cwd=alt / risky.cwd.relative_to(cwd))
    except ValueError:
        return risky


def _env(where: _Where) -> dict:
    """What a child process inherits: exported variables and functions."""
    values = {k: v for k, v in where.vars.items() if v.exported or where.allexport}
    funcs = {k: f for k, f in where.funcs.items() if f.exported}
    return {"vars": values, "funcs": funcs}


def _child(where: _Where, args: list[str]) -> _Where:
    env = _env(where)
    return _Where(where.cwd, where.unknown, env["vars"], env["funcs"], {}, tuple(args),
                  alts=where.alts)  # fmt: skip


def _runtime(arg: _Arg, what: str, where: _Where, ctx: _Ctx) -> bool:
    """A command, script or code known only at run time (`$CMD`, `bash -c "$X"`, `eval "$X"`):
    "could not check" when it, or what its variables were set from, names a publish."""
    evidence = _evidence(arg, where)
    if not evidence:
        return False
    verb = evidence.lower()
    label = ctx.label or what or (arg.word.value if arg.word is not None else arg.text)
    why = _RUNTIME.format(what=label, verb=verb)
    ctx.found.append(Risky(PUBLISH_VERBS[verb], "sh", where.cwd, unsure=why, label=label))
    for origin in arg.origins:
        origin.flagged = True
    for name in _names(arg.word) if arg.word is not None else []:
        var = where.vars.get(name)
        for origin in var.origins if var else ():
            origin.flagged = True
    return True


def _evidence(arg: _Arg, where: _Where) -> str:
    texts = [arg.text]
    if arg.word is not None:
        for part in arg.word.parts:
            if part.kind == "var":
                texts.append(part.text)
                texts += [where.vars[n].evidence for n in _names(Word([part])) if n in where.vars]
            elif part.kind == "sub":
                texts.append(part.text)
                if following._looks_like_release(part.text) and _CAT.match(part.text):
                    return "publish"
    for text in texts:
        if match := EVIDENCE.search(text):
            return match.group(1)
    return ""


_CAT = re.compile(r"\s*(?:cat|source|\.)\s")


def _dispatch(arg: _Arg, where: _Where, ctx: _Ctx) -> None:
    """A command named only at run time may be any function whose name fits around what is
    written (`"cmd_$1"` -> cmd_*): those are read, with arguments known only at run time."""
    word = arg.word
    prefix = suffix = ""
    if word is not None:
        parts = word.parts
        literal = [p.kind == "lit" for p in parts]
        if False in literal:
            first = literal.index(False)
            last = len(literal) - 1 - literal[::-1].index(False)
            prefix = "".join(p.text for p in parts[:first])
            suffix = "".join(p.text for p in parts[last + 1 :])
    reading = following._READING.get()
    if reading is not None and (prefix, suffix) not in reading.dynamic:
        reading.dynamic.append((prefix, suffix))
    for name, func in list(where.funcs.items()):
        if not func.reached and name.startswith(prefix) and name.endswith(suffix):
            _call(func, None, where, ctx)


# ---- builtins that change the shell's state --------------------------------------------------


def _shift(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    count = args[0].text if args else "1"
    if where.args is None or not count.isdigit() or not all(a.known for a in args):
        return replace(where, args=None)
    return replace(where, args=where.args[int(count) :])


def _set(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    allexport = where.allexport
    for i, arg in enumerate(args):
        text = arg.text
        following_word = args[i + 1].text if i + 1 < len(args) else ""
        if text == "-a" or (text == "-o" and following_word == "allexport"):
            allexport = True
        elif text == "+a":
            allexport = False
        if text == "--" or not text.startswith(("-", "+")):
            rest = args[i + 1 :] if text == "--" else args[i:]
            known = all(a.known for a in rest)
            return replace(
                where, args=tuple(a.text for a in rest) if known else None, allexport=allexport
            )
    return replace(where, allexport=allexport)


_READ_VALUES = {"-a", "-d", "-n", "-N", "-p", "-t", "-u", "-i"}


def _read_builtin(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    names: list[str] = []
    array = raw = False
    i = 0
    while i < len(args):
        text = args[i].text
        if text.startswith("-") and len(text) > 1:
            raw = raw or "r" in text
            if text[-1] in "adnNptui" and text[-1] != "r":
                array = array or text[-1] == "a"
                if text[-1] == "a" and i + 1 < len(args):
                    names.append(args[i + 1].text)
                i += 2
                continue
        else:
            names.append(text)
        i += 1
    names = [n for n in names if re.fullmatch(r"[A-Za-z_]\w*", n)] or ["REPLY"]
    line = None
    if stdin is not None and stdin.text is not None and not array:
        line = stdin.text.split("\n", 1)[0]
        if not raw:
            line = line.replace("\\", "")
    origins = stdin.origins if stdin is not None else ()
    evidence = _bounded(stdin.text) if stdin is not None and stdin.text else ""
    values = dict(where.vars)
    pieces = line.split(None, len(names) - 1) if line is not None else []
    for k, name in enumerate(names):
        value = (pieces[k] if k < len(pieces) else "") if line is not None else None
        if name in values or len(values) < MAX_VALUES:
            values[name] = _Var(value, origins, evidence, _exported(where, name))
    return replace(where, vars=values)


def _printf_v(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    if len(args) < 2 or args[0].text != "-v":
        return where
    name = args[1].text
    value = _printf(args[2:])
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        return where
    origins = tuple(o for a in args[2:] for o in (a.word, *a.origins) if o is not None)
    evidence = " ".join(a.word.value if a.word is not None else a.text for a in args[2:])
    var = _Var(value.text, origins, _bounded(evidence), _exported(where, name))
    if name in where.vars or len(where.vars) < MAX_VALUES:
        return replace(where, vars={**where.vars, name: var})
    return where


def _alias(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    aliases = dict(where.aliases)
    for arg in args:
        name, eq, text = arg.text.partition("=")
        if eq and arg.known and name and not name.startswith("-"):
            aliases[name] = (text, (arg.word, *arg.origins) if arg.word is not None else ())
    return replace(where, aliases=aliases)


def _unset(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    functions = any(a.text == "-f" for a in args)
    names = {a.text for a in args if not a.text.startswith("-")}
    if functions:
        return replace(where, funcs={k: v for k, v in where.funcs.items() if k not in names})
    values = dict(where.vars)
    for name in names:  # known to be gone: neither set here nor passed on to a child
        if re.fullmatch(r"[A-Za-z_]\w*", name) and (name in values or len(values) < MAX_VALUES):
            values[name] = _Var("", (), "", False, unset=True)
    return replace(where, vars=values)


def _trap(args: list[_Arg], where: _Where, ctx: _Ctx, stdin) -> _Where:
    rest = [a for a in args if not a.text.startswith("-")]
    if len(rest) >= 2:  # trap 'code' SIGNAL...: the code runs later, in this shell
        return _eval(rest[:1], where, ctx)
    return where


_STATE = {
    "shift": _shift,
    "set": _set,
    "read": _read_builtin,
    "alias": _alias,
    "unset": _unset,
    "trap": _trap,
}


def _declare(program: str, words: list[Word], where: _Where, ctx: _Ctx, subs: dict) -> _Where:
    """export / local / readonly / declare / typeset: assignments (not split), names, flags."""
    flags = ""
    items = []
    names: list[str] = []
    for word in words:
        text = word.literal
        if text is not None and text[:1] in {"-", "+"} and len(text) > 1:
            flags += text[1:] if text[0] == "-" else ""
        elif _assignment_name(word):
            items.append(_assignment(word, where, subs))
        else:
            names += [a.text for a in _expand(word, where, subs) if a.known]
    exported = program == "export" or "x" in flags
    if "n" in flags or "a" in flags or "A" in flags:  # a name reference or an array: run time
        items = [(n, a, _Value(None, v.origins), w) for n, a, v, w in items]
    if "f" in flags and exported:
        for name in names:
            if name in where.funcs:
                where.funcs[name].exported = True
        return replace(where, funcs=dict(where.funcs))  # a new state: what children get changed
    where = _assign(where, items, exported=exported)
    values = dict(where.vars)
    for name in names:
        old = values.get(name)
        if exported and old is not None:
            values[name] = replace(old, exported=True)
        elif exported:
            values[name] = _Var(None, (), "", True)
        elif program == "local":
            values[name] = _Var("", (), "", False)
    if program == "local" or (program in {"declare", "typeset"} and "g" not in flags and ctx.calls):
        ctx.locals |= {name for name, *_ in items} | set(names)
    return replace(where, vars=values)


def _eval(args: list[_Arg], where: _Where, ctx: _Ctx) -> _Where:
    """`eval`: its words joined and read again, in this shell, for what they run."""
    for arg in args:
        if arg.word is not None:
            arg.word.delegated = True
        for origin in arg.origins:
            origin.delegated = True
    text = " ".join(a.text for a in args)
    inner, after = _read(text, where, ctx.depth + 1, label="eval")
    ctx.found += inner
    return after


def _source(args: list[_Arg], where: _Where, ctx: _Ctx, stdin: _Value | None) -> _Where:
    """`source file` / `. file`: the file read in this shell; its functions and variables last."""
    if not args:
        return where
    name = args[0].text
    saved = where.args
    if len(args) > 1:
        where = replace(where, args=tuple(a.text for a in args[1:]))
    if name in {"/dev/stdin", "-", "/dev/fd/0"}:
        if stdin is None or stdin.text is None:
            return replace(where, args=saved)
        for origin in stdin.origins:
            origin.delegated = True
        inner, after = _read(stdin.text, where, ctx.depth + 1, sourced=True)
        ctx.found += inner
        return replace(after, args=saved)
    got = following.sourced(name, where.cwd, ctx.depth, where.unknown)
    if isinstance(got, list):
        ctx.found += got
        return replace(where, args=saved)
    inner, after = _read(got, where, ctx.depth + 1, sourced=True)
    ctx.found += inner
    return replace(after, args=saved)


def _shell(program: str, args: list[_Arg], where: _Where, ctx: _Ctx, stdin: _Value | None) -> None:
    words = [a.text for a in args]
    index = _shell_script(words)
    if index is not None:  # bash -c 'script' [name args...]
        if index >= len(args):
            return
        script = args[index]
        if not script.known:
            _runtime(script, f"{program} -c", where, ctx)
        if script.word is not None:
            script.word.delegated = True
        for origin in script.origins:
            origin.delegated = True
        child = _child(where, words[index + 2 :])
        ctx.found += _read(script.text, child, ctx.depth + 1)[0]
        return
    file = following._shell_file_arg(words)
    if file is not None and file not in {"/dev/stdin", "-", "/dev/fd/0"}:
        rest = words[words.index(file) + 1 :]  # what the script gets as "$@"
        ctx.found += following._script(file, where.cwd, ctx.depth, program, where.unknown, rest,
                                       _env(where))  # fmt: skip
        return
    if stdin is None:
        return
    if stdin.text is not None:  # commands from stdin: a here-document, a pipe, `< file`
        for origin in stdin.origins:
            origin.delegated = True
        ctx.found += _read(stdin.text, _child(where, []), ctx.depth + 1)[0]
    elif stdin.built:
        why = _BUILT_TEXT
        ctx.found.append(Risky(Action.publish, "sh", where.cwd, unsure=why, label=program))


def _interpret(
    program: str, args: list[_Arg], where: _Where, ctx: _Ctx, stdin: _Value | None
) -> None:
    words = [a.text for a in args]
    kind, index = following.interpreter_input(program, words)
    if kind == "code" and index is not None and index < len(args):
        code = args[index]
        if not code.known:
            _runtime(code, f"{program} {words[index - 1] if index else ''}".strip(), where, ctx)
        if code.word is not None:
            code.word.delegated = True
        for origin in code.origins:
            origin.delegated = True
    text = None
    if kind == "stdin" and stdin is not None and stdin.text is not None:
        text = stdin.text
        for origin in stdin.origins:
            origin.delegated = True
    ctx.found += following._interpreted(program, words, where.cwd, ctx.depth, text)


def _code_stdin(stdin: _Value | None) -> str | None:
    if stdin is None or stdin.text is None:
        return None
    for origin in stdin.origins:
        origin.delegated = True
    return stdin.text


_XARGS_VALUES = {"-n", "-L", "-P", "-s", "-d", "-E", "-a", "--max-args", "--max-lines",
                 "--max-procs", "--max-chars", "--delimiter", "--eof", "--arg-file",
                 "--process-slot-var"}  # fmt: skip
_PUBLISHERS = NODE | PYTHON_PUBLISH | WORKSPACE_RELEASERS | {"twine", "git", "gh", "cargo",
                                                             "docker", "podman"}  # fmt: skip


def _xargs(
    args: list[_Arg],
    where: _Where,
    ctx: _Ctx,
    stdin: _Value | None,
    cmd: Simple,
    seen: set[Action],
) -> None:
    """`xargs [options] command`: the command with words from its input. When the input is
    known (`echo publish | xargs npm`) they are filled in; when it is not, a publishing tool
    whose subcommand would come from it is "could not check"."""
    marker = ""
    i = 0
    while i < len(args) and args[i].text.startswith("-") and args[i].text != "--":
        text = args[i].text
        if text in {"-I", "--replace"} and i + 1 < len(args):
            marker = args[i + 1].text
            i += 2
            continue
        if text.startswith("-I") or text.startswith("--replace="):
            marker = text[2:] if text.startswith("-I") else text.partition("=")[2]
        elif text == "-i" or text.startswith("-i"):
            marker = text[2:] or "{}"
        elif text in _XARGS_VALUES:
            i += 1
        i += 1
    if i < len(args) and args[i].text == "--":
        i += 1
    command = args[i:] or [_Arg("echo")]
    if stdin is not None and stdin.text is not None:
        if marker:
            for line in [x for x in stdin.text.split("\n") if x.strip()][:MAX_XARGS]:
                filled = [replace(a, text=a.text.replace(marker, line)) for a in command]
                _segment(filled, where, ctx, None, cmd, seen)
        else:
            items = [_Arg(w, None, stdin.origins) for w in stdin.text.split()]
            _segment(command + items, where, ctx, None, cmd, seen)
        return
    before = len(ctx.found)
    _segment(command, where, ctx, None, cmd, seen)
    tool = _base(command[0].text)
    verbs = [a.text for a in command[1:] if not a.text.startswith("-")]
    if len(ctx.found) == before and tool in _PUBLISHERS and (not verbs or marker in verbs[0]):
        why = _FROM_INPUT.format(tool=tool)
        action = Action.push if tool in {"git", "docker", "podman"} else Action.publish
        ctx.found.append(Risky(action, tool, where.cwd, unsure=why, label=f"xargs {tool}"))


def _docker(rest: list[str]) -> set[Action]:
    plain = [w for w in rest if not w.startswith("-")]
    return {Action.push} if plain[:1] == ["push"] or plain[:2] == ["image", "push"] else set()


def _cargo(rest: list[str]) -> set[Action]:
    plain = [w for w in rest if not w.startswith("-")]
    return {Action.publish} if plain[:1] == ["publish"] else set()


def _gh(rest: list[str]) -> set[Action]:
    plain = [w for w in rest if not w.startswith("-")]
    if plain[:1] == ["release"] and plain[1:2] in (["create"], ["upload"]):
        return {Action.publish}
    return set()


# Publishes agentprint reads but does not check (container images, crates, GitHub releases):
# written plainly they are accounted for; hidden, the backstop says "could not check".
_OUT_OF_SCOPE = {"docker": _docker, "podman": _docker, "cargo": _cargo, "gh": _gh}


# ---- what a command prints -------------------------------------------------------------------

_FILTERS = {"tr", "sed", "awk", "gawk", "cut", "head", "tail", "sort", "uniq", "xxd", "od",
            "openssl", "gzip", "gunzip", "zcat", "fold", "paste", "column", "fmt", "nl", "tac",
            "expand", "unexpand", "iconv", "envsubst", "perl", "base32"}  # fmt: skip
_PRINTERS = {"echo", "printf", "cat", "tee"}
_TRANSFORMS = _FILTERS | {"rev", "base64"}


def _prints(program: str, args: list[_Arg], stdin: _Value | None, where: _Where) -> _Value:
    if program not in _PRINTERS and (stdin is None or program not in _TRANSFORMS):
        return _UNKNOWN
    words = [a.text for a in args]
    sources = tuple(o for a in args for o in (a.word, *a.origins) if o is not None)
    if program == "echo":
        if not all(a.known for a in args):
            return _UNKNOWN
        newline, escapes = True, False
        while words and re.fullmatch(r"-[neE]+", words[0]):
            newline = newline and "n" not in words[0]
            escapes = "e" in words[0] or (escapes and "E" not in words[0])
            words = words[1:]
        text = " ".join(words)
        if escapes:
            text = shell.ansi_c(text.replace("'", "\\'"))
        return _Value(text + ("\n" if newline else ""), sources)
    if program == "printf":
        return replace(_printf(args), origins=sources) if words[:1] != ["-v"] else _NOTHING
    if program in {"cat", "tee"}:
        files = [w for w in words if w != "-" and not w.startswith("-")]
        if program == "tee":
            for name in files:
                following.write_virtual(name, where.cwd, stdin or _UNKNOWN, append="-a" in words)
            return stdin or _UNKNOWN
        if not files:
            return stdin or _UNKNOWN
        texts = [following.file_text(name, where.cwd) for name in files]
        if any(t is None for t in texts) or not all(a.known for a in args):
            return _UNKNOWN
        return _Value("".join(t or "" for t in texts))
    if stdin is None:
        return _UNKNOWN
    literal = stdin.text is not None or stdin.built
    if program == "rev" and stdin.text is not None:
        return _Value("\n".join(line[::-1] for line in stdin.text.split("\n")), stdin.origins)
    if program == "base64" and {"-d", "--decode", "-D"} & set(words) and stdin.text is not None:
        try:
            raw = base64.b64decode("".join(stdin.text.split()), validate=True)
            return _Value(raw.decode("utf-8"), stdin.origins)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return _Value(None, stdin.origins, True)
    if program in _FILTERS or program in {"rev", "base64"}:
        return _Value(None, stdin.origins, literal)
    return _UNKNOWN


_FORMAT = re.compile(r"%(?:%|[-+ #0]*[0-9*]*(?:\.[0-9*]*)?[sbdiouxXcqfeEgG])")


def _printf(args: list[_Arg]) -> _Value:
    """What `printf FORMAT ARGS...` prints, when every word is known: %s/%b/%d..., the format
    used again while arguments are left, escapes decoded."""
    if not args or not all(a.known for a in args):
        return _UNKNOWN
    fmt, rest = args[0].text, [a.text for a in args[1:]]
    out: list[str] = []
    for _ in range(64):
        values = iter(rest)
        used = [0]

        def fill(match: re.Match[str], values=values, used=used) -> str:
            spec = match.group()
            if spec == "%%":
                return "%"
            used[0] += 1
            value = next(values, "")
            return shell.ansi_c(value.replace("'", "\\'")) if spec.endswith("b") else value

        out.append(shell.ansi_c(_FORMAT.sub(fill, fmt).replace("'", "\\'")))
        rest = rest[used[0] :]
        if not rest or not used[0]:
            break
    return _Value("".join(out))


# ---- the backstop -----------------------------------------------------------------------------


def _backstops(reading) -> list[Risky]:
    found: list[Risky] = []
    for record in reading.texts:
        found += _backstop(record)
    return found


def _backstop(record: _Text) -> list[Risky]:
    """A publish or push written in this text that the reader did not account for."""
    scan = _Scan()
    scan.nodes(record.nodes, True)
    live = scan.live_names()
    found: list[Risky] = []
    for unit in scan.units:
        if not scan.wanted(unit, live, record.sourced):
            continue
        for action, tool in unit.unaccounted():
            label = _shape_label(action, tool)
            risky = Risky(action, tool, record.cwd, unsure=_UNFOLLOWED, label=label)
            if risky not in found:
                found.append(risky)
    return found


def scan_hits(text: str) -> list[tuple[Action, str]]:
    """The publishes and pushes written in shell text that is only scanned, not followed (dry
    runs, comments and other inert text left out)."""
    nodes = shell.parse(shell.lex(text).tokens)
    scan = _Scan()
    scan.nodes(nodes, True)
    live = scan.live_names()
    hits: list[tuple[Action, str]] = []
    for unit in scan.units:
        if scan.wanted(unit, live, True):
            for hit in unit.shapes():
                if hit not in hits:
                    hits.append(hit)
    return hits


def _shape_label(action: Action, tool: str) -> str:
    if tool in {"docker", "podman"}:
        return f"{tool} push"
    if tool == "gh":
        return "gh release"
    return following._what(action, tool)


# Any tool a publish or push shape starts with (a cheap test before a command is looked at).
_TOOLISH = re.compile(
    r"(?=[npybuhfcltgdsr])(?:npm|pnpm|yarn|bun|uv|poetry|hatch|flit|changeset|lerna|cargo|twine"
    r"|git|docker|podman|gh|np|semantic-release|release-it)",
    re.I,
)
_SCAN_SPECIAL = {"export", "local", "readonly", "declare", "typeset", "eval", "source", ".", "set",
                 "compgen"}  # fmt: skip
# Programs whose arguments are never run: the words given to them are only text.
# (Not `ack --pager`, `sort --compress-program`, `less`: those can start a program.)
_QUIET = {":", "true", "false", "test", "[", "[[", "grep", "egrep", "fgrep", "zgrep", "head",
          "tail", "wc", "ls", "touch", "mkdir", "rm", "rmdir", "cp", "mv", "chmod", "chown",
          "ln", "basename", "dirname", "sleep", "jq", "uniq", "cut", "column", "diff", "cmp",
          "stat", "file", "du", "df", "which", "type", "pwd", "date", "tr", "rev", "wait",
          "exit", "return", "printenv", "cat"}  # fmt: skip
# Programs that pass what they read on (or swallow it): a here-document fed to them is data.
_SINKS = _QUIET | {"tee", "base64", "pbcopy", "wl-copy", "xclip", "gzip", "fold", "fmt", "nl",
                   "sort",
                   "column", "md5sum", "sha256sum", "shasum", "envsubst", "tac",
                   "paste"}  # fmt: skip
_WRAPS = {"sudo", "doas", "env", "command", "builtin", "nohup", "time", "exec", "nice", "stdbuf",
          "chronic", "unbuffer", "caffeinate", "ionice", "timeout", "gtimeout"}  # fmt: skip
_MESSAGE = {"-m", "--message", "-F", "--file", "--body", "-b", "--title", "-t", "--notes", "-n",
            "--body-file", "--notes-file"}  # fmt: skip


@dataclass(eq=False)
class _Unit:
    """Words (or a here-document body) to scan, and what accounts for a publish found there."""

    words: list[Word]
    simple: Simple | None = None
    doc: Heredoc | None = None
    needs: str = ""  # a variable that must be live for this text to matter ("" = always)
    file: str = ""  # a file the text is written to: matters if the command line names it later
    at: int = 0
    assigns: frozenset[int] = frozenset()  # words that are VAR=value assignments

    def shapes(self) -> list[tuple[Action, str]]:
        return [(a, t) for a, t, _, _ in _shapes(self._tokens())]

    def _tokens(self) -> list[_Tok]:
        if self.doc is not None and not self.words:
            return _raw_tokens(self.doc.body, self.doc)
        return _view(self.words, self.assigns)

    def unaccounted(self) -> list[tuple[Action, str]]:
        out = []
        tokens = self._tokens()
        for action, tool, i, j in _shapes(tokens):
            if self.simple is not None and action in (self.simple.consumed or ()):
                continue
            owners = {id(t.owner): t.owner for t in tokens[i : j + 1]}.values()
            if any(
                action in (getattr(o, "consumed", None) or ())
                or getattr(o, "delegated", False)
                or getattr(o, "flagged", False)
                for o in owners
            ):
                continue
            out.append((action, tool))
        return out


class _Scan:
    """Sort a parsed text into what can run (scanned) and what provably can't (left out)."""

    def __init__(self) -> None:
        self.units: list[_Unit] = []
        self.refs: set[str] = set()
        self.exported: set[str] = set()
        self.indirect = False
        self.seen: list[Word] = []  # every word, in order: is a written file named later?

    def live_names(self) -> set[str] | None:
        """Variables something may run, or None when that can't be told (all of them)."""
        if self.indirect:
            return None
        return self.refs | self.exported

    def wanted(self, unit: _Unit, live: set[str] | None, all_live: bool) -> bool:
        if unit.needs and not all_live and live is not None and unit.needs not in live:
            return False
        if unit.file:
            base = unit.file.rsplit("/", 1)[-1]
            return any(base in word.value for word in self.seen[unit.at :])
        return True

    # -- walking --

    def nodes(self, nodes: list[AndOr], quiet: bool | str) -> None:
        for item in nodes:
            for pipeline in item.pipelines:
                self.pipeline(pipeline, quiet)

    def pipeline(self, pipeline: Pipeline, quiet: bool | str) -> None:
        commands = pipeline.commands
        dests: list[bool | str] = [quiet] * len(commands)
        for i in range(len(commands) - 2, -1, -1):  # what each part prints goes to the next
            nxt = commands[i + 1]
            program = _static_program(nxt.words)[1] if isinstance(nxt, Simple) else None
            dests[i] = dests[i + 1] if program in _SINKS else False
        for command, dest in zip(commands, dests, strict=True):
            self.command(command, dest)

    def command(self, command, dest: bool | str) -> None:
        if isinstance(command, Simple):
            self.simple(command, dest)
        elif isinstance(command, Function):
            record = command.record
            body = command.body
            closed = not isinstance(body, Compound) or body.closed
            if isinstance(record, _Func) and not record.reached and closed:
                return  # a function nobody calls: its body never runs
            self.command(command.body, dest)
        elif isinstance(command, Compound):
            if command.kind in {"for", "select"} and command.head:
                name = command.head[0].literal or ""
                self._words(command.head[1:], dest, needs=name)
            elif command.kind == "case" and command.head:
                self._subs(command.head[0], False)
            for body, _ in command.bodies:
                self.nodes(body, dest)
            for redirect in command.redirects:
                self.redirect(redirect, None, None, dest)

    def _words(self, words: list[Word], dest, needs: str = "", simple=None) -> None:
        if words:
            self.units.append(
                _Unit(list(words), simple, needs=needs, assigns=frozenset(map(id, words)))
            )
            for word in words:
                self._refs(word)
                self._subs(word, False)

    def _refs(self, word: Word) -> None:
        for part in word.parts:
            if part.kind == "var":
                if part.text.startswith("${!"):
                    self.indirect = True
                self.refs.update(_names(Word([part])))
            elif part.kind == "array" and part.tokens:
                for token in part.tokens:
                    if isinstance(token, Word):
                        self._refs(token)

    def _subs(self, word: Word, quiet: bool | str) -> None:
        for part in word.parts:
            if part.kind in {"sub", "array"} and part.tokens is not None:
                self.nodes(shell.nodes_of(part), quiet if part.kind == "sub" else False)

    def simple(self, cmd: Simple, dest: bool | str) -> None:
        words = cmd.words
        self.seen.extend(words)
        if not cmd.redirects and words and words[0].value not in _SCAN_SPECIAL:
            for word in words:  # plain text naming no tool: nothing to find, nothing it sets
                if len(word.parts) != 1 or word.parts[0].kind != "lit":
                    break
            else:
                if not _TOOLISH.search(" ".join(word.value for word in words)):
                    return
        lead, rest = _leading_assignments(cmd.words)
        index, program = _static_program(rest)
        args = rest[index + 1 :] if program is not None else []
        quiet_words: set[int] = set()
        sets = bool(args) and args[0].literal == "-v"  # printf -v NAME: a variable, not output
        if program in {"echo", "printf"} and dest is True and not sets:
            quiet_words = {id(w) for w in args}
        elif program in {"echo", "printf"} and isinstance(dest, str) and dest and not sets:
            for word in args:
                self.units.append(_Unit([word], cmd, needs=dest))
                self._subs(word, dest)
            quiet_words = {id(w) for w in args}
        elif program in _QUIET or (
            program == "rg" and not any((w.literal or "").startswith("--pre") for w in args)
        ):
            quiet_words = {id(w) for w in args}
        elif program == "git":
            quiet_words = _git_messages(args)
        elif program == "gh":
            quiet_words = _option_values(args, _MESSAGE)
        if program in {"eval", "source", ".", "compgen"} or (
            program in _DECLARERS
            and any(
                (w.literal or "").startswith("-") and "n" in (w.literal or "") for w in args
            )  # fmt: skip
        ):
            self.indirect = True
        if program in {"export"} or (
            program in _DECLARERS
            and any(
                (w.literal or "").startswith("-") and "x" in (w.literal or "") for w in args
            )  # fmt: skip
        ):
            for word in args:
                match = _assignment_name(word)
                self.exported.add(match.group(1) if match else (word.literal or ""))
        if program == "set" and any(w.literal in {"-a", "allexport"} for w in args):
            self.indirect = True
        live: list[Word] = []
        for word in lead:
            name = _assignment_name(word)
            if rest:  # VAR=x cmd: the command (and what it runs) sees it
                (live if program not in _QUIET else []).append(word)
                if name:
                    self.exported.add(name.group(1))
                self._subs(word, False)
            elif name:
                self.units.append(_Unit([word], cmd, needs=name.group(1)))
                self._subs(word, name.group(1))
        for word in rest:
            if id(word) in quiet_words:
                self._subs(word, True)
                continue
            live.append(word)
            self._refs(word)
            self._subs(word, False)
        if program in _DECLARERS:  # export X=y: assignments are values, not commands
            assigned = [w for w in live if _assignment_name(w)]
            live = [w for w in live if not _assignment_name(w)]
            for word in assigned:
                name = _assignment_name(word)
                self.units.append(_Unit([word], cmd, needs=name.group(1) if name else ""))
        if live:
            declares = program in _DECLARERS
            assigns = frozenset(
                id(w) for w in live if w in lead or (declares and _assignment_name(w))
            )
            self.units.append(_Unit(live, cmd, assigns=assigns))
        for redirect in cmd.redirects:
            self.redirect(redirect, cmd, program, dest)

    def redirect(self, redirect, cmd: Simple | None, program: str | None, dest) -> None:
        target = redirect.target
        if redirect.op in _WRITES and target is not None:
            self._subs(target, False)
            return
        if redirect.heredoc is None and redirect.op != "<<<":
            if target is not None:
                self._subs(target, False)
            return
        file = ""
        if cmd is not None:
            for other in cmd.redirects:
                if other.op in _WRITES and other.target is not None and other.target.literal:
                    file = other.target.literal
        if program == "tee" and cmd is not None:
            names = [
                w.literal for w in _static_args(cmd.words) if w.literal and w.literal[0] != "-"
            ]
            file = names[0] if names else file
        quiet = (
            program in _SINKS
            and (dest is True or file)
            or (program == "git" and cmd is not None and _reads_message(cmd.words))
            or (program == "gh" and cmd is not None and _reads_message(cmd.words))
        )
        needs = dest if isinstance(dest, str) and program in _SINKS else ""
        if program == "read" and cmd is not None:
            names = [
                w.literal for w in _static_args(cmd.words) if w.literal and w.literal[0] != "-"
            ]
            needs, quiet = (names[-1] if names else "REPLY"), False
        if redirect.heredoc is not None:
            doc = redirect.heredoc
            if quiet and not file:
                return
            if not doc.quoted:
                for part in shell.body_parts(doc):
                    if part.kind == "sub" and part.tokens is not None:
                        self.nodes(shell.nodes_of(part), False)
            unit = _Unit([], cmd, doc=doc, needs=needs, file=file if quiet else "")
            unit.at = len(self.seen)
            self.units.append(unit)
        elif target is not None:
            self._subs(target, False)
            if quiet and not file:
                return
            unit = _Unit([target], cmd, needs=needs, file=file if quiet else "")
            unit.at = len(self.seen)
            self.units.append(unit)


def _static_args(words: list[Word]) -> list[Word]:
    lead, rest = _leading_assignments(words)
    index, program = _static_program(rest)
    return rest[index + 1 :] if program is not None else []


def _static_program(words: list[Word]) -> tuple[int, str | None]:
    """The program a command runs, read from its literal words (wrappers like sudo, env and
    time skipped): its index and name, or None when a variable or substitution names it."""
    i = 0
    while i < len(words):
        text = words[i].literal
        if text is None:
            return i, None
        name = _base(text)
        if _ASSIGN.match(text) and i:
            i += 1
            continue
        if name in _WRAPS:
            i += 1
            values = SUDO_VALUES | ENV_VALUES | PREFIXES.get(name, set())
            while i < len(words) and (words[i].literal or "").startswith("-"):
                flag = words[i].literal or ""
                i += 2 if flag in values else 1
            if name in {"timeout", "gtimeout"}:
                i += 1
            continue
        return i, name
    return i, None


def _option_values(words: list[Word], options: set[str]) -> set[int]:
    out: set[int] = set()
    for k, word in enumerate(words):
        text = word.literal or ""
        name, eq, _ = text.partition("=")
        if name in options and eq:
            out.add(id(word))
        elif text in options and k + 1 < len(words):
            out.add(id(words[k + 1]))
        elif text[:2] in {"-m", "-F"} and len(text) > 2 and text[1] in "mF":
            out.add(id(word))
    return out


def _git_messages(args: list[Word]) -> set[int]:
    """The message of `git commit -m` / `git tag -m` / `git notes add -m` / `git stash push -m`,
    and the patterns of `git grep` / `git log --grep`: text, never run."""
    i = 0
    while i < len(args) and (args[i].literal or "").startswith("-"):
        i += 2 if args[i].literal in VALUE_OPTS["git"] | {"-C"} else 1
    sub = args[i].literal if i < len(args) else None
    if sub in {"grep", "log", "show", "diff", "blame", "shortlog"}:
        return {id(w) for w in args[i + 1 :]}
    if sub in {"commit", "tag", "notes", "stash", "merge", "revert", "cherry-pick"}:
        return _option_values(args[i + 1 :], {"-m", "--message", "-F", "--file"})
    return set()


def _reads_message(words: list[Word]) -> bool:
    """`git commit -F -`, `gh pr create --body-file -`: the here-document is the message."""
    texts = [w.literal for w in words]
    for k, text in enumerate(texts[:-1]):
        if text in {"-F", "--file", "--body-file", "--notes-file"} and texts[k + 1] == "-":
            return True
    return any(t in {"--file=-", "--body-file=-", "-F-"} for t in texts)


# ---- shapes -------------------------------------------------------------------------------------

_VIEW_SPLIT = re.compile(r"[ \t\n\r\f\v]+|([;&|()<>`])")
_RENDER_DEFAULT = re.compile(r"\$\{[#!]?\w+(?:\[[^\]]*\])?:?[-=+?](.*)\}\Z", re.S)
_VERB_OF = {"npm": "publish", "pnpm": "publish", "yarn": "publish", "bun": "publish",
            "uv": "publish", "poetry": "publish", "hatch": "publish", "flit": "publish",
            "changeset": "publish", "lerna": "publish", "cargo": "publish", "twine": "upload",
            "git": "push", "docker": "push", "podman": "push", "gh": "release"}  # fmt: skip
_RELEASERS = {"semantic-release", "release-it"}
_LAUNCH_WORDS = {"npx", "pnpx", "bunx", "dlx", "exec", "--", "x"}
_NEGATIONS = {"false", "0", "no", "off"}


@dataclass(slots=True)
class _Tok:
    text: str
    owner: object  # the word or here-document it came from
    literal: bool = True
    sep: bool = False


def _render(word: Word) -> tuple[str, bool]:
    out: list[str] = []
    literal = True
    for part in word.parts:
        if part.kind == "lit":
            out.append(part.text)
            continue
        literal = False
        if part.kind == "var":
            default = _RENDER_DEFAULT.match(part.text)
            out.append(default.group(1).strip("'\"") if default else f" -{part.text} ")
        elif part.kind == "array":
            out.append(" " + part.text + " ")
        else:
            out.append(" ; ")  # a substitution is scanned on its own
    return "".join(out), literal


def _view(words: list[Word], assigns: frozenset[int] = frozenset()) -> list[_Tok]:
    tokens: list[_Tok] = []
    for word in words:
        text, literal = _render(word)
        _split_into(text, word, literal, tokens)
        if id(word) in assigns:  # a value: a command inside it, not one running on past it
            tokens.append(_Tok(";", word, literal, True))
    return tokens


def _raw_tokens(text: str, owner) -> list[_Tok]:
    tokens: list[_Tok] = []
    _split_into(text, owner, True, tokens, strip_quotes=True)
    return tokens


_NEEDS_SPLIT = re.compile(r"[\s;&|()<>`='\"]")


def _split_into(text: str, owner, literal: bool, tokens: list, strip_quotes: bool = False) -> None:
    if text and not _NEEDS_SPLIT.search(text):
        tokens.append(_Tok(text, owner, literal))
        return
    for piece in _VIEW_SPLIT.split(text):
        if not piece:
            continue
        if piece in ";&|()<>`":
            tokens.append(_Tok(piece, owner, literal, True))
            continue
        if strip_quotes:
            piece = piece.strip("'\"")
            if not piece:
                continue
        assign = _ASSIGN.match(piece)
        if assign and len(piece) > assign.end():
            piece = piece[assign.end() :].strip("'\"")
        tokens.append(_Tok(piece, owner, literal))
        if piece.startswith("-") and "=" in piece:  # --pager=npm publish: the value is a word too
            value = piece.partition("=")[2].strip("'\"")
            if value:
                tokens.append(_Tok(value, owner, literal))


_ALL_TOOLS = set(_VERB_OF) | _RELEASERS | {"np"}


def _tool_name(text: str) -> str:
    low = text.lower()
    if low in _ALL_TOOLS or ("/" not in low and "." not in low):
        return low
    name = low.rsplit("/", 1)[-1]
    for suffix in (".cmd", ".exe", ".ps1", ".bat"):
        name = name.removesuffix(suffix)
    return name


def _shapes(tokens: list[_Tok]) -> list[tuple[Action, str, int, int]]:
    """Publish and push commands written in a run of words: a tool, its options, its verb
    (`npm --tag x publish`, `git -C dir push`, `gh release create`), dry runs left out."""
    out = []
    count = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.sep:
            continue
        name = _tool_name(tok.text)
        if name in _RELEASERS or (
            name == "np"
            and (i == 0 or tokens[i - 1].sep or _tool_name(tokens[i - 1].text) in _LAUNCH_WORDS)
        ):
            if not _dry(tokens, i + 1, name):
                out.append((Action.publish, name, i, i))
            continue
        verb = _VERB_OF.get(name)
        if verb is None:
            continue
        j, value_ok, steps = i + 1, False, 0
        while j < count and steps < 24 and not tokens[j].sep:
            word = tokens[j].text
            low = word.lower()
            if (low == verb or (name == "npm" and low in NPM_PUBLISH_WORDS)) and name != "gh":
                break
            if name == "gh" and low == "release":
                k = j + 1
                while k < count and not tokens[k].sep and tokens[k].text.startswith("-"):
                    k += 1
                if k < count and tokens[k].text.lower() in {"create", "upload"}:
                    j = k
                    break
                j = count
                break
            if word.startswith("-"):
                value_ok = True
            elif value_ok:
                value_ok = False
            elif (name == "yarn" and low == "npm") or (
                name in {"docker", "podman"} and low == "image"
            ):
                pass
            else:
                j = count
                break
            j += 1
            steps += 1
        else:
            continue
        if j >= count or tokens[j].sep:
            continue
        if _dry(tokens, i + 1, name):
            continue
        action = Action.push if name in {"git", "docker", "podman"} else Action.publish
        out.append((action, name, i, j))
    return out


def _dry(tokens: list[_Tok], start: int, tool: str) -> bool:
    """A literal, true dry-run flag in the rest of this command (`--dry-run`, `git push -n`);
    `--dry-run=false`, `--no-dry-run` and `--dry-run false` publish for real."""
    dry = False
    flags = set(DRY_RUN)
    if tool == "git":
        flags.add("-n")
    if tool in ROOT_RELEASERS | _RELEASERS:
        flags.add("-d")
    k = start
    while k < len(tokens) and not tokens[k].sep:
        tok = tokens[k]
        name, eq, value = tok.text.partition("=")
        if name in flags and tok.literal:
            if eq:
                dry = value.lower() == "true"
            elif (
                tool in NODE
                and k + 1 < len(tokens)
                and tokens[k + 1].text.lower() in {"true", "false"}
            ):
                dry = tokens[k + 1].text.lower() == "true" and tokens[k + 1].literal
                k += 1
            else:
                dry = True
        elif name in flags or (name.startswith("--no-") and "--" + name[5:] in flags):
            dry = False
        k += 1
    return dry


# ---- the tool readers ---------------------------------------------------------------------------


def _named_by_variable(first: str, program: str, args: list[str], cwd: Path) -> Risky | None:
    """A publish or push whose program or subcommand is a variable (`"$NPM" publish`,
    `npm $CMD`): what runs is known only when the command runs, so it is "could not check",
    never nothing. A variable followed by an ordinary subcommand only held options."""
    if program not in _OTHER_VERBS and not _VARIABLE.search(first):
        return None
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


def _unwrap(args: list[_Arg]) -> tuple[list[_Arg], str | None]:
    """Drop env-var prefixes and wrappers until the real program is first. Also returns the
    folder `env -C dir` / `sudo -D dir` run it in, if any."""
    chdir: str | None = None
    while args:
        word = args[0].text
        head = _base(word)
        if _ASSIGN.match(word) and args[0].known:
            args = args[1:]
        elif head in {"sudo", "env"}:
            args = args[1:]
            while args and (args[0].text.startswith("-") or _ASSIGN.match(args[0].text)):
                flag = args[0].text
                args = args[1:]
                if flag == "--":
                    break
                name, eq, value = flag.partition("=")
                values = ENV_VALUES if head == "env" else SUDO_VALUES
                if head == "env" and (name in {"-S", "--split-string"} or flag.startswith("-S")):
                    text = value if eq else (flag[2:] if flag != "-S" else "")
                    if not text and args:
                        text, args = args[0].text, args[1:]
                    try:
                        split = shlex.split(text)
                    except ValueError:
                        split = text.split()
                    args = [_Arg(s) for s in split] + args
                    break
                if name in values and not eq and len(flag) == 2 and args:
                    value, args = args[0].text, args[1:]
                    eq = "="
                elif flag[:2] in {"-C", "-D"} and len(flag) > 2:
                    name, value, eq = flag[:2], flag[2:], "="
                folder = {"-C", "--chdir"} if head == "env" else {"-D", "--chdir"}
                if eq and name in folder:
                    chdir = value
        elif head in {"timeout", "gtimeout"}:
            args = _drop_options(args[1:], {"-s", "-k", "--signal", "--kill-after"})
            args = args[1:]  # the duration
        elif head == "command" and args[1:2] and args[1].text in {"-v", "-V"}:
            return [], chdir  # command -v x: prints where x is
        elif head in PREFIXES:
            args = _drop_options(args[1:], PREFIXES[head])
        elif head in LAUNCHERS:
            args = _drop_options(args[1:], LAUNCHER_VALUES)
        elif _two_word_launcher(head, [a.text for a in args]):  # pipx run X, python -m X
            args = args[2:]
        elif head in RUNNERS and len(args) > 2 and args[1].text == "run":  # uv run X
            args = _drop_options(args[2:], RUNNER_VALUES)
            if args and args[0].text.endswith(".py"):  # uv run scripts/release.py
                args = [_Arg("python"), *args]
        else:
            return args, chdir
    return args, chdir


def _drop_options(args: list, with_value: set[str]) -> list:
    while args and args[0].text.startswith("-") and args[0].text not in {"-", "--"}:
        flag = args.pop(0).text
        if flag in with_value and args:
            args.pop(0)
    if args and args[0].text == "--":
        args = args[1:]
    return args


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
        return replace(where, unknown=where.unknown or why)
    there = os.path.expanduser(target)
    return replace(where, cwd=where.cwd / there, alts=tuple(alt / there for alt in where.alts))


def _shell_script(args: list[str]) -> int | None:
    """Where the script of `bash -c 'script'` is, or None."""
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return i + 1
    return None


def _dry_run(args: list[str], flags: set[str], node: bool) -> bool:
    """Is this a dry run? Only a literal, true flag counts: `--dry-run=false`, `--no-dry-run`
    and (npm and friends) `--dry-run false` publish for real; the last one given wins."""
    dry = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        name, eq, value = arg.partition("=")
        if name in flags:
            if eq:
                dry = value.lower() == "true"
            elif node and i + 1 < len(args) and args[i + 1].lower() in {"true", "false"}:
                dry = args[i + 1].lower() == "true"
                i += 1
            else:
                dry = True
        elif name.startswith("--no-") and "--" + name[5:] in flags:
            dry = False
        i += 1
    return dry


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
            if name in DRY_RUN and not eq and tool in NODE and value.lower() in {"true", "false"}:
                step = 2  # --dry-run false: npm reads the value
        else:
            out.words.append(arg)
            step = 1
        i += step
    return out


def _node(tool: str, args: list[str], cwd: Path, found: list[Risky], depth: int, seen) -> None:
    if tool == "bun" and {"-e", "--eval", "-p", "--print"} & set(args):  # bun -e '<code>'
        found.extend(following._interpreted("bun", args, cwd, depth))
        return
    opts = _options(tool, args, cwd)
    words = opts.words
    typed = words[0] if words else ""
    if tool == "npm" and words:  # npm pub, npm publ...: npm reads abbreviations
        words = [npm_command(typed), *words[1:]]
    if not words or words[0] in NOT_PUBLISH:
        return
    if tool == "yarn" and words[0] == "workspace" and len(words) > 2:
        inner: list[Risky] = []
        _node("yarn", words[2:], opts.cwd, inner, depth, seen)
        found.extend(replace(r, workspaces=(words[1],)) for r in inner)
        return
    if tool == "yarn" and words[:2] == ["workspaces", "foreach"]:
        rest = _drop_strs(args[args.index("foreach") + 1 :], {"--include", "--exclude"})
        inner = []
        _node("yarn", rest, opts.cwd, inner, depth, seen)  # foreach runs a yarn command
        found.extend(replace(r, all_workspaces=True) for r in inner)
        return
    if tool == "yarn" and words[:2] == ["npm", "publish"]:
        words = words[1:]
    if words[0] == "publish":
        _node_publish(tool, args, words[1:], opts, found, seen)
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
        rest = args[args.index(typed) + 1 :] if words[0] in EXEC_VERBS else words
        found.extend(detect(shlex.join(_drop_strs(list(rest), set())), opts.cwd, depth + 1))


def _drop_strs(words: list[str], with_value: set[str]) -> list[str]:
    while words and words[0].startswith("-") and words[0] not in {"-", "--"}:
        flag = words.pop(0)
        if flag in with_value and words:
            words.pop(0)
    if words and words[0] == "--":
        words = words[1:]
    return words


def _node_publish(
    tool: str, args: list[str], targets: list[str], opts: _Options, found: list[Risky], seen
) -> None:
    seen.add(Action.publish)
    if _dry_run(args, DRY_RUN, node=True):
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
    reading = following._READING.get()
    key = ("scripts", cwd, reading.writes if reading is not None else 0)
    if reading is not None and key in reading.parsed:
        return reading.parsed[key]  # type: ignore[return-value]
    try:
        data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    out = (
        {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(scripts, dict)
        else {}
    )
    if reading is not None:
        reading.parsed[key] = out
    return out


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
    seen.add(Action.publish)
    if _dry_run(args, DRY_RUN | {"-d"}, node=False):
        return
    found.append(Risky(Action.publish, tool, cwd, all_workspaces=tool in WORKSPACE_RELEASERS))


def _python(tool: str, args: list[str], cwd: Path, found: list[Risky], seen) -> None:
    opts = _options(tool, args, cwd)
    verb = "upload" if tool == "twine" else "publish"
    if not opts.words or opts.words[0] != verb:
        return
    seen.add(Action.publish)
    if _dry_run(args, DRY_RUN, node=False):
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
        seen.add(Action.push)
        if _dry_run(args[i + 1 :], {"--dry-run", "-n"}, node=False):
            return
        found.append(Risky(Action.push, "git", cwd))
