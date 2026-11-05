"""Shared machinery for checks: the Check shape, secret scanning, parsed agent configs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from ..agents import by_name
from ..files import iter_lines
from ..models import AgentConfig, Artifact, Finding, Kind, Severity
from ..secrets import find_secrets, redact


class Check(Protocol):
    id: str
    severity: Severity
    title: str
    fix: str  # general advice; each finding carries the exact fix for its case

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]: ...


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def safe_snippet(text: str, limit: int = 120) -> str:
    """Text taken from a scanned file, made safe to show: secrets redacted, control and ANSI
    characters removed, whitespace collapsed, length capped."""
    for match in sorted(find_secrets(text), key=lambda m: m.start, reverse=True):
        text = text[: match.start] + redact(match.value) + text[match.end :]
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def agent_configs(artifacts: Iterable[Artifact]) -> Iterator[tuple[Artifact, AgentConfig]]:
    """Each settings file with what its agent module understood from it."""
    for artifact in artifacts:
        if artifact.kind is not Kind.SETTINGS:
            continue
        agent = by_name(artifact.agent)
        if agent is None:
            continue
        try:
            config = agent.read_config(artifact)
        except Exception:  # fail open: a parser bug must never stop a scan
            continue
        yield artifact, config


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
