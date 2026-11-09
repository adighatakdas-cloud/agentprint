"""Zed (the editor's own agent).

Not installed on the verification Mac. Paths from Zed's docs and source, read 2026-11-05 and
2026-11-07: threads in threads/threads.db (SQLite) under the data folder
(~/Library/Application Support/Zed on macOS, ~/.local/share/zed on Linux, %LOCALAPPDATA%\\Zed on
Windows); settings.json (JSON with comments) in ~/.config/zed (%APPDATA%\\Zed on Windows) and
.zed/settings.json in a project, with MCP servers under context_servers
(command, args, env; remote: url, headers).
Docs: https://zed.dev/docs/ai/mcp, https://zed.dev/docs/configuring-zed,
github.com/zed-industries/zed crates/paths/src/paths.rs and crates/agent/src/db.rs.

Zed compresses each thread (zstd) before saving it, so only the text it keeps uncompressed
(thread titles) is read; provider keys are in the system keychain, never in a file.
Ruling: project .zed/settings.json is shared config, read for MCP keys but never flagged as
personal (editor settings are committed on purpose in many repos).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from .base import AgentDoc, artifacts, from_home, matches_any, read_mcp_file

NAME = "zed"
_SECRET_FIX = (
    "put the new key only in your own settings.json (never in a project's .zed/settings.json) "
    "and make that file private with chmod 600; for a remote server, leave the Authorization "
    "header out and Zed signs in with OAuth where the server supports it. Zed's docs name no "
    "environment-variable reference here, so <VAR> cannot be read from your shell."
)

_RECORDS = (re.compile(r"(?i)(^|/)zed/threads/threads\.db$"),)

DOC = AgentDoc(
    label="Zed",
    reads=(
        "`threads/threads.db` (SQLite; titles only, thread bodies are compressed) in "
        "`~/Library/Application Support/Zed`, `~/.local/share/zed` or `%LOCALAPPDATA%\\Zed`; "
        "`settings.json` in `~/.config/zed` or `%APPDATA%\\Zed`, and project `.zed/settings.json` "
        "(MCP `context_servers`)"
    ),
    keys="",
    fix="keys in the database and MCP keys: needs you",
    checked="docs and source, 2026-11-05 and 2026-11-07",
    unverified="`threads.db` on Linux and Windows, `settings.json` on Windows (Zed's source only)",
)


class Zed:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(?i)(^|/)zed/threads/threads\.db$"),
            Severity.HIGH,
            "Zed agent thread database.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            home / ".config" / "zed" / "settings.json",
            home / "AppData" / "Roaming" / "Zed" / "settings.json",
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            *(
                data / "threads" / "threads.db"
                for data in (
                    home / "Library" / "Application Support" / "Zed",
                    home / ".local" / "share" / "zed",
                    home / "AppData" / "Local" / "Zed",
                )
            ),
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, root / ".zed" / "settings.json")

    def extension_paths(self, home: Path | None, project: Path | None) -> list[Path]:
        # Project instructions: the first of .rules, .cursorrules, .windsurfrules, .clinerules,
        # .github/copilot-instructions.md, AGENT.md, AGENTS.md, CLAUDE.md, GEMINI.md (the rest
        # are read through the agents that own them), and ~/.config/zed/AGENTS.md
        # (https://zed.dev/docs/ai/rules, read 2026-11-07).
        paths: list[Path] = []
        if home is not None:
            paths += [home / ".config" / "zed" / "AGENTS.md"]
        if project is not None:
            paths += [project / ".rules", project / "AGENT.md"]
        return paths

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name != "settings.json":
            return AgentConfig()
        return read_mcp_file(artifact.path, _SECRET_FIX, "context_servers")
