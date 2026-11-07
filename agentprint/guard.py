"""Decide whether a shell command an agent is about to run should be blocked.

Agent-neutral: `integrations/` turn the agent's hook payload into (command, cwd) and the
verdict back into the agent's native reply. Only publishes and `git push` are checked; every
other command returns None straight away. Never runs a build and never touches the network:
npm projects are listed with `npm pack --dry-run --ignore-scripts`, Python publishes are checked
through the already-built files they upload. By default any internal error, or a publish or push
it cannot check, lets the command through with a warning saying so. In strict mode
(`AGENTPRINT_STRICT=1` or `guard --strict`) those cases block instead.

On Windows the command line is read as PowerShell (and the text of `cmd /c` as cmd):
`commands.detect` with `windows=True`.
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import platform
from .advice import Moment
from .checks import PACKAGE_WANT, check_package
from .commands import NODE, ROOT_RELEASERS, WORKSPACE_RELEASERS, Action, Risky, detect
from .gitcheck import check_push
from .models import CannotCheck, Finding
from .pack import PackageListing, PackError, read_archive, read_npm_project
from .report import FailOn, exit_code, to_agent
from .sanitize import clean

ARCHIVES = (".whl", ".tar.gz", ".zip", ".tgz")
DEFAULT_DIST = ("dist/*",)
JS_PUBLISHERS = NODE | WORKSPACE_RELEASERS | ROOT_RELEASERS
MAX_WORKSPACES = 30  # each one is an `npm pack --dry-run`; more would outlast hook timeouts


@dataclass(frozen=True)
class Verdict:
    block: bool
    reason: str = ""  # agent-format report; what the model sees when blocked
    warning: str | None = None  # shown to the human/agent, never blocks


__all__ = [
    "CannotCheck",
    "Verdict",
    "check_package_path",
    "evaluate",
    "strict_block",
    "strict_requested",
    "workspace_folders",
]


# Words that make a command look like a publish or push. Only used in strict mode, when the
# parser itself crashes: then only a command with one of these is blocked.
PUBLISH_WORDS = [
    "publish",
    "push",
    "upload",
    "twine",
    "changeset",
    "changesets",
    "lerna",
    "release-it",
    "semantic-release",
    "np",
]
_LOOKS_RISKY = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in PUBLISH_WORDS) + r")(?![\w-])", re.I
)

NEXT_STEP = "Run the publish or push as its own command, so agentprint can check it."
STRICT_HINT = (
    "Strict mode is on (AGENTPRINT_STRICT=1 or --strict), so a command agentprint cannot check "
    "is blocked. Fix the cause and retry, or ask the human to turn strict mode off."
)


def strict_requested(env: Mapping[str, str]) -> bool:
    """True when AGENTPRINT_STRICT is set to a yes-like value (1, true, yes, on)."""
    return env.get("AGENTPRINT_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}


def strict_block(why: str) -> Verdict:
    """The verdict strict mode gives when agentprint cannot check: a normal BLOCK."""
    return Verdict(True, f"agentprint: BLOCK (strict mode)\n{why}\n{STRICT_HINT}\n")


def evaluate(
    command: str,
    cwd: Path,
    fail_on: FailOn = FailOn.high,
    strict: bool = False,
    *,
    windows: bool | None = None,
    shell: str | None = None,
) -> Verdict | None:
    """None when the command neither publishes nor pushes.

    With `strict`, an internal error or a publish/push that cannot be checked blocks.
    `windows` says whether the agent runs on Windows (by default, whether agentprint does), and
    `shell` names the shell that runs the command (`sh`, `powershell`, `cmd`) when the agent's
    hook says; together they choose how the command line is read (`commands.detect`)."""
    try:
        on_windows = platform.is_windows() if windows is None else windows
        risky = detect(command, cwd, shell=shell, windows=on_windows)
    except Exception as exc:  # noqa: BLE001 - a parser bug must never block work
        return _failed(exc, strict and bool(_LOOKS_RISKY.search(command)))
    if not risky:
        return None
    try:
        return _decide(command, risky, fail_on, strict)
    except Exception as exc:  # noqa: BLE001 - fail open (unless strict), but say so
        return _failed(exc, strict)


def _failed(exc: Exception, strict: bool) -> Verdict:
    if strict:
        return strict_block(
            f"agentprint hit an internal error ({type(exc).__name__}) and could not check "
            "this command."
        )
    return Verdict(False, warning=_internal(exc))


def _internal(exc: Exception) -> str:
    return (
        f"agentprint hit an internal error ({type(exc).__name__}) and allowed the command "
        "without checking it. Please report this."
    )


def _decide(command: str, risky: list[Risky], fail_on: FailOn, strict: bool = False) -> Verdict:
    os.environ.setdefault("NPM_CONFIG_UPDATE_NOTIFIER", "false")  # keep npm pack offline
    findings: list[Finding] = []
    warnings: list[str] = []
    for item in risky:
        try:
            if item.unsure:  # never report OK after checking something else
                raise CannotCheck(item.unsure)
            if item.action is Action.push:
                findings += check_push(item.cwd)
            else:
                findings += _check_publish(item)
        except (CannotCheck, PackError) as exc:
            warnings.append(_could_not(item, exc))
    moment = Moment.publish if any(r.action is Action.publish for r in risky) else Moment.push
    warning = " ".join(warnings) or None
    if exit_code(findings, fail_on):
        return Verdict(True, to_agent(findings, fail_on, moment, rerun=command), warning)
    if strict and warnings:
        return strict_block(warning or "")
    return Verdict(False, warning=warning)


def _could_not(item: Risky, exc: Exception) -> str:
    """One "could not check" line. When it is about a file or command (`item.label`), it names
    that and says what to do instead."""
    if item.label:
        return f"agentprint could not check `{clean(item.label)}`: {clean(str(exc))}. {NEXT_STEP}"
    what = "git push" if item.action is Action.push else f"{item.tool} publish"
    return f"agentprint could not check this {what}: {clean(str(exc))}."


def _built(cwd: Path, patterns: tuple[str, ...]) -> list[PackageListing]:
    files = sorted(
        {
            Path(name)
            for pattern in patterns
            for name in glob.glob(os.path.join(cwd, pattern))
            if name.endswith(ARCHIVES) and os.path.isfile(name)
        }
    )
    if not files:
        raise CannotCheck(
            "no built package (.whl / .tar.gz) found to upload. Build it first, then run "
            "`agentprint pack dist/<file> --format agent`."
        )
    return [read_archive(file, PACKAGE_WANT) for file in files]


def check_package_path(path: Path) -> list[Finding]:
    """Findings for a built package file, an npm package folder, or a Python project's dist/.

    Never builds anything (no project code runs, no network)."""
    if path.is_file():
        listings = [read_archive(path, PACKAGE_WANT)]
    elif (path / "package.json").is_file():
        listings = [read_npm_project(path, PACKAGE_WANT)]
    elif (path / "pyproject.toml").is_file() or (path / "setup.py").is_file():
        listings = _built(path, DEFAULT_DIST)
    else:
        raise CannotCheck(f"{path} is not a package folder or a built package file")
    return [finding for listing in listings for finding in check_package(listing)]


def _check_publish(item: Risky) -> list[Finding]:
    if item.tool in JS_PUBLISHERS:
        if item.workspaces or item.all_workspaces:
            single_ok = item.tool in WORKSPACE_RELEASERS  # changesets also run in one-package repos
            folders = workspace_folders(item.cwd, item.workspaces, single_ok)
        else:
            folders = [item.cwd / target for target in item.targets or (".",)]
        return [finding for folder in folders for finding in _node_target(folder)]
    listings = _built(item.cwd, item.targets or DEFAULT_DIST)
    return [finding for listing in listings for finding in check_package(listing)]


def _package_json(folder: Path) -> dict:
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_patterns(root: Path) -> list[str]:
    """Workspace globs from package.json, pnpm-workspace.yaml and lerna.json."""
    patterns: list[object] = []
    spaces = _package_json(root).get("workspaces")
    patterns += spaces if isinstance(spaces, list) else []
    if isinstance(spaces, dict) and isinstance(spaces.get("packages"), list):
        patterns += spaces["packages"]
    for name, key in (("pnpm-workspace.yaml", "packages"), ("lerna.json", "packages")):
        try:
            text = (root / name).read_text(encoding="utf-8")
            data = json.loads(text) if name.endswith(".json") else yaml.safe_load(text)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and isinstance(data.get(key), list):
            patterns += data[key]
    return [p for p in patterns if isinstance(p, str) and p.strip()]


def workspace_folders(root: Path, names: tuple[str, ...], single_ok: bool) -> list[Path]:
    """The package folders a workspace publish would publish (private packages never are)."""
    patterns = _workspace_patterns(root)
    if not patterns:
        if single_ok and not names and (root / "package.json").is_file():
            return [root]
        raise CannotCheck("no workspaces are set up in package.json or pnpm-workspace.yaml")
    excluded = {
        Path(p)
        for pattern in patterns
        if pattern.startswith("!")
        for p in glob.glob(os.path.join(root, pattern[1:]), recursive=True)
    }
    folders = sorted(
        {
            Path(p)
            for pattern in patterns
            if not pattern.startswith("!")
            for p in glob.glob(os.path.join(root, pattern), recursive=True)
            if "node_modules" not in Path(p).parts and (Path(p) / "package.json").is_file()
        }
        - excluded
    )
    if names:
        return [_named_workspace(root, name, folders) for name in names]
    candidates = [root, *folders] if root not in folders else folders
    public = [f for f in candidates if not _package_json(f).get("private")]
    if not public:
        raise CannotCheck("no publishable workspace packages were found")
    if len(public) > MAX_WORKSPACES:
        raise CannotCheck(
            f"{len(public)} workspace packages are too many to check before a hook "
            "times out; run `agentprint pack <package folder>` for each"
        )
    return public


def _named_workspace(root: Path, name: str, folders: list[Path]) -> Path:
    by_path = root / name
    if (by_path / "package.json").is_file():
        return by_path
    for folder in folders:
        if _package_json(folder).get("name") == name or folder.name == name:
            return folder
    raise CannotCheck(f"no workspace package called {name!r}")


def _node_target(path: Path) -> list[Finding]:
    if not path.is_file() and not (path / "package.json").is_file():
        raise CannotCheck(f"no package.json in {path}")
    return check_package_path(path)
