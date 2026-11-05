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
import os
from dataclasses import dataclass
from pathlib import Path

from .advice import Moment
from .checks.ap006_package_leak import check_listing, is_agent_file
from .commands import NODE, Action, Risky, detect
from .gitcheck import check_push
from .models import Finding
from .pack import PackageListing, PackError, read_archive, read_npm_project
from .report import FailOn, exit_code, to_agent
from .sanitize import clean

ARCHIVES = (".whl", ".tar.gz", ".zip", ".tgz")
DEFAULT_DIST = ("dist/*",)


@dataclass(frozen=True)
class Verdict:
    block: bool
    reason: str = ""  # agent-format report; what the model sees when blocked
    warning: str | None = None  # shown to the human/agent, never blocks


class CannotCheck(Exception):
    """agentprint could not tell what this publish would upload."""


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
        if item.action is Action.push:
            findings += check_push(item.cwd)
            continue
        try:
            findings += _check_publish(item)
        except (CannotCheck, PackError) as exc:
            warnings.append(
                f"agentprint could not check this {item.tool} publish: {clean(str(exc))}"
            )
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
    return [read_archive(file, is_agent_file) for file in files]


def check_package_path(path: Path) -> list[Finding]:
    """Findings for a built package file, an npm package folder, or a Python project's dist/.

    Never builds anything (no project code runs, no network)."""
    if path.is_file():
        listings = [read_archive(path, is_agent_file)]
    elif (path / "package.json").is_file():
        listings = [read_npm_project(path, is_agent_file)]
    elif (path / "pyproject.toml").is_file() or (path / "setup.py").is_file():
        listings = _built(path, DEFAULT_DIST)
    else:
        raise CannotCheck(f"{path} is not a package folder or a built package file")
    return [finding for listing in listings for finding in check_listing(listing)]


def _check_publish(item: Risky) -> list[Finding]:
    if item.tool in NODE:
        return [f for target in item.targets or (".",) for f in _node_target(item.cwd / target)]
    listings = _built(item.cwd, item.targets or DEFAULT_DIST)
    return [finding for listing in listings for finding in check_listing(listing)]


def _node_target(path: Path) -> list[Finding]:
    if not path.is_file() and not (path / "package.json").is_file():
        raise CannotCheck(f"no package.json in {path}")
    return check_package_path(path)
