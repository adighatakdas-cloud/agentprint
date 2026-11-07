"""Decide whether a shell command an agent is about to run should be blocked.

Agent-neutral: `integrations/` turn the agent's hook payload into (command, cwd) and the
verdict back into the agent's native reply. Only publishes and `git push` are checked; every
other command returns None straight away. Never runs a build and never touches the network:
npm projects are listed with `npm pack --dry-run --ignore-scripts`, Python publishes are checked
through the already-built files they upload. Any internal error lets the command through, with
a warning saying so.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

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


__all__ = ["CannotCheck", "Verdict", "check_package_path", "evaluate", "workspace_folders"]


def evaluate(command: str, cwd: Path, fail_on: FailOn = FailOn.high) -> Verdict | None:
    """None when the command neither publishes nor pushes."""
    try:
        risky = detect(command, cwd)
    except Exception as exc:  # noqa: BLE001 - a parser bug must never block work
        return Verdict(False, warning=_internal(exc))
    if not risky:
        return None
    try:
        return _decide(command, risky, fail_on)
    except Exception as exc:  # noqa: BLE001 - fail open, but say so
        return Verdict(False, warning=_internal(exc))


def _internal(exc: Exception) -> str:
    return (
        f"agentprint hit an internal error ({type(exc).__name__}) and allowed the command "
        "without checking it. Please report this."
    )


def _decide(command: str, risky: list[Risky], fail_on: FailOn) -> Verdict:
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
            what = "git push" if item.action is Action.push else f"{item.tool} publish"
            warnings.append(f"agentprint could not check this {what}: {clean(str(exc))}.")
    moment = Moment.publish if any(r.action is Action.publish for r in risky) else Moment.push
    warning = " ".join(warnings) or None
    if exit_code(findings, fail_on):
        return Verdict(True, to_agent(findings, fail_on, moment, rerun=command), warning)
    return Verdict(False, warning=warning)


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
