"""Hermes Agent (Nous Research).

Layout checked on macOS on 2026-11-05 (names only): ~/.hermes/ exists with skills/ only, so the
other names come from the docs: config.yaml (approvals.mode, command_allowlist,
hooks_auto_accept, hooks.<event>[].command, mcp_servers.<name>.{command,args,env,url,headers}),
state.db (SQLite: full message history), sessions/, memories/, cron/, logs/, SOUL.md, and the
plain-text key stores .env and auth.json. Named profiles repeat the layout under
~/.hermes/profiles/<name>/.
Docs: https://hermes-agent.nousresearch.com/docs/user-guide/configuration/,
https://hermes-agent.nousresearch.com/docs/user-guide/security/,
https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks,
https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference,
https://github.com/advisories/GHSA-99f9-j8r3-p853 (CVE-2026-53870: world-readable history).

.env, auth.json, webhook_subscriptions.json and vault/vault.key are Hermes's intended key stores:
never secret-scanned, only checked for who can read them (AP008). Hermes keeps no project folder;
its project files (.hermes.md, HERMES.md, AGENTS.md) are instructions, not leaks.
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
    hook_commands,
    is_blanket_command,
    matches_any,
    mcp_servers,
)

NAME = "hermes"

_MODE_FIX = (
    "Set approvals.mode: smart (or manual) in ~/.hermes/config.yaml, and list commands that must "
    "never run under approvals.deny. Use --yolo only for a one-off run inside a container or VM."
)
_ALLOWLIST_FIX = (
    "Delete this entry from command_allowlist in ~/.hermes/config.yaml and keep only exact "
    'commands you trust, e.g. "git status".'
)
_HOOKS_FIX = (
    "Set hooks_auto_accept: false in ~/.hermes/config.yaml so every new shell hook needs your "
    "approval on first use."
)
_SECRET_FIX = (
    'replace the value with "${env:<VAR>}" (Hermes expands ${VAR} and ${env:VAR} in any '
    "mcp_servers string), put <VAR>=... in ~/.hermes/.env, then run chmod 600 ~/.hermes/.env."
)


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    mode = dig(data, "approvals", "mode")
    if mode == "off" or mode is False:  # unquoted `off` is the YAML boolean false
        grants.append(Grant("approvals.mode", "off", _MODE_FIX))
    allowlist = data.get("command_allowlist")
    for entry in allowlist if isinstance(allowlist, list) else []:
        # Entries are exact commands or shell globs, so "*" or "bash *" approves anything.
        if isinstance(entry, str) and is_blanket_command(entry, prefix=False):
            grants.append(Grant("command_allowlist", entry.strip(), _ALLOWLIST_FIX))
    if data.get("hooks_auto_accept") is True:
        grants.append(Grant("hooks_auto_accept", "true", _HOOKS_FIX))
    return tuple(grants)


def _glob(base: Path, pattern: str) -> list[Path]:
    try:
        return sorted(base.glob(pattern))
    except OSError:
        return []


# History that is only a record (fix masks keys there): the message databases, gateway sessions
# and logs, in the main folder and in each profile. skills/, cron/ (scheduled jobs), memories/
# and SOUL.md are read back as instructions, so fix leaves keys there alone.
_RECORDS = (
    re.compile(r"(^|/)\.hermes/(profiles/[^/]+/)?(state|response_store)\.db$"),
    re.compile(r"(^|/)\.hermes/(profiles/[^/]+/)?(sessions|logs)/"),
)


class Hermes:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.hermes/"),
            Severity.HIGH,
            "Hermes Agent home: API keys (.env, auth.json), full chat history (state.db), "
            "memories.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".hermes"
        for root in (base, *_glob(base / "profiles", "*")):
            yield from artifacts(NAME, Kind.SETTINGS, root / "config.yaml")
            yield from artifacts(
                NAME,
                Kind.HISTORY,
                root / "state.db",  # SQLite: every message, tool call and result
                root / "response_store.db",
                root / "sessions",  # gateway sessions
                root / "memories",
                root / "cron",
                root / "logs",
                root / "SOUL.md",
            )
            yield from artifacts(
                NAME,
                Kind.CREDENTIALS,
                root / ".env",
                root / "auth.json",
                root / "webhook_subscriptions.json",
                root / "vault" / "vault.key",
            )
        yield from artifacts(NAME, Kind.HISTORY, base / "skills")  # agent-written skills

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        return iter(())

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name != "config.yaml":
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        return AgentConfig(
            grants=_grants(data),
            hooks=hook_commands(data.get("hooks")),
            mcp_servers=mcp_servers(data.get("mcp_servers"), _SECRET_FIX),
        )
