"""Factory Droid (the droid CLI).

Not installed on the verification Mac. Paths from the docs, read 2026-11-05: ~/.factory
(%USERPROFILE%\\.factory on Windows) holds settings.json and settings.local.json
(customModels[].apiKey for your own model keys, which may be written literally; a hooks
fallback), mcp.json
(mcpServers.<name>.{command, args, env, url, headers}) and hooks.json (Claude Code-style, keyed by
event with no wrapper). A project has the same files under .factory/; settings.local.json is
personal. Sessions: JSONL under ~/.factory/sessions/ according to Factory's own plugin repo (seen
through a mirror), not the docs.
Docs: https://docs.factory.com/droid-cli/settings.md, https://docs.factory.com/harness/mcp.md
(${NAME} in mcp.json), https://docs.factory.com/reference/hooks-reference,
https://docs.factory.com/model-independence/byok.md.

Not read: where the login token is kept (the docs give no path); permission rules (AP003).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    MCP_WHY,
    AgentDoc,
    EnvReference,
    artifacts,
    from_home,
    hook_commands,
    matches_any,
    read_mcp_servers,
)

NAME = "factory-droid"
_SECRET_FIX = (
    'replace the value with "${<VAR>}" (Droid expands ${NAME} in mcp.json, in env and headers, '
    "from the shell that starts it), then export <VAR> in the shell that starts Droid."
)
ENV = EnvReference("mcpServers", "${{var}}", "the shell that starts Droid")

_RECORDS = (re.compile(r"(^|/)\.factory/sessions/"),)

DOC = AgentDoc(
    label="Factory Droid",
    reads=(
        "`~/.factory/settings.json`, `settings.local.json` (model keys), `mcp.json` (MCP), "
        "`hooks.json`, and the same under a project's `.factory/`; sessions (JSONL)"
    ),
    keys="",
    fix=(
        'masks keys in sessions; MCP `env` keys in `mcp.json` become `"${NAME}"` (headers: '
        "needs you); keys in settings: needs you"
    ),
    checked="docs, 2026-11-05",
    unverified="`~/.factory/sessions/` (from Factory's plugin repo, not the docs)",
)


class FactoryDroid:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.factory/sessions/"),
            Severity.HIGH,
            "Factory Droid session transcripts.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.factory/settings\.local\.json$"),
            Severity.MEDIUM,
            "Personal Factory Droid settings; meant to be gitignored.",
        ),
        PackageRule(NAME, re.compile(r"(^|/)\.factory/mcp\.json$"), Severity.LOW, MCP_WHY),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".factory"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "settings.json",
            base / "settings.local.json",
            base / "mcp.json",
            base / "hooks.json",
        )
        yield from artifacts(NAME, Kind.HISTORY, base / "sessions")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".factory"
        yield from artifacts(
            NAME, Kind.SETTINGS, base / "settings.json", base / "mcp.json", base / "hooks.json"
        )
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.local.json", personal=True)

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def env_reference(self, path: Path) -> EnvReference | None:
        return ENV if path.name == "mcp.json" else None

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("settings.json", "settings.local.json", "mcp.json", "hooks.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name == "mcp.json":
            return AgentConfig(mcp_servers=read_mcp_servers(data, _SECRET_FIX, "mcpServers"))
        if name == "hooks.json":  # keyed by event, with no "hooks" wrapper
            return AgentConfig(hooks=hook_commands(data.get("hooks", data)))
        return AgentConfig(hooks=hook_commands(data.get("hooks")))
