"""Find agent files, run every check, return findings most severe first."""

from __future__ import annotations

from pathlib import Path

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


def scan(home: Path | None, project: Path | None) -> list[Finding]:
    artifacts = discover(home, project)
    findings = [finding for check in SCAN_CHECKS for finding in check.run(artifacts)]
    return sorted(findings, key=lambda f: (-f.severity, f.check_id, f.location, f.line or 0))
