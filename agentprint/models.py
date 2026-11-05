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
    # Personal notes and env files (CLAUDE.local.md, .gemini/.env). Keys there are expected, so
    # the content is not scanned; the only question is whether git would commit them (AP007).
    PERSONAL = "personal"


@dataclass(frozen=True)
class Artifact:
    agent: str
    kind: Kind
    path: Path
    personal: bool = False  # belongs to one person and must never be committed (AP007)


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


@dataclass(frozen=True)
class Grant:
    """A setting that lets an agent act without asking (AP003)."""

    setting: str  # where it is set, e.g. "permissions.allow"
    value: str  # what is granted, e.g. "Bash(*)"
    fix: str  # the exact change that removes it


@dataclass(frozen=True)
class HookCommand:
    """A shell command an agent runs on an event (AP004)."""

    event: str
    command: str


@dataclass(frozen=True)
class McpServer:
    """One MCP server entry, flattened to the text fields a key could hide in (AP005)."""

    name: str
    fields: tuple[tuple[str, str], ...]  # (field, text), e.g. ("env.API_KEY", "API_KEY=...")
    secret_fix: str  # how to load a value from the environment instead; "<VAR>" = variable name


@dataclass(frozen=True)
class AgentConfig:
    """What an agent module understood from one of its config files."""

    grants: tuple[Grant, ...] = ()
    hooks: tuple[HookCommand, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
