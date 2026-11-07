"""Windsurf (Codeium; the editor's docs now live under Devin Desktop).

Layout checked on macOS on 2026-11-05 (names only): ~/.codeium/windsurf/{memories/global_rules.md,
cascade/, brain/, code_tracker/, database/, implicit/, skills/} and
~/Library/Application Support/Windsurf/User/{globalStorage/state.vscdb, workspaceStorage/}.
mcp_config.json, hooks.json and User/settings.json are not present yet on the verification Mac,
so their names and keys come from the docs:
~/.codeium/windsurf/mcp_config.json {mcpServers.<name>.{command,args,env,serverUrl,headers}};
hooks in ~/.codeium/windsurf/hooks.json and <project>/.windsurf/hooks.json
{hooks.<event>[].command}; command allow list "windsurf.cascadeCommandsAllowList" (prefixes).
Docs: https://docs.windsurf.com/windsurf/cascade/hooks, https://docs.devin.ai/desktop/terminal,
https://docs.windsurf.com/windsurf/cascade/mcp (now redirects to docs.devin.ai).

Not covered: Cascade conversations (~/.codeium/windsurf/cascade/*.pb, binary protobuf), and
Devin Desktop's new locations (.devin/, ~/.config/devin/).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, Kind, PackageRule, Severity
from ..structured import load
from .base import artifacts, from_home, hook_commands, is_blanket_command, matches_any, mcp_servers

NAME = "windsurf"

_ALLOW_LIST = "windsurf.cascadeCommandsAllowList"
_ALLOW_FIX = (
    f'Delete this entry from "{_ALLOW_LIST}" in Windsurf\'s settings.json and add exact command '
    'prefixes instead (e.g. "git status", "npm test"); allow-listed commands run without asking.'
)
_SECRET_FIX = (
    'replace the value with "${env:<VAR>}" (Windsurf interpolates ${env:NAME} in '
    "mcp_config.json), then set <VAR> in your environment and restart Windsurf."
)


def _user_dirs(home: Path) -> list[Path]:
    """Windsurf's VS Code-style user folder on macOS, Linux and Windows."""
    return [
        home / "Library" / "Application Support" / "Windsurf" / "User",
        home / ".config" / "Windsurf" / "User",
        home / "AppData" / "Roaming" / "Windsurf" / "User",
    ]


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    allow = data.get(_ALLOW_LIST)
    return tuple(
        Grant(_ALLOW_LIST, entry.strip(), _ALLOW_FIX)
        for entry in (allow if isinstance(allow, list) else [])
        # The allow list matches command prefixes, so "bash" or "" approves anything.
        if isinstance(entry, str) and is_blanket_command(entry, prefix=True)
    )


# History that is only a record: none. memories/ holds Cascade's memories and rules (such as
# global_rules.md), which Cascade reads back into every chat, so fix leaves keys there alone.
_RECORDS: tuple[re.Pattern[str], ...] = ()


class Windsurf:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codeium/"),
            Severity.HIGH,
            "Windsurf (Codeium) home folder: MCP config, memories and Cascade data.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".codeium" / "windsurf"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "mcp_config.json",
            base / "hooks.json",
            *(user / "settings.json" for user in _user_dirs(home)),
        )
        yield from artifacts(NAME, Kind.HISTORY, base / "memories")  # Cascade memories and rules

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, root / ".windsurf" / "hooks.json")

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("mcp_config.json", "hooks.json", "settings.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name == "mcp_config.json":
            return AgentConfig(mcp_servers=mcp_servers(data.get("mcpServers"), _SECRET_FIX))
        if name == "hooks.json":
            return AgentConfig(hooks=hook_commands(data.get("hooks")))
        return AgentConfig(grants=_grants(data))
