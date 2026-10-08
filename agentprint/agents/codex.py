"""OpenAI Codex (CLI and app). Layout verified on macOS on 2026-10-08."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import Artifact, Kind, PackageRule, Severity
from .base import artifacts

NAME = "codex"


class Codex:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codex/(sessions|archived_sessions|attachments)/"),
            Severity.HIGH,
            "Codex session logs or pasted attachments.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codex/(history\.jsonl|auth\.json)$"),
            Severity.HIGH,
            "Codex prompt history or login token.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codex/config\.toml$"),
            Severity.MEDIUM,
            "Personal Codex config; may hold MCP server tokens.",
        ),
    )

    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".codex"
        # auth.json is Codex's own login store; a token there is expected, so it is not scanned.
        yield from artifacts(NAME, Kind.SETTINGS, base / "config.toml")
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            base / "history.jsonl",
            base / "sessions",
            base / "archived_sessions",
            base / "attachments",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, root / ".codex" / "config.toml")
