"""Claude Code (Anthropic). Layout verified on macOS on 2026-10-08."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import Artifact, Kind, PackageRule, Severity
from .base import artifacts

NAME = "claude-code"


class ClaudeCode:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.claude/settings\.local\.json$"),
            Severity.HIGH,
            "Personal permission allowlist; often holds pasted commands with keys.",
        ),
        PackageRule(
            NAME, re.compile(r"(^|/)\.claude/.+\.jsonl$"), Severity.HIGH, "Agent conversation log."
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.claude\.json$"),
            Severity.HIGH,
            "Global Claude Code state, including MCP server config.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)CLAUDE\.local\.md$"),
            Severity.MEDIUM,
            "Personal project notes meant to stay local.",
        ),
    )

    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".claude"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "settings.json",
            base / "settings.local.json",
            home / ".claude.json",
            base / "backups",
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            base / "history.jsonl",  # every prompt typed, plus pasted text
            base / "paste-cache",  # large pastes
            base / "projects",  # full transcripts, per project
            base / "file-history",  # backups of files Claude edited (can include .env)
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            root / ".claude" / "settings.json",
            root / ".claude" / "settings.local.json",
            root / ".mcp.json",
        )
