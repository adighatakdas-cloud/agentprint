"""AP007: a personal agent file in a git repo is not gitignored (or is already committed).

Personal files are the ones agent modules mark `personal`: Claude Code's
.claude/settings.local.json and CLAUDE.local.md, Gemini CLI's .gemini/.env. They hold one
person's approvals, notes and keys, and `git add .` would publish them with the repo.

Uses only local git plumbing (rev-parse, ls-files, check-ignore): no network, nothing written.
check-ignore honours .gitignore, .git/info/exclude and the global excludes file, which is where
Claude Code itself ignores settings.local.json. Not a git repo, or git missing: no finding.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Iterable
from pathlib import Path

from ..models import Artifact, Finding, Severity
from .base import safe_snippet

_TIMEOUT = 10


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",  # never take index locks or refresh the index
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return subprocess.run(
            # core.fsmonitor=false: a repo's own config must not make us run its programs.
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def git_state(path: Path) -> tuple[Path, str, str] | None:
    """(repo root, path relative to it, "tracked" | "unignored"), or None when it's fine,
    not in a git repo, or git can't tell."""
    top_run = _git(path.parent, "rev-parse", "--show-toplevel")
    if top_run is None or top_run.returncode != 0 or not top_run.stdout.strip():
        return None
    top = Path(top_run.stdout.strip())
    try:
        rel = path.resolve().relative_to(top.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    # ":(literal)": a path is a path, never a glob. (check-ignore takes plain paths already.)
    tracked = _git(top, "ls-files", "--error-unmatch", "--", f":(literal){rel}")
    if tracked is None:
        return None
    if tracked.returncode == 0:
        return top, rel, "tracked"
    ignored = _git(top, "check-ignore", "-q", "--", rel)
    if ignored is not None and ignored.returncode == 1:  # 0 = ignored, 128 = error
        return top, rel, "unignored"
    return None


class LocalFileNotIgnored:
    id = "AP007"
    severity = Severity.LOW
    title = "Personal agent file not gitignored"
    fix = (
        "Add the file to .gitignore (and untrack it with git rm --cached if it was committed) "
        "so personal agent settings and keys never reach the repo."
    )

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact in artifacts:
            if not artifact.personal:
                continue
            state = git_state(artifact.path)
            if state is None:
                continue
            top, rel_path, status = state
            rel, root = safe_snippet(rel_path, 200), safe_snippet(str(top), 200)
            if status == "tracked":
                detail = f"{rel} is committed to git: everyone who clones the repo gets it"
                # shlex.quote: inside double quotes a folder named $(...) would still run.
                fix = (
                    f"In {shlex.quote(root)} run: git rm --cached -- {shlex.quote(rel)} ; then "
                    f"add the line {rel} to .gitignore and commit both. Ask the human to rotate "
                    "any key this file ever held: it stays in git history."
                )
            else:
                detail = f"{rel} is not in .gitignore: the next `git add .` would commit it"
                fix = f"Add this line to {root}/.gitignore: {rel}"
            findings.append(
                Finding(
                    check_id=self.id,
                    severity=self.severity,
                    title=self.title,
                    agent=artifact.agent,
                    location=str(artifact.path),
                    line=None,
                    detail=detail,
                    fix=fix,
                )
            )
        return findings


CHECK = LocalFileNotIgnored()
