"""What a `git push` would publish: agent files tracked by git, and keys inside them.

Read-only and offline: only local `git ls-files` / `git rev-parse` are run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import platform
from .agents import ALL_AGENTS
from .checks.ap002_settings_secret import CHECK as AP002
from .checks.ap005_mcp_inline_secret import CHECK as AP005
from .checks.ap006_package_leak import check_listing, is_agent_file
from .checks.ap007_local_not_ignored import LocalFileNotIgnored as AP007
from .models import CannotCheck, Finding, Kind
from .pack import MAX_CONTENT_BYTES, PackageListing
from .sanitize import clean
from .scanner import drop_claimed

GIT_TIMEOUT = 20
PUSHED_TITLE = "Agent file would be pushed to the git remote"
PUSHED_FIX = "Untrack it with `git rm --cached`, add it to .gitignore, commit, then push."


def _git(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(root), "-c", "core.quotepath=false", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT,
            **platform.subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def repo_root(cwd: Path) -> Path | None:
    """The repository's top folder, spelled relative to `cwd` (so /var vs /private/var and
    symlinked checkouts keep the caller's spelling)."""
    out = _git(cwd, "rev-parse", "--show-cdup")
    if out is None:
        return None
    return Path(os.path.normpath(cwd / out.strip())) if out.strip() else cwd


def _read(path: Path) -> bytes:
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_CONTENT_BYTES:
            return path.read_bytes()
    except OSError:
        pass
    return b""


def check_push(cwd: Path) -> list[Finding]:
    """Findings for what a push from `cwd` would publish. Raises CannotCheck when it can't
    look (no such folder, git missing); a folder outside any repository has nothing to push."""
    if not cwd.is_dir():
        raise CannotCheck(f"the folder {clean(str(cwd))!r} does not exist")
    if shutil.which("git") is None:
        raise CannotCheck("git is not installed, so the repository can't be read")
    root = repo_root(cwd)
    if root is None:
        return []
    tracked_out = _git(root, "ls-files", "-z")
    tracked = [name for name in (tracked_out or "").split("\0") if name]
    findings = _tracked_agent_files(root, tracked)
    findings += _tracked_settings_secrets(root, set(tracked), findings)
    findings += _not_ignored(root)
    return sorted(findings, key=lambda f: (-f.severity, f.check_id, f.location, f.line or 0))


def _tracked_agent_files(root: Path, tracked: list[str]) -> list[Finding]:
    agent_files = [name for name in tracked if is_agent_file(name)]
    listing = PackageListing(
        label="git",
        files=agent_files,
        contents={name: _read(root / name) for name in agent_files},
    )
    findings = check_listing(listing)
    for finding in findings:
        finding.location = str(root / finding.location.partition("!")[2])
        if finding.check_id != AP002.id:
            finding.title, finding.fix = PUSHED_TITLE, PUSHED_FIX
    return findings


def _tracked_settings_secrets(
    root: Path, tracked: set[str], already: list[Finding]
) -> list[Finding]:
    """Keys in tracked shared agent settings (.mcp.json, .claude/settings.json, ...), reported
    exactly as a scan reports them (an MCP-config key is AP005, other keys AP002)."""
    seen = {f.location for f in already}
    artifacts = [
        artifact
        for agent in ALL_AGENTS
        for artifact in agent.discover_project(root)
        if artifact.kind is Kind.SETTINGS
        and str(artifact.path) not in seen
        and artifact.path.relative_to(root).as_posix() in tracked
    ]
    return drop_claimed([*AP002.run(artifacts), *AP005.run(artifacts)])


def _not_ignored(root: Path) -> list[Finding]:
    out = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    return [
        # Same problem the AP007 check reports in a scan, so it carries that check's metadata.
        Finding(
            AP007.id,
            AP007.severity,
            AP007.title,
            "agent",
            str(root / name),
            None,
            "Agent file that is neither committed nor ignored; `git add .` would commit it.",
            AP007.fix,
        )
        for name in (out or "").split("\0")
        if name and is_agent_file(name)
    ]
