"""JetBrains Junie (Junie CLI and the IDE plugin).

Not installed on the verification Mac. Paths from the Junie docs, read 2026-11-05 and 2026-11-07:
~/.junie (%USERPROFILE%\\.junie on Windows) holds mcp/mcp.json (mcpServers.<name>.{command, args,
env, url, headers}; the IDE plugin reads it too), config.json (Claude-style hooks for the CLI) and
sessions/<id>/ (transcript.md next to events.jsonl). A project has .junie/mcp/mcp.json and
.junie/config.json.
Docs: https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html,
https://junie.jetbrains.com/docs/junie-cli-configuration.html,
https://junie.jetbrains.com/docs/junie-cli-hooks.html, https://junie.jetbrains.com/docs/junie-cli.html.

Not read: where BYOK keys and the login are stored (not documented); JUNIE_HOME, which moves the
folder; the action allowlist (AP003). .junie/AGENTS.md and rules are instructions, not leaks.
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
    artifacts,
    from_home,
    hook_commands,
    matches_any,
    read_mcp_servers,
)

NAME = "junie"
_SECRET_FIX = (
    "put the new key only in ~/.junie/mcp/mcp.json (never in a project's .junie/mcp/mcp.json) "
    "and make it private with chmod 600 ~/.junie/mcp/mcp.json: Junie's docs name no "
    "environment-variable reference for MCP servers, so <VAR> cannot be read from your shell."
)

_RECORDS = (re.compile(r"(^|/)\.junie/sessions/"),)

DOC = AgentDoc(
    label="Junie",
    reads=(
        "`~/.junie/sessions/` (transcripts, events); `~/.junie/mcp/mcp.json` (MCP), "
        "`config.json` (hooks), and the same under a project's `.junie/`"
    ),
    keys="",
    fix="masks keys in sessions; MCP keys: needs you",
    checked="docs, 2026-11-05 and 2026-11-07",
    unverified="`sessions/` and `config.json` (in the 2026-11-05 docs; not found again 2026-11-07)",
)


class Junie:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME, re.compile(r"(^|/)\.junie/sessions/"), Severity.HIGH, "Junie session transcripts."
        ),
        PackageRule(NAME, re.compile(r"(^|/)\.junie/mcp/mcp\.json$"), Severity.LOW, MCP_WHY),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".junie"
        yield from artifacts(NAME, Kind.SETTINGS, base / "mcp" / "mcp.json", base / "config.json")
        yield from artifacts(NAME, Kind.HISTORY, base / "sessions")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".junie"
        yield from artifacts(NAME, Kind.SETTINGS, base / "mcp" / "mcp.json", base / "config.json")

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("mcp.json", "config.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name == "mcp.json":
            return AgentConfig(mcp_servers=read_mcp_servers(data, _SECRET_FIX, "mcpServers"))
        return AgentConfig(hooks=hook_commands(data.get("hooks")))
