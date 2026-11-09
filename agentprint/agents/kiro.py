"""Kiro (AWS): the IDE and Kiro CLI.

Not installed on the verification Mac. Paths from the docs, read 2026-11-05: ~/.kiro
(%USERPROFILE%\\.kiro on Windows) holds settings/mcp.json (mcpServers.<name>.{command, args, env,
url, headers}), settings/cli.json and, for the CLI, hooks/*.json
({"version": "v1", "hooks": [{"trigger", "matcher", "action": {"type": "command", "command"}}]});
a project has .kiro/settings/mcp.json and .kiro/hooks/.
Docs: https://kiro.dev/docs/configuration/, https://kiro.dev/docs/mcp/configuration/,
https://kiro.dev/docs/hooks/, https://kiro.dev/docs/hooks/actions/.

Not read yet: the CLI's session database (the docs say only "a SQLite database in ~/.kiro/"),
the IDE's chats and where sign-ins are stored, none of them named in the docs; KIRO_HOME, which
moves the folder. .kiro/steering/ and .kiro/specs/ are instructions and plans, not leaks.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, HookCommand, Kind, PackageRule, Severity
from ..structured import load
from .base import MCP_WHY, AgentDoc, artifacts, from_home, read_mcp_servers

NAME = "kiro"
_SECRET_FIX = (
    'replace the value with "${<VAR>}" and approve <VAR> when Kiro asks (it expands only '
    "variables you have approved), then export <VAR> in the shell that starts Kiro."
)

DOC = AgentDoc(
    label="Kiro",
    reads=(
        "`~/.kiro/settings/mcp.json` (MCP), `cli.json`, CLI hooks; project "
        "`.kiro/settings/mcp.json` and `.kiro/hooks/`"
    ),
    keys="",
    fix="MCP keys: needs you (approve an env variable in Kiro)",
    checked="docs, 2026-11-05",
)


def _hooks(data: Any) -> tuple[HookCommand, ...]:
    """Commands from a Kiro hook file: a list of hooks, each with a trigger and an action."""
    entries = data.get("hooks") if isinstance(data, dict) else None
    found: list[HookCommand] = []
    for entry in entries if isinstance(entries, list) else []:
        action = entry.get("action") if isinstance(entry, dict) else None
        command = action.get("command") if isinstance(action, dict) else None
        if isinstance(command, str) and command and entry.get("enabled") is not False:
            found.append(HookCommand(str(entry.get("trigger", "")), command))
    return tuple(found)


class Kiro:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(NAME, re.compile(r"(^|/)\.kiro/settings/mcp\.json$"), Severity.LOW, MCP_WHY),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".kiro"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "settings" / "mcp.json",
            base / "settings" / "cli.json",
            base / "hooks",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".kiro"
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings" / "mcp.json", base / "hooks")

    def pure_history(self, path: Path) -> bool:
        return False  # no Kiro history is read yet

    def read_config(self, artifact: Artifact) -> AgentConfig:
        path = artifact.path
        if path.name == "mcp.json":
            return AgentConfig(mcp_servers=read_mcp_servers(load(path), _SECRET_FIX, "mcpServers"))
        if path.parent.name == "hooks" and path.suffix == ".json":
            return AgentConfig(hooks=_hooks(load(path)))
        return AgentConfig()
