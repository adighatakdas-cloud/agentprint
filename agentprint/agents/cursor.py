"""Cursor (Anysphere): the IDE and the cursor-agent CLI.

Layout verified on macOS on 2026-11-05 (file names, JSON key names and SQLite table names only):
~/.cursor/mcp.json {mcpServers}; ~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl
(role, message.content[].text); ~/.cursor/projects/<slug>/terminals/;
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb and
workspaceStorage/<id>/state.vscdb (tables ItemTable, cursorDiskKV: IDE chat history).
Docs: https://cursor.com/docs/context/mcp, https://cursor.com/docs/cli/reference/permissions,
https://cursor.com/docs/cli/reference/configuration, https://cursor.com/docs/agent/hooks.

Not covered: the IDE's auto-run ("yolo") mode is stored inside state.vscdb, not in a JSON
settings file, so AP003 cannot see it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, Kind, PackageRule, Severity
from ..structured import load
from .base import artifacts, dig, hook_commands, is_blanket_command, mcp_servers

NAME = "cursor"

_SHELL_RULE = re.compile(r"Shell\((.*)\)", re.DOTALL)
_SHELL_FIX = (
    "Delete this entry from permissions.allow and list the exact base commands instead, e.g. "
    '"Shell(git)", "Shell(npm)"; anything not listed will ask first.'
)
_MCP_FIX = (
    'Replace "Mcp(*:*)" with the specific tools you use, e.g. "Mcp(github:get_issue)", in '
    "permissions.allow."
)
_MODE_FIX = (
    'Set "approvalMode": "allowlist" in ~/.cursor/cli-config.json so only commands listed in '
    "permissions.allow run without asking."
)
_SECRET_FIX = (
    'replace the value with "${env:<VAR>}" (Cursor interpolates ${env:NAME} in command, args, '
    'env, url and headers; stdio servers can also load an "envFile"), then set <VAR> in your '
    "environment."
)


def _user_dirs(home: Path) -> list[Path]:
    """Cursor's VS Code-style user folder on macOS, Linux and Windows."""
    return [
        home / "Library" / "Application Support" / "Cursor" / "User",
        home / ".config" / "Cursor" / "User",
        home / "AppData" / "Roaming" / "Cursor" / "User",
    ]


def _glob(base: Path, pattern: str) -> list[Path]:
    try:
        return sorted(base.glob(pattern))
    except OSError:
        return []


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    allow = dig(data, "permissions", "allow")
    for rule in allow if isinstance(allow, list) else []:
        if not isinstance(rule, str):
            continue
        rule_text = rule.strip()
        shell = _SHELL_RULE.fullmatch(rule_text)
        # Shell(commandBase) matches by base command, so Shell(bash) runs anything via bash -c.
        if shell and is_blanket_command(shell.group(1), prefix=True):
            grants.append(Grant("permissions.allow", rule_text, _SHELL_FIX))
        elif rule_text.replace(" ", "") == "Mcp(*:*)":
            grants.append(Grant("permissions.allow", rule_text, _MCP_FIX))
    if data.get("approvalMode") == "unrestricted":
        grants.append(Grant("approvalMode", "unrestricted", _MODE_FIX))
    return tuple(grants)


class Cursor:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.cursor/(projects|chats)/"),
            Severity.HIGH,
            "Cursor agent transcripts, terminal output or chat databases.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)state\.vscdb(\.backup)?$"),
            Severity.HIGH,
            "Cursor / VS Code-family editor state database; holds AI chat history.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.cursor/cli-config\.json$"),
            Severity.MEDIUM,
            "Personal Cursor CLI config and permission allowlist.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.cursor/mcp\.json$"),
            Severity.LOW,
            "Cursor MCP server config; rarely meant for a package (keys inside are reported "
            "separately).",
        ),
    )

    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".cursor"
        users = _user_dirs(home)
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "mcp.json",
            base / "cli-config.json",
            base / "hooks.json",
            *(user / "settings.json" for user in users),
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            base / "projects",  # agent transcripts (.jsonl) and terminal output, per project
            base / "chats",  # cursor-agent CLI chat stores (SQLite); absent on the verified Mac
            *(user / "globalStorage" / "state.vscdb" for user in users),
            # One state.vscdb per workspace; the .backup copies are skipped to avoid duplicates.
            *(db for user in users for db in _glob(user / "workspaceStorage", "*/state.vscdb")),
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".cursor"
        yield from artifacts(
            NAME, Kind.SETTINGS, base / "mcp.json", base / "cli.json", base / "hooks.json"
        )

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("mcp.json", "cli-config.json", "cli.json", "hooks.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name == "mcp.json":
            return AgentConfig(mcp_servers=mcp_servers(data.get("mcpServers"), _SECRET_FIX))
        if name == "hooks.json":
            return AgentConfig(hooks=hook_commands(data.get("hooks")))
        return AgentConfig(grants=_grants(data))
