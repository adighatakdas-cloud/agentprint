"""Shared data types."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

# Checks whose findings are a key, shown only as its redact() stub ("<rule>: sk-a…(len 105)").
# Ruling C1 and the baseline lean on this set: a new check that reports keys belongs in it.
KEY_CHECKS = frozenset({"AP001", "AP002", "AP005"})


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
    # An agent's own credential store (Codex auth.json, Hermes .env, Copilot providers.json).
    # Keys are meant to be there, so the content is never scanned; only who can read the file
    # is checked (AP008).
    CREDENTIALS = "credentials"
    # What an agent loads as instructions or runs as its tools: skills, commands, subagents,
    # rules and instruction files, and the files of installed plugins and extensions (their
    # hooks, scripts and MCP config). Read for risky content (AP010) and for MCP servers that
    # run an unpinned package (AP011); never secret-scanned, never permission-checked.
    EXTENSION = "extension"


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

    def __post_init__(self) -> None:
        # Not a dataclass field, so it never reaches to_dict() or the reports: the exact files
        # a check wants changed (AP008), for `agentprint fix` to use instead of re-deriving them.
        self.paths: tuple[Path, ...] = ()
        # Not a field either: what a finding that is not a key is about, for its baseline
        # fingerprint (AP003: the setting and the granted value; AP004: the hook event and its
        # network indicators). Never a key or a command's text. Empty: the check's title.
        self.subject: str = ""

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
    """One MCP server entry, flattened to the text fields a key could hide in (AP005), with the
    command line that starts it (AP011)."""

    name: str
    fields: tuple[tuple[str, str], ...]  # (field, text), e.g. ("env.API_KEY", "API_KEY=...")
    secret_fix: str  # how to load a value from the environment instead; "<VAR>" = variable name
    argv: tuple[str, ...] = ()  # the command and its arguments, as written (AP011)


@dataclass(frozen=True)
class AgentConfig:
    """What an agent module understood from one of its config files."""

    grants: tuple[Grant, ...] = ()
    hooks: tuple[HookCommand, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()


# Ends the detail of a key found in an older version of a file that a push would upload (in a
# commit on the way out, though no longer in the newest one): " (in pushed commit 1a2b3c4d5e6f)".
IN_PUSHED_COMMIT = " (in pushed commit "


class CannotCheck(Exception):
    """agentprint could not tell what a publish or push would upload. Callers report
    "could not check" (and allow), never "OK"."""
