"""Claude Desktop (Anthropic's desktop app), as an MCP client.

Not checked on the verification Mac. Paths from the MCP docs, read 2026-11-07:
claude_desktop_config.json (mcpServers.<name>.{command, args, env}) in
~/Library/Application Support/Claude/ on macOS, %APPDATA%\\Claude\\ on Windows and ~/.config/Claude/
on Linux; logs (mcp*.log, which can echo a server's output) in ~/Library/Logs/Claude/ on macOS and
in the logs/ folder next to the config elsewhere.
Docs: https://modelcontextprotocol.io/docs/develop/connect-local-servers.

Chats are kept in the cloud, not in a documented local file, and the app has no hooks. No package
rule: MCP server projects often ship an example claude_desktop_config.json on purpose.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind
from .base import AgentDoc, app_dirs, artifacts, from_home, matches_any, read_mcp_file

NAME = "claude-desktop"
CONFIG = "claude_desktop_config.json"
_SECRET_FIX = (
    "put the new key only in this file and make it private with chmod 600 on it: Claude "
    "Desktop documents no environment-variable reference in claude_desktop_config.json, so "
    "<VAR> cannot be read from your shell."
)

# Logs are only a record (fix masks keys there).
_RECORDS = (re.compile(r"(^|/)Logs/Claude/"), re.compile(r"(^|/)Claude/logs/"))

DOC = AgentDoc(
    label="Claude Desktop",
    reads=(
        "`claude_desktop_config.json` (MCP servers) in `~/Library/Application Support/Claude/`, "
        "`%APPDATA%\\Claude\\` or `~/.config/Claude/`; app logs, including `mcp*.log`"
    ),
    keys="",
    fix="masks keys in logs; MCP keys: needs you (no env reference)",
    checked="MCP docs (modelcontextprotocol.io), 2026-11-07",
)


class ClaudeDesktop:
    name = NAME
    doc = DOC
    package_rules = ()

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        apps = app_dirs(home, "Claude")
        yield from artifacts(NAME, Kind.SETTINGS, *(app / CONFIG for app in apps))
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            home / "Library" / "Logs" / "Claude",
            *(app / "logs" for app in apps[1:]),
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        return iter(())

    def extension_paths(self, home: Path | None, project: Path | None) -> list[Path]:
        return []  # extensions' install folder is not in the docs read

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name != CONFIG:
            return AgentConfig()
        return read_mcp_file(artifact.path, _SECRET_FIX, "mcpServers")
