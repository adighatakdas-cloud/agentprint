"""Find agent files, run every check, return findings most severe first."""

from __future__ import annotations

from pathlib import Path

from . import unread
from .agents import ALL_AGENTS
from .checks import SCAN_CHECKS
from .models import Artifact, Finding


def discover(home: Path | None, project: Path | None) -> list[Artifact]:
    found: dict[Path, Artifact] = {}
    for agent in ALL_AGENTS:
        if home is not None:
            for artifact in agent.discover_home(home):
                found.setdefault(artifact.path.resolve(), artifact)
        if project is not None:
            for artifact in agent.discover_project(project):
                found.setdefault(artifact.path.resolve(), artifact)
    return list(found.values())


def drop_claimed(findings: list[Finding]) -> list[Finding]:
    """A check with a `claims(finding, mine)` method reports some problems more precisely than
    another check (AP005 vs AP002 for MCP keys); keep only its version."""
    for check in SCAN_CHECKS:
        claims = getattr(check, "claims", None)
        if claims is None:
            continue
        mine = [f for f in findings if f.check_id == check.id]
        if mine:
            findings = [f for f in findings if f.check_id == check.id or not claims(f, mine)]
    return findings


def scan(home: Path | None, project: Path | None) -> list[Finding]:
    unread.forget()  # platform.scan_notes() then names what this scan could not read
    artifacts = discover(home, project)
    findings = drop_claimed([finding for check in SCAN_CHECKS for finding in check.run(artifacts)])
    return sorted(findings, key=lambda f: (-f.severity, f.check_id, f.location, f.line or 0))
