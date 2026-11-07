"""The badge files: a shields.io endpoint and a small verdict. Counts only, never a path or key."""

from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .models import Finding
from .report import FailOn, exit_code, summary

_COLOR = {"CLEAR": "5CC8FF", "BLOCK": "FF3B4E"}


def verdict_of(findings: list[Finding], fail_on: FailOn) -> str:
    """BLOCK exactly when the exit code would be 1."""
    return "BLOCK" if exit_code(findings, fail_on) else "CLEAR"


def badge_data(verdict: str) -> dict:
    return {
        "schemaVersion": 1,
        "label": "agentprint",
        "message": verdict,
        "color": _COLOR[verdict],
        "labelColor": "050B14",
    }


def verdict_data(*, command: str, fail_on: FailOn, findings: list[Finding]) -> dict:
    return {
        "tool": "agentprint",
        "version": __version__,
        "command": command,
        "fail_on": fail_on.value,
        "verdict": verdict_of(findings, fail_on),
        "summary": summary(findings),
    }


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def write(
    directory: Path, *, command: str, fail_on: FailOn, findings: list[Finding]
) -> tuple[Path, Path]:
    """Write badge.json and verdict.json into an existing directory; OSError if it can't."""
    verdict = verdict_data(command=command, fail_on=fail_on, findings=findings)
    badge_path, verdict_path = directory / "badge.json", directory / "verdict.json"
    badge_path.write_text(_dump(badge_data(verdict["verdict"])), encoding="utf-8")
    verdict_path.write_text(_dump(verdict), encoding="utf-8")
    return badge_path, verdict_path
