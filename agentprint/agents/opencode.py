"""OpenCode (sst).

Layout verified on macOS on 2026-11-05 (file names, JSON key names and SQLite table names only):
~/.config/opencode/opencode.json ($schema, mcp.<name>.{type, command[], enabled});
~/.local/share/opencode/opencode.db (tables session, message, part, ...) and
~/.local/share/opencode/storage/ (JSON session files). Project config: opencode.json or
opencode.jsonc in the project root. Provider keys live in ~/.local/share/opencode/auth.json.
Docs: https://opencode.ai/docs/config/, https://opencode.ai/docs/permissions/,
https://opencode.ai/docs/mcp-servers/.

Not covered: OpenCode has no hook config; plugins are JavaScript files, which AP004 doesn't read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, Kind, PackageRule, Severity
from ..structured import load
from .base import artifacts, from_home, is_blanket_command, mcp_servers

NAME = "opencode"

_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
_PERMISSION_FIX = (
    'In opencode.json set "permission": {"*": "ask", "bash": {"*": "ask", "git status": '
    '"allow"}} (add the exact commands you trust as "allow" patterns). OpenCode allows most '
    "tools when nothing is set, so write the asks out explicitly."
)
_SECRET_FIX = (
    'replace the value with "{env:<VAR>}" (OpenCode substitutes {env:NAME} anywhere in '
    "opencode.json), then export <VAR> in the shell that starts OpenCode."
)


def _grants(permission: Any) -> tuple[Grant, ...]:
    if permission == "allow":  # the bare string sets every permission at once
        return (Grant("permission", "allow", _PERMISSION_FIX),)
    if not isinstance(permission, dict):
        return ()
    grants: list[Grant] = []
    if permission.get("*") == "allow":
        grants.append(Grant('permission["*"]', "allow", _PERMISSION_FIX))
    bash = permission.get("bash")
    if bash == "allow":
        grants.append(Grant("permission.bash", "allow", _PERMISSION_FIX))
    elif isinstance(bash, dict):
        for pattern, action in bash.items():
            # Glob patterns: "*" or "bash *" lets any command through.
            if action == "allow" and is_blanket_command(str(pattern), prefix=False):
                grants.append(Grant(f'permission.bash["{pattern}"]', "allow", _PERMISSION_FIX))
    return tuple(grants)


class OpenCode:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.local/share/opencode/"),
            Severity.HIGH,
            "OpenCode data folder: session database, logs and provider keys.",
        ),
        PackageRule(
            NAME, re.compile(r"(^|/)opencode\.db$"), Severity.HIGH, "OpenCode session database."
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)opencode/(auth|mcp-auth)\.json$"),
            Severity.HIGH,
            "OpenCode provider or MCP login tokens.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        config = home / ".config" / "opencode"
        data = home / ".local" / "share" / "opencode"
        yield from artifacts(NAME, Kind.SETTINGS, *(config / name for name in _CONFIG_NAMES))
        yield from artifacts(
            NAME, Kind.HISTORY, data / "opencode.db", data / "storage", data / "log"
        )
        # OpenCode's own key stores: never secret-scanned, only checked for who can read them.
        yield from artifacts(NAME, Kind.CREDENTIALS, data / "auth.json", data / "mcp-auth.json")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, *(root / name for name in _CONFIG_NAMES))

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name not in _CONFIG_NAMES:
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        return AgentConfig(
            grants=_grants(data.get("permission")),
            mcp_servers=mcp_servers(data.get("mcp"), _SECRET_FIX),
        )
