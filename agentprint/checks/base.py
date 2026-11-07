"""Shared machinery for checks: the Check shape, secret scanning, parsed agent configs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from .. import secrets
from ..agents import by_name
from ..files import iter_lines
from ..models import AgentConfig, Artifact, Finding, Kind, Severity
from ..secrets import Budget, SecretMatch, find_hidden, find_secrets, hidden_spans, redact


class Check(Protocol):
    # Read-only members: implementations only need to be readable.
    @property
    def id(self) -> str: ...
    @property
    def severity(self) -> Severity: ...
    @property
    def title(self) -> str: ...
    @property
    def fix(self) -> str: ...  # general advice; each finding carries the exact fix for its case

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]: ...


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def safe_snippet(text: str, limit: int = 120) -> str:
    """Text taken from a scanned file, made safe to show: secrets redacted (every copy of a
    hidden one too, with all the text it was hidden in), control and ANSI characters removed,
    whitespace collapsed, length capped."""
    spans: list[SecretMatch] = []
    found = [*find_secrets(text, placeholders=True), *hidden_spans(text)]  # placeholders too
    found.sort(key=lambda m: m.start)
    for match in found:  # in text order; a hidden one may overlap a plain one
        if spans and match.start < spans[-1].end:
            if match.end > spans[-1].end:
                last = spans[-1]
                spans[-1] = SecretMatch(last.rule_id, last.name, last.value, last.start, match.end)
            continue
        spans.append(match)
    for match in reversed(spans):
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
        hidden: list[tuple[SecretMatch, int | None]] = []
        chunk: list[tuple[int, str]] = []  # the file's text, a chunk at a time, for the deep pass
        size = 0
        budget = Budget()
        for number, line in iter_lines(artifact.path):
            for match in find_secrets(line):
                if match.value in seen:
                    seen[match.value].count += 1
                    continue
                seen[match.value] = self._finding(artifact, number or None, match)
            chunk.append((number, line if line.endswith("\n") else line + "\n"))
            size += len(line)
            if size >= secrets.DEEP_CHUNK_CHARS:
                hidden += _hidden_in_chunk(chunk, seen, budget)
                chunk = _overlap(chunk)
                size = sum(len(text) for _, text in chunk)
        if chunk:
            hidden += _hidden_in_chunk(chunk, seen, budget)
        for match, number in hidden:  # after every plain key of the file is known
            if match.value not in seen:
                seen[match.value] = self._finding(artifact, number, match)
        return list(seen.values())

    def _finding(self, artifact: Artifact, line: int | None, match: SecretMatch) -> Finding:
        return Finding(
            check_id=self.id,
            severity=self.severity,
            title=self.title,
            agent=artifact.agent,
            location=str(artifact.path),
            line=line,  # None: text from a database row
            detail=f"{match.name}: {redact(match.value)}{secrets.hidden_note(match)}",
            fix=self.fix,
        )


def _hidden_in_chunk(
    chunk: list[tuple[int, str]], known: Iterable[str], budget: Budget
) -> list[tuple[SecretMatch, int | None]]:
    """Hidden keys in a chunk of lines, each with the line its first character is on."""
    text = "".join(line for _, line in chunk)
    found = []
    line, counted = 0, 0
    for match in find_hidden(text, set(known), budget):  # in text order
        line += text.count("\n", counted, match.start)
        counted = match.start
        found.append((match, chunk[line][0] or None))
    return found


def _overlap(chunk: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The last lines of a chunk, read again with the next one so that a key folded over the
    boundary is whole in it."""
    room = min(64 * 1024, secrets.DEEP_CHUNK_CHARS // 4)
    kept: list[tuple[int, str]] = []
    for number, line in reversed(chunk):
        room -= len(line)
        if room < 0:
            break
        kept.append((number, line))
    return kept[::-1]
