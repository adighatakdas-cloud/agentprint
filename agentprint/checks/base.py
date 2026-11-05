"""Shared machinery for checks that look for secrets in agent files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..files import iter_lines
from ..models import Artifact, Finding, Kind, Severity
from ..secrets import find_secrets, redact


@dataclass(frozen=True)
class SecretCheck:
    id: str
    severity: Severity
    title: str
    fix: str
    kind: Kind

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact in artifacts:
            if artifact.kind is self.kind:
                findings.extend(self._scan(artifact))
        return findings

    def _scan(self, artifact: Artifact) -> list[Finding]:
        seen: dict[str, Finding] = {}  # one finding per distinct key per file
        for number, line in iter_lines(artifact.path):
            for match in find_secrets(line):
                if match.value in seen:
                    seen[match.value].count += 1
                    continue
                seen[match.value] = Finding(
                    check_id=self.id,
                    severity=self.severity,
                    title=self.title,
                    agent=artifact.agent,
                    location=str(artifact.path),
                    line=number or None,  # 0 = text from a database row
                    detail=f"{match.name}: {redact(match.value)}",
                    fix=self.fix,
                )
        return list(seen.values())
