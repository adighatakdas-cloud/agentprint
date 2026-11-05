"""OpenAI Codex (CLI and app). Layout verified on macOS on 2026-10-08; config keys on 2026-11-05.

Docs: https://learn.chatgpt.com/docs/config-file/config-reference (approval_policy,
sandbox_mode, mcp_servers.*, hooks, notify), https://developers.openai.com/codex/hooks
(~/.codex/hooks.json and <repo>/.codex/hooks.json).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, HookCommand, Kind, PackageRule, Severity
from ..structured import load
from .base import artifacts, hook_commands, mcp_servers

NAME = "codex"

_FULL_ACCESS = {"approval_policy": "never", "sandbox_mode": "danger-full-access"}
_FULL_ACCESS_FIX = (
    'Set approval_policy = "on-request" and sandbox_mode = "workspace-write" in this table '
    "(or delete both lines). For a one-off unattended run, pass "
    "--dangerously-bypass-approvals-and-sandbox inside a container or VM instead."
)
_SECRET_FIX = (
    "delete the value from config.toml and read it from the environment: for a stdio server "
    'list env_vars = ["<VAR>"]; for an HTTP server use bearer_token_env_var = "<VAR>" or '
    'env_http_headers = { "<header>" = "<VAR>" }. Then export <VAR> in the shell that starts Codex.'
)


def _full_access(table: dict[str, Any]) -> bool:
    return all(table.get(key) == value for key, value in _FULL_ACCESS.items())


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    value = "never + danger-full-access"
    if _full_access(data):
        return (Grant("approval_policy + sandbox_mode", value, _FULL_ACCESS_FIX),)
    grants: list[Grant] = []
    profiles = data.get("profiles")
    for name, profile in profiles.items() if isinstance(profiles, dict) else []:
        if isinstance(profile, dict):
            effective = {key: profile.get(key, data.get(key)) for key in _FULL_ACCESS}
            if _full_access(effective):
                grants.append(Grant(f"profiles.{name}", value, _FULL_ACCESS_FIX))
    return tuple(grants)


def _notify(data: dict[str, Any]) -> tuple[HookCommand, ...]:
    notify = data.get("notify")
    if isinstance(notify, list) and notify:
        return (HookCommand("notify", " ".join(str(part) for part in notify)),)
    return ()


class Codex:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codex/(sessions|archived_sessions|attachments)/"),
            Severity.HIGH,
            "Codex session logs or pasted attachments.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codex/(history\.jsonl|auth\.json)$"),
            Severity.HIGH,
            "Codex prompt history or login token.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.codex/config\.toml$"),
            Severity.MEDIUM,
            "Personal Codex config; may hold MCP server tokens.",
        ),
    )

    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".codex"
        # auth.json is Codex's own login store: a token there is expected, so it is never
        # secret-scanned, only checked for who can read it (AP008).
        yield from artifacts(NAME, Kind.SETTINGS, base / "config.toml", base / "hooks.json")
        yield from artifacts(NAME, Kind.CREDENTIALS, base / "auth.json")
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            base / "history.jsonl",
            base / "sessions",
            base / "archived_sessions",
            base / "attachments",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".codex"
        yield from artifacts(NAME, Kind.SETTINGS, base / "config.toml", base / "hooks.json")

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("config.toml", "hooks.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name == "hooks.json":
            return AgentConfig(hooks=hook_commands(data.get("hooks")))
        return AgentConfig(
            grants=_grants(data),
            hooks=hook_commands(data.get("hooks")) + _notify(data),
            mcp_servers=mcp_servers(data.get("mcp_servers"), _SECRET_FIX),
        )
