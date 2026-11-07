"""Gemini CLI (Google).

Layout checked on macOS on 2026-11-05 (names only): ~/.gemini/ and ~/.gemini/tmp/ exist but
hold no settings or chats yet on the verification Mac, so file names come from the docs:
~/.gemini/settings.json and <project>/.gemini/settings.json (general, tools.allowed,
mcpServers.<name>.{command,args,env,url,httpUrl,headers,trust}, hooks.<Event>[].hooks[].command);
chats, logs, shell history and checkpoints under ~/.gemini/tmp/<project_hash>/; API keys in
.gemini/.env or ~/.gemini/.env.
Docs: https://geminicli.com/docs/reference/configuration/, https://geminicli.com/docs/hooks/reference/,
https://geminicli.com/docs/tools/mcp-server/, https://geminicli.com/docs/cli/session-management/.

~/.gemini/antigravity/ belongs to Google Antigravity, a different product: not covered here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    artifacts,
    dig,
    from_home,
    gemini_folder,
    hook_commands,
    is_blanket_command,
    matches_any,
    mcp_servers,
)

NAME = "gemini-cli"

_SHELL_RULE = re.compile(r"run_shell_command(?:\((.*)\))?", re.DOTALL)
_SHELL_FIX = (
    "Delete this entry from tools.allowed in settings.json and list specific commands instead, "
    'e.g. "run_shell_command(git status)"; other shell commands will ask first.'
)
_TRUST_FIX = (
    'Set "trust": false for this server under mcpServers (or delete the key) so its tool calls '
    "ask for confirmation again."
)
_SECRET_FIX = (
    'replace the value with "$<VAR>" (Gemini CLI expands $VAR and ${VAR} in an MCP server\'s '
    '"env" block) and put <VAR> in ~/.gemini/.env or your shell profile. "headers" values are '
    "not expanded: keep such a server in ~/.gemini/settings.json, never in a project file."
)


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    allowed = dig(data, "tools", "allowed")
    for rule in allowed if isinstance(allowed, list) else []:
        if not isinstance(rule, str):
            continue
        rule_text = rule.strip()
        shell = _SHELL_RULE.fullmatch(rule_text)
        # Bare run_shell_command allows every command; run_shell_command(x) matches by prefix.
        if shell and (shell.group(1) is None or is_blanket_command(shell.group(1), prefix=True)):
            grants.append(Grant("tools.allowed", rule_text, _SHELL_FIX))
    servers = data.get("mcpServers")
    for name, server in servers.items() if isinstance(servers, dict) else []:
        if dig(server, "trust") is True:
            grants.append(Grant(f"mcpServers.{name}.trust", "true", _TRUST_FIX))
    return tuple(grants)


# History that is only a record (fix masks keys there): tmp/<project_hash>/ holds chats, logs,
# shell history and checkpoints. Memory (GEMINI.md) lives elsewhere and is not scanned.
_RECORDS = (re.compile(r"(^|/)\.gemini/tmp/"),)


class Gemini:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.gemini/tmp/"),
            Severity.HIGH,
            "Gemini CLI chat sessions, logs and shell history.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.gemini/(\.env|oauth_creds\.json|google_accounts\.json)$"),
            Severity.HIGH,
            "Gemini CLI API-key env file or login token.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.gemini/history/"),
            Severity.MEDIUM,
            "Gemini CLI checkpoint snapshots of project files.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = gemini_folder(home)  # honours GEMINI_CLI_HOME
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.json")
        yield from artifacts(NAME, Kind.HISTORY, base / "tmp")
        # Where Gemini CLI keeps keys and login tokens on purpose: permission check only.
        yield from artifacts(
            NAME,
            Kind.CREDENTIALS,
            base / ".env",
            base / "oauth_creds.json",
            base / "google_accounts.json",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".gemini"
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.json")
        yield from artifacts(NAME, Kind.CREDENTIALS, base / ".env", personal=True)

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name != "settings.json":
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        return AgentConfig(
            grants=_grants(data),
            hooks=hook_commands(data.get("hooks")),
            mcp_servers=mcp_servers(data.get("mcpServers"), _SECRET_FIX),
        )
