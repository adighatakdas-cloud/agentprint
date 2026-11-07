"""Augment Code (the Auggie CLI and the editor extensions).

Not installed on the verification Mac. Paths from the docs, read 2026-11-05 and 2026-11-07:
~/.augment/settings.json (%USERPROFILE%\\.augment on Windows) with mcpServers and Claude-style
hooks; a project's .augment/settings.json (shared) and .augment/settings.local.json (personal).
The login is session.json in ~/.augment (named on the 2026-11-05 docs pages; the 2026-11-07 CLI
reference names no file).
Docs: https://docs.augmentcode.com/cli/config, https://docs.augmentcode.com/cli/hooks,
https://docs.augmentcode.com/cli/integrations (${VAR} in MCP entries),
https://docs.augmentcode.com/cli/reference,
https://docs.augmentcode.com/cli/automation/service-accounts.

session.json is Augment's own key store: never secret-scanned, only checked for who can read it.
Not read: sessions (the default folder is not documented); toolPermissions (AP003).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from ..structured import load
from .base import AgentDoc, EnvReference, artifacts, from_home, hook_commands, read_mcp_servers

NAME = "augment"
_NAMES = ("settings.json", "settings.local.json")
_SECRET_FIX = (
    'replace the value with "${<VAR>}" (Auggie expands ${NAME} in an MCP server\'s command, args, '
    "env, url and headers), then export <VAR> in the shell that starts Auggie."
)
ENV = EnvReference("mcpServers", "${{var}}", "the shell that starts Auggie")

DOC = AgentDoc(
    label="Augment (Auggie)",
    reads="`~/.augment/settings.json` and project `.augment/settings*.json` (MCP, hooks)",
    keys="`~/.augment/session.json`",
    fix='MCP `env` keys become `"${NAME}"` (headers: needs you)',
    checked="docs, 2026-11-05 and 2026-11-07",
    unverified="`session.json` (not named on the current CLI reference page)",
)


class Augment:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.augment/session\.json$"),
            Severity.HIGH,
            "Augment (Auggie) login token in plain text.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.augment/settings\.local\.json$"),
            Severity.MEDIUM,
            "Personal Augment settings.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".augment"
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.json")
        yield from artifacts(NAME, Kind.CREDENTIALS, base / "session.json")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".augment"
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.json")
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.local.json", personal=True)

    def pure_history(self, path: Path) -> bool:
        return False  # no Augment history is read yet

    def env_reference(self, path: Path) -> EnvReference | None:
        return ENV if path.name in _NAMES else None

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name not in _NAMES:
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        return AgentConfig(
            hooks=hook_commands(data.get("hooks")),
            mcp_servers=read_mcp_servers(data, _SECRET_FIX, "mcpServers"),
        )
