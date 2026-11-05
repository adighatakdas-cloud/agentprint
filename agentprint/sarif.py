"""SARIF 2.1.0 output, for GitHub code scanning and other SARIF viewers.

Rules are built from the metadata every finding carries (check id, title, fix hint, severity),
so a new check shows up here without touching this file. Finding details are already redacted;
text taken from scanned files or packages is cleaned of control characters before it is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Collection, Sequence
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .models import Finding, Severity

SCHEMA_URI = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
)
INFO_URI = "https://github.com/adighatakdas-cloud/agentprint"
SRCROOT = "%SRCROOT%"
MAX_TEXT = 500

LEVEL = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
}
# GitHub buckets security-severity scores: >=9.0 critical, 7.0-8.9 high, 4.0-6.9 medium, else low.
SECURITY_SEVERITY = {
    Severity.CRITICAL: "9.5",
    Severity.HIGH: "8.0",
    Severity.MEDIUM: "5.5",
    Severity.LOW: "2.0",
}


def _clean(text: str) -> str:
    """Drop control, format (bidi) and other invisible characters; cap the length."""
    cleaned = "".join("?" if unicodedata.category(ch).startswith("C") else ch for ch in text)
    return cleaned if len(cleaned) <= MAX_TEXT else cleaned[: MAX_TEXT - 1] + "…"


def _relative(path: Path, root: Path) -> str | None:
    """POSIX path of `path` relative to `root`, or None if it lies outside."""
    candidates = ((Path(os.path.abspath(path)), root), (path.resolve(), root.resolve()))
    for file, base in candidates:
        try:
            return file.relative_to(base).as_posix()
        except ValueError:
            continue
    return None


def _file_location(path: Path, root: Path) -> dict:
    rel = _relative(path, root)
    if rel is not None:
        return {"uri": quote(rel), "uriBaseId": SRCROOT}
    return {"uri": Path(os.path.abspath(path)).as_uri()}


def _split(location: str, labels: Collection[str]) -> tuple[str, str] | None:
    """Split a pack location `label!member` on a known label (labels may contain '!')."""
    for label in sorted(labels, key=len, reverse=True):
        if location.startswith(label + "!"):
            return label, location[len(label) + 1 :]
    return None


def _source_file(project: Path, member: str) -> Path | None:
    """The project file a package member was built from, if it can be found safely."""
    parts = member.lstrip("/").split("/")
    root = project.resolve()
    for candidate in ("/".join(parts), "/".join(parts[1:])):  # npm path, or sdist "name-ver/…"
        if not candidate:
            continue
        file = project / candidate
        try:
            inside = file.resolve().is_relative_to(root)
        except (OSError, ValueError):  # unreadable, or a name the OS can't represent
            continue
        if inside and file.is_file():
            return file
    return None


class _Artifacts:
    """run.artifacts: archives (top level) and the members inside them (nested)."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._index: dict[tuple[str, int | None], int] = {}

    def add(self, location: dict, parent: int | None = None) -> int:
        key = (json.dumps(location, sort_keys=True), parent)
        if key not in self._index:
            item: dict = {"location": location}
            if parent is not None:
                item["parentIndex"] = parent
            self._index[key] = len(self.items)
            self.items.append(item)
        return self._index[key]


def _rules(findings: Sequence[Finding]) -> list[dict]:
    worst: dict[str, Finding] = {}
    for f in findings:
        if f.check_id not in worst or f.severity > worst[f.check_id].severity:
            worst[f.check_id] = f
    return [
        {
            "id": check_id,
            "shortDescription": {"text": f.title},
            "fullDescription": {"text": f.title},
            "help": {"text": f.fix, "markdown": f"**How to fix:** {f.fix}"},
            "helpUri": f"{INFO_URI}#checks",
            "defaultConfiguration": {"level": LEVEL[f.severity]},
            "properties": {
                "security-severity": SECURITY_SEVERITY[f.severity],
                "tags": ["security", "secrets", "ai-agents"],
            },
        }
        for check_id, f in sorted(worst.items())
    ]


def _message(f: Finding, where: str | None) -> str:
    text = f"{f.title} ({f.agent}): {f.detail}"
    if where:
        text += f" [in {where}]"
    if f.count > 1:
        text += f" (seen {f.count} times)"
    return _clean(text)


def to_sarif(
    findings: Sequence[Finding],
    *,
    root: Path | None = None,
    package: Path | None = None,
    labels: Collection[str] = (),
) -> str:
    """Render findings as a SARIF 2.1.0 log.

    root: base for relative URIs (default: the current directory, which is the repository root
      in GitHub Actions). package/labels: for `pack`, the target (an archive or a project
      directory) and the listing labels used in "label!member" locations.
    """
    root = Path(os.path.abspath(root or Path.cwd()))
    rules = _rules(findings)
    rule_index = {rule["id"]: i for i, rule in enumerate(rules)}
    artifacts = _Artifacts()
    results = []
    for f in findings:
        split = _split(f.location, labels)
        where = None
        if split is None:
            artifact_location = _file_location(Path(f.location), root)
        else:
            label, member = split
            where = f"{label}!{member}"
            source = _source_file(package, member) if package and package.is_dir() else None
            if source is not None:
                artifact_location = _file_location(source, root)
            else:
                if package is not None and package.is_file():
                    archive = _file_location(package, root)
                else:  # a package that was only listed, never written to disk
                    archive = {"uri": quote(label)}
                member_uri = "/" + quote(member.lstrip("/"))
                index = artifacts.add({"uri": member_uri}, parent=artifacts.add(archive))
                artifact_location = {"uri": member_uri, "index": index}
        physical: dict = {"artifactLocation": artifact_location}
        if f.line:
            physical["region"] = {"startLine": f.line}
        identity = "\0".join(
            [f.check_id, json.dumps(artifact_location, sort_keys=True), str(f.line or 0), f.detail]
        )
        results.append(
            {
                "ruleId": f.check_id,
                "ruleIndex": rule_index[f.check_id],
                "level": LEVEL[f.severity],
                "message": {"text": _message(f, where)},
                "locations": [{"physicalLocation": physical}],
                "partialFingerprints": {
                    "agentprint/v1": hashlib.sha256(identity.encode()).hexdigest()
                },
            }
        )
    run: dict = {
        "tool": {
            "driver": {
                "name": "agentprint",
                "version": __version__,
                "informationUri": INFO_URI,
                "rules": rules,
            }
        },
        "originalUriBaseIds": {SRCROOT: {"uri": root.as_uri().rstrip("/") + "/"}},
        "results": results,
    }
    if artifacts.items:
        run["artifacts"] = artifacts.items
    return json.dumps({"$schema": SCHEMA_URI, "version": "2.1.0", "runs": [run]}, indent=2)
