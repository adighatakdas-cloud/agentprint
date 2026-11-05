"""Spot the risky moments in a shell command line: publishing a package, or `git push`.

This is a best-effort reader of what an agent is about to run, not a shell. It follows `cd`,
`git -C`, env-var prefixes, common wrappers (`env`, `sudo`, `time`, `uvx`, `npx`, `python -m`,
`bash -c`) and chains (`&&`, `||`, `;`, `|`, newlines, subshells). Anything it can't parse is
matched by a plain word scan instead, so a publish is still caught.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Action(StrEnum):
    publish = "publish"
    push = "push"


@dataclass(frozen=True)
class Risky:
    action: Action
    tool: str  # npm, pnpm, yarn, bun, uv, twine, poetry, hatch, flit, git
    cwd: Path  # directory the command runs in, after any `cd`
    targets: tuple[str, ...] = ()  # explicit files/folders given to the publish command


NODE = {"npm", "pnpm", "yarn", "bun"}
PYTHON_PUBLISH = {"uv", "poetry", "hatch", "flit"}
NOT_PUBLISH = {"run", "run-script", "exec", "x", "dlx", "create", "install", "i", "add", "pack"}
# Options whose next word is a value, so that value is not mistaken for a subcommand or target.
VALUE_OPTS = {
    # npm / pnpm / yarn / bun
    "--tag", "--access", "--otp", "--registry", "--workspace", "-w", "--filter", "-F",
    "--publish-branch", "--userconfig", "--scope", "--loglevel", "--provenance-file",
    # uv / twine / poetry / hatch / flit
    "--index", "--publish-url", "--username", "-u", "--password", "-p", "--token", "-t",
    "--keyring-provider", "--check-url", "--trusted-publishing", "--project", "-r",
    "--repository", "--repository-url", "--comment", "-c", "--config-file", "--cert",
    "--client-cert", "--identity", "--sign-with", "--repo", "--user", "--auth", "--ca-cert",
    "--pypirc",
}  # fmt: skip
CWD_OPTS = {"--prefix", "--dir", "-C", "--cwd", "--directory"}  # change the package folder
GIT_VALUE_OPTS = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
SUDO_VALUE_OPTS = {"-u", "-g", "-p", "-C", "-h", "-U", "-r", "-t", "-T", "-D"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
LAUNCHERS = {"uvx", "npx", "pnpx", "bunx"}  # run the next word as a program
PREFIXES = {"time", "nohup", "command", "exec", "builtin", "nice", "caffeinate", "stdbuf"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^(python[0-9.]*|py|pypy[0-9.]*)$")
_SEPARATOR = re.compile(r"^[;&|()]+$")
_REDIRECT = re.compile(r"^[0-9]*(?:>>?|<<?<?)$")
_REDIRECT_ATTACHED = re.compile(r"^[0-9]*(?:>>?|<<?<?)\S")
_FD_DUP = re.compile(r"[0-9]*[<>]&[0-9-]+")  # 2>&1, >&2: the '&' is not a separator here
_AMP_REDIRECT = re.compile(r"&>>?")  # &>file sends both streams to file
_FALLBACK = re.compile(
    r"(?:^|[\s;&|(])(?:(npm|pnpm|yarn|bun|uv|poetry|hatch|flit)\s+(?:npm\s+)?publish"
    r"|(twine)\s+upload|(git)\s+push)(?=$|[\s;&|)])"
)


def detect(command: str, cwd: Path) -> list[Risky]:
    """Every publish or push in `command`, in order, with the directory each runs in."""
    try:
        text = _FD_DUP.sub(" ", command.replace("\n", " ; "))
        text = _AMP_REDIRECT.sub(" > ", text)
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes and similar
        return _fallback(command, cwd)
    found: list[Risky] = []
    stack: list[Path] = []
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if not _SEPARATOR.match(token):
            segment.append(token)
            continue
        cwd = _segment(segment, cwd, found)
        segment = []
        if "(" in token:
            stack.extend([cwd] * token.count("("))
        if ")" in token:
            for _ in range(token.count(")")):
                if stack:
                    cwd = stack.pop()
    return found


def _fallback(command: str, cwd: Path) -> list[Risky]:
    found = []
    for match in _FALLBACK.finditer(command):
        node_or_py, twine, git = match.groups()
        tool = node_or_py or twine or git
        found.append(Risky(Action.push if git else Action.publish, tool, cwd))
    return found


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


def _segment(words: list[str], cwd: Path, found: list[Risky]) -> Path:
    """Analyse one simple command. Returns the working directory after it."""
    words = _strip_redirects(words)
    words = _unwrap(words)
    if not words:
        return cwd
    program, args = Path(words[0]).name, words[1:]
    if program in {"cd", "pushd"}:
        return _chdir(cwd, args[0] if args else "~")
    if program in SHELLS:
        script = _shell_script(args)
        if script is not None:
            found.extend(detect(script, cwd))
        return cwd
    if program == "git":
        _git(args, cwd, found)
    elif program in NODE:
        _node(program, args, cwd, found)
    elif program in PYTHON_PUBLISH or program == "twine":
        _python(program, args, cwd, found)
    return cwd


def _unwrap(words: list[str]) -> list[str]:
    """Drop env-var prefixes and wrappers until the real program is first."""
    while words:
        head = Path(words[0]).name
        if _ASSIGN.match(words[0]):
            words = words[1:]
        elif head in {"sudo", "doas", "env"}:
            words = words[1:]
            while words and (words[0].startswith("-") or _ASSIGN.match(words[0])):
                flag = words.pop(0)
                if flag in SUDO_VALUE_OPTS and head != "env" and words:
                    words.pop(0)
        elif head in PREFIXES:
            words = words[1:]
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag == "-n" and words:  # nice -n 10
                    words.pop(0)
        elif head in LAUNCHERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag in {"--from", "--with", "-p", "--package", "--python"} and words:
                    words.pop(0)
        elif _two_word_launcher(head, words):  # pipx run X, python -m X
            words = words[2:]
        else:
            return words
    return words


def _two_word_launcher(head: str, words: list[str]) -> bool:
    if head == "pipx":
        return len(words) > 2 and words[1] == "run"
    return bool(_PYTHON.match(head)) and len(words) > 2 and words[1] == "-m"


def _chdir(cwd: Path, target: str) -> Path:
    if target == "-":
        return cwd
    return cwd / os.path.expanduser(target)


def _shell_script(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return args[i + 1] if i + 1 < len(args) else None
    return None


def _positionals(args: list[str], cwd: Path) -> tuple[list[str], Path]:
    """Non-option words, skipping option values; folder options move the working directory."""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            out.extend(args[i + 1 :])
            break
        if arg in CWD_OPTS and i + 1 < len(args):
            cwd = _chdir(cwd, args[i + 1])
            i += 2
            continue
        if arg in VALUE_OPTS:
            i += 2
            continue
        if not arg.startswith("-"):
            out.append(arg)
        i += 1
    return out, cwd


def _node(tool: str, args: list[str], cwd: Path, found: list[Risky]) -> None:
    words, cwd = _positionals(args, cwd)
    if not words or words[0] in NOT_PUBLISH:
        return
    if tool == "yarn" and words[:2] == ["npm", "publish"]:
        words = words[1:]
    if words[0] == "publish":
        found.append(Risky(Action.publish, tool, cwd, tuple(words[1:])))


def _python(tool: str, args: list[str], cwd: Path, found: list[Risky]) -> None:
    words, cwd = _positionals(args, cwd)
    verb = "upload" if tool == "twine" else "publish"
    if words and words[0] == verb:
        targets = tuple(words[1:]) if tool in {"uv", "twine"} else ()
        found.append(Risky(Action.publish, tool, cwd, targets))


def _git(args: list[str], cwd: Path, found: list[Risky]) -> None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            cwd = _chdir(cwd, args[i + 1])
            i += 2
        elif args[i] in GIT_VALUE_OPTS:
            i += 2
        else:
            i += 1
    if i < len(args) and args[i] == "push":
        found.append(Risky(Action.push, "git", cwd))
