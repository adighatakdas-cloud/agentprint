"""Shared data types."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from pathlib import Path


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class Kind(StrEnum):
    HISTORY = "history"  # chat logs, transcripts, pasted text, edit backups
    SETTINGS = "settings"  # permission allowlists, hooks, agent and MCP config


@dataclass(frozen=True)
class Artifact:
    agent: str
    kind: Kind
    path: Path


@dataclass
class Finding:
    check_id: str
    severity: Severity
    title: str
    agent: str
    location: str
    line: int | None
    detail: str  # always redacted
    fix: str
    count: int = 1

    def to_dict(self) -> dict:
        data = asdict(self)
        data["severity"] = self.severity.name.lower()
        return data


@dataclass(frozen=True)
class PackageRule:
    """A file an agent writes that should never ship inside a published package."""

    agent: str
    pattern: re.Pattern[str]
    severity: Severity
    why: str
