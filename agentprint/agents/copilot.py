"""GitHub Copilot: the Copilot CLI and Copilot chat in VS Code.

Layout verified on macOS on 2026-11-05 (file names, JSON key names only): ~/.copilot/
{config.json, mcp-config.json (mcpServers.<name>.{type, command, args, tools}), logs/, skills/};
VS Code ~/Library/Application Support/Code/User/workspaceStorage/<id>/chatSessions/*.json(l)
(version, requests, sessionId, ...). session-state/, hooks/, settings.json and providers.json are
not present yet on the verification Mac; their names come from the docs.
Docs: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference,
https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference,
https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers,
https://code.visualstudio.com/docs/agents/approvals,
https://code.visualstudio.com/docs/agents/reference/mcp-configuration.

Credential stores (config.json: login state; providers.json: BYOK keys in plain text;
mcp-oauth-config/, mcp-secrets/) are listed only for the permission check, never secret-scanned.
A project .mcp.json is read through the Claude Code module (same file, both agents load it).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import platform
from ..models import AgentConfig, Artifact, Grant, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    EnvReference,
    artifacts,
    from_home,
    glob,
    hook_commands,
    is_blanket_command,
    matches_any,
    mcp_servers,
    read_mcp_servers,
)

NAME = "copilot"

_CLI_MODE_FIX = (
    'Set "defaultPermissionMode": "manual" (or "assisted") in ~/.copilot/settings.json; use '
    "--allow-all / --yolo only for a one-off run inside a container or VM."
)
_VSCODE_ALL_FIX = (
    'Set "chat.tools.global.autoApprove": false in VS Code settings.json and approve tools per '
    "workspace instead."
)
_VSCODE_TERMINAL_FIX = (
    'Delete this entry from "chat.tools.terminal.autoApprove" in VS Code settings.json and list '
    'exact command prefixes instead, e.g. "git status": true.'
)
_CLAUDE_SKIP_FIX = (
    'Set "github.copilot.chat.claudeAgent.allowDangerouslySkipPermissions": false in VS Code '
    "settings.json."
)
_CLI_SECRET_FIX = (
    "keep this server only in ~/.copilot/mcp-config.json (never in a repo's .mcp.json or "
    ".github/mcp.json), make the file private with chmod 600 ~/.copilot/mcp-config.json, and put "
    "the new key there: Copilot CLI documents no environment-variable expansion for this file, "
    "so <VAR> cannot be read from your shell."
)
_VSCODE_SECRET_FIX = (
    'replace the value with "${input:<VAR>}" and add {"id": "<VAR>", "type": "promptString", '
    '"password": true} to the file\'s "inputs" list: VS Code then asks for the key once and keeps '
    "it in its secret storage."
)
# VS Code-format mcp.json (top key "servers") names an environment variable as ${env:NAME}.
# https://code.visualstudio.com/docs/copilot/reference/mcp-configuration (read 2026-11-07)
VSCODE_ENV = EnvReference("servers", "${env:{var}}", "the environment VS Code starts from")
_REGEX_ALL = re.compile(r"/\^?\.[*+]\$?/[a-z]*")


def _user_dirs(home: Path) -> list[Path]:
    """VS Code's user folder on macOS, Linux and Windows."""
    return [
        home / "Library" / "Application Support" / "Code" / "User",
        home / ".config" / "Code" / "User",
        platform.roaming_appdata(home) / "Code" / "User",
    ]


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    if data.get("defaultPermissionMode") == "allow-all":  # Copilot CLI settings.json
        grants.append(Grant("defaultPermissionMode", "allow-all", _CLI_MODE_FIX))
    if data.get("chat.tools.global.autoApprove") is True:  # VS Code: YOLO in every workspace
        grants.append(Grant("chat.tools.global.autoApprove", "true", _VSCODE_ALL_FIX))
    terminal = data.get("chat.tools.terminal.autoApprove")
    for rule, approved in terminal.items() if isinstance(terminal, dict) else []:
        # Keys are command prefixes, or /regex/ patterns.
        blanket = _REGEX_ALL.fullmatch(rule) or is_blanket_command(rule, prefix=True)
        if approved is True and blanket:
            grants.append(Grant("chat.tools.terminal.autoApprove", rule, _VSCODE_TERMINAL_FIX))
    skip = "github.copilot.chat.claudeAgent.allowDangerouslySkipPermissions"
    if data.get(skip) is True:
        grants.append(Grant(skip, "true", _CLAUDE_SKIP_FIX))
    return tuple(grants)


# History that is only a record (fix masks keys there): each session's events.jsonl, the
# session store, typed-command history, logs, and VS Code's chat sessions and edit snapshots.
# Other files in a session folder (its plan, workspace files) are read back, so they are not.
_RECORDS = (
    re.compile(r"(^|/)\.copilot/session-state/(?:[^/]+/)*[^/]+\.jsonl$"),
    re.compile(r"(^|/)\.copilot/session-store\.db$"),
    re.compile(r"(^|/)\.copilot/command-history-state(/|$)"),
    re.compile(r"(^|/)\.copilot/logs/"),
    re.compile(r"(^|/)workspaceStorage/[^/]+/(chatSessions|chatEditingSessions)/"),
    re.compile(r"(^|/)globalStorage/(emptyWindowChatSessions|transferredChatSessions)/"),
)


class Copilot:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.github/copilot/settings\.local\.json$"),
            Severity.MEDIUM,
            "Personal Copilot CLI settings; GitHub's docs say to keep it out of git.",
        ),
        PackageRule(
            NAME,
            re.compile(
                r"(^|/)\.copilot/(session-state|command-history-state|logs|mcp-oauth-config"
                r"|mcp-secrets)/|(^|/)\.copilot/session-store\.db$"
            ),
            Severity.HIGH,
            "Copilot CLI session history, logs or MCP token fallbacks.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.copilot/(config|providers)\.json$"),
            Severity.HIGH,
            "Copilot CLI login state or BYOK provider keys (plain text).",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.copilot/(mcp-config|permissions-config)\.json$"),
            Severity.MEDIUM,
            "Personal Copilot CLI MCP config or saved approvals; may hold server tokens.",
        ),
        PackageRule(
            NAME,
            # Only where VS Code keeps them, so a chat app's own src/chatSessions/ is fine.
            re.compile(
                r"(^|/)workspaceStorage/[^/]+/(chatSessions|chatEditingSessions)/"
                r"|(^|/)globalStorage/(emptyWindowChatSessions|transferredChatSessions)/"
            ),
            Severity.HIGH,
            "VS Code chat sessions (Copilot chat history and edit snapshots).",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.(vscode|github)/mcp\.json$"),
            Severity.LOW,
            "MCP server config; rarely meant for a package (keys inside are reported separately).",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".copilot"
        users = _user_dirs(home)
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "settings.json",
            base / "mcp-config.json",
            base / "hooks",
            base / "permissions-config.json",
            *(user / name for user in users for name in ("settings.json", "mcp.json")),
            # Each VS Code profile keeps its own mcp.json (the folder name is not in the docs).
            *(mcp for user in users for mcp in glob(user / "profiles", "*/mcp.json")),
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            base / "session-state",  # events.jsonl per session, plus workspace artifacts
            base / "session-store.db",
            base / "command-history-state",
            base / "logs",
            *(
                session
                for user in users
                for pattern in ("*/chatSessions", "*/chatEditingSessions")
                for session in glob(user / "workspaceStorage", pattern)
            ),
            *(
                user / "globalStorage" / name
                for user in users
                for name in ("emptyWindowChatSessions", "transferredChatSessions")
            ),
        )
        yield from artifacts(
            NAME,
            Kind.CREDENTIALS,
            base / "config.json",
            base / "providers.json",
            base / "mcp-oauth-config",
            base / "mcp-secrets",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        github = root / ".github"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            github / "copilot" / "settings.json",
            github / "hooks",
            github / "mcp.json",
            root / ".vscode" / "mcp.json",
        )
        yield from artifacts(
            NAME, Kind.SETTINGS, github / "copilot" / "settings.local.json", personal=True
        )

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def env_reference(self, path: Path) -> EnvReference | None:
        """VS Code's own mcp.json files (workspace .vscode/mcp.json, the user's and each
        profile's), never Copilot CLI's mcp-config.json or a repo's .github/mcp.json."""
        return VSCODE_ENV if path.name == "mcp.json" and path.parent.name != ".github" else None

    def read_config(self, artifact: Artifact) -> AgentConfig:
        path = artifact.path
        if path.suffix != ".json":
            return AgentConfig()
        data = load(path)
        if not isinstance(data, dict):
            return AgentConfig()
        if path.parent.name == "hooks":  # ~/.copilot/hooks/*.json, .github/hooks/*.json
            return AgentConfig(hooks=hook_commands(data.get("hooks")))
        if path.name in ("mcp-config.json", "mcp.json"):
            if isinstance(data.get("servers"), dict):  # VS Code format: .vscode/mcp.json
                servers = read_mcp_servers(data, _VSCODE_SECRET_FIX, "servers")
                return AgentConfig(mcp_servers=servers)
            servers = data.get("mcpServers")
            if servers is None and path.parent.name == ".github":
                servers = data  # project files may leave out the mcpServers wrapper
            return AgentConfig(mcp_servers=mcp_servers(servers, _CLI_SECRET_FIX))
        if path.name in ("settings.json", "settings.local.json"):
            return AgentConfig(grants=_grants(data), hooks=hook_commands(data.get("hooks")))
        return AgentConfig()
