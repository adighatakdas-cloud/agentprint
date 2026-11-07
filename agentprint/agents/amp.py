"""Amp (ampcode.com): the CLI.

Not installed on the verification Mac. Paths from the docs, read 2026-11-05: settings in
~/.config/amp/settings.json or settings.jsonc (the same path under %USERPROFILE% on Windows) and a
project's .amp/settings.json[c]; keys are flat names with an "amp." prefix, MCP servers under
"amp.mcpServers" ({command, args, env} or {url, headers}), which expand ${NAME}. Threads live on
ampcode.com, not on disk. A third-party tool reports pasted keys in ~/.local/share/amp/secrets.json
(not in Amp's docs): probed, and only checked for who can read it.
Docs: https://ampcode.com/docs/cli/settings, https://ampcode.com/docs/customize/mcp.

Not covered: plugins (TypeScript).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule
from .base import AgentDoc, artifacts, from_home, read_mcp_file

NAME = "amp"
SERVERS = "amp.mcpServers"
_NAMES = ("settings.json", "settings.jsonc")
_SECRET_FIX = (
    'replace the value with "${<VAR>}" (Amp expands ${NAME} in amp.mcpServers), then export '
    "<VAR> in the shell that starts Amp."
)

DOC = AgentDoc(
    label="Amp",
    reads="`~/.config/amp/settings.json(c)` and project `.amp/settings.json(c)` (`amp.mcpServers`)",
    keys="`~/.local/share/amp/secrets.json`",
    fix="MCP keys: needs you",
    checked="docs, 2026-11-05",
    unverified="`~/.local/share/amp/secrets.json` (third-party report)",
)


class Amp:
    name = NAME
    doc = DOC
    package_rules: tuple[PackageRule, ...] = ()

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        config = home / ".config" / "amp"
        yield from artifacts(NAME, Kind.SETTINGS, *(config / name for name in _NAMES))
        yield from artifacts(
            NAME, Kind.CREDENTIALS, home / ".local" / "share" / "amp" / "secrets.json"
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, *(root / ".amp" / name for name in _NAMES))

    def pure_history(self, path: Path) -> bool:
        return False  # Amp keeps no history on disk

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name not in _NAMES:
            return AgentConfig()
        return read_mcp_file(artifact.path, _SECRET_FIX, SERVERS)
