"""Qwen Code (QwenLM, a Gemini CLI fork).

Not installed on the verification Mac. Paths from the docs and the official source, read
2026-11-05: ~/.qwen (%USERPROFILE%\\.qwen on Windows) holds chats as
projects/<project>/chats/<session>.jsonl (older docs: tmp/<hash>/chats/), tmp/<hash>/shell_history,
file-history/ (backups for rewind), projects/<project>/memory/ (auto memory, read back),
settings.json (mcpServers, Claude-style hooks, and an `env` block the docs say is stored in plain
text), and the key stores oauth_creds.json, mcp-oauth-tokens.json and .env. In a project:
.qwen/settings.json (shared), .qwen/.env and .qwen/QWEN.local.md (personal).
Docs: github.com/QwenLM/qwen-code docs/users/configuration/settings.md, docs/users/features/
{mcp,hooks,memory}.md, packages/core/src/config/storage.ts.

Not covered: QWEN_HOME, which moves the folder; permissions and approval mode (AP003).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    AgentDoc,
    artifacts,
    from_home,
    hook_commands,
    matches_any,
    read_mcp_servers,
)

NAME = "qwen-code"
MCP_WHY = "MCP server config; rarely meant for a package (keys inside are reported separately)."
_SECRET_FIX = (
    'replace the value with "${<VAR>}" (Qwen Code expands ${NAME} in an MCP server\'s env), '
    "then export <VAR> in the shell that starts Qwen Code."
)

# History that is only a record (fix masks keys there): chats, everything under tmp/ (chats,
# shell history), and edit backups. projects/<project>/memory/ is read back, so it is not.
_RECORDS = (
    re.compile(r"(^|/)\.qwen/projects/[^/]+/chats/"),
    re.compile(r"(^|/)\.qwen/(tmp|file-history)/"),
)

DOC = AgentDoc(
    label="Qwen Code",
    reads=(
        "`~/.qwen/` chats (JSONL), shell history, edit backups, auto memory; `settings.json` "
        "(MCP, hooks, `env` block) and project `.qwen/settings.json`"
    ),
    keys="`oauth_creds.json`, `mcp-oauth-tokens.json`, `.env`; project `.qwen/.env`",
    fix="masks keys in chats and history; MCP keys: needs you",
    checked="docs and source, 2026-11-05",
)


class QwenCode:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME, re.compile(r"(^|/)\.qwen/\.env$"), Severity.HIGH, "Qwen Code API-key env file."
        ),
        PackageRule(
            NAME,
            re.compile(
                r"(^|/)\.qwen/(projects|tmp|file-history)/"
                r"|(^|/)\.qwen/(oauth_creds|mcp-oauth-tokens)\.json$"
            ),
            Severity.HIGH,
            "Qwen Code chats, shell history, edit backups or login tokens.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.qwen/QWEN\.local\.md$"),
            Severity.MEDIUM,
            "Personal Qwen Code notes meant to stay local.",
        ),
        PackageRule(NAME, re.compile(r"(^|/)\.qwen/settings\.json$"), Severity.LOW, MCP_WHY),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".qwen"
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.json")
        yield from artifacts(
            NAME, Kind.HISTORY, base / "projects", base / "tmp", base / "file-history"
        )
        yield from artifacts(
            NAME,
            Kind.CREDENTIALS,
            base / "oauth_creds.json",
            base / "mcp-oauth-tokens.json",
            base / ".env",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".qwen"
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.json")
        yield from artifacts(NAME, Kind.CREDENTIALS, base / ".env", personal=True)
        yield from artifacts(NAME, Kind.PERSONAL, base / "QWEN.local.md", personal=True)

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name != "settings.json":
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        return AgentConfig(
            hooks=hook_commands(data.get("hooks")),
            mcp_servers=read_mcp_servers(data, _SECRET_FIX, "mcpServers"),
        )
