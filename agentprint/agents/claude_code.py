"""Claude Code (Anthropic). Layout verified on macOS on 2026-10-08; config keys on 2026-11-05.

Docs: https://code.claude.com/docs/en/settings, https://code.claude.com/docs/en/permissions
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    EnvReference,
    artifacts,
    children,
    dig,
    from_home,
    glob,
    hook_commands,
    is_blanket_command,
    matches_any,
    mcp_servers,
)

NAME = "claude-code"

_BASH_RULE = re.compile(r"Bash\((.*)\)", re.DOTALL)
_ALLOW_FIX = (
    "Delete this entry from permissions.allow and allow only the commands you need, e.g. "
    '"Bash(npm run test *)". (A bare "*" allow rule is ignored by Claude Code and is not flagged.)'
)
_MODE_FIX = (
    'Set permissions.defaultMode to "default" (or "acceptEdits"), or delete the key. For an '
    "unattended run, pass --permission-mode bypassPermissions inside a container or VM instead."
)
_MCP_ALL_FIX = (
    'Set "enableAllProjectMcpServers": false (or delete it) and approve servers by name in '
    '"enabledMcpjsonServers", so a cloned repo cannot start MCP servers without asking.'
)
_SECRET_FIX = (
    'replace the value with "${<VAR>}" (Claude Code expands environment variables in .mcp.json '
    "and ~/.claude.json), then export <VAR> in the shell that starts Claude Code."
)


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    allow = dig(data, "permissions", "allow")
    for rule in allow if isinstance(allow, list) else []:
        if not isinstance(rule, str):
            continue
        rule_text = rule.strip()
        match = _BASH_RULE.fullmatch(rule_text)
        if rule_text == "Bash" or (match and is_blanket_command(match.group(1), prefix=False)):
            grants.append(Grant("permissions.allow", rule_text, _ALLOW_FIX))
    if dig(data, "permissions", "defaultMode") == "bypassPermissions":
        grants.append(Grant("permissions.defaultMode", "bypassPermissions", _MODE_FIX))
    if data.get("enableAllProjectMcpServers") is True:
        grants.append(Grant("enableAllProjectMcpServers", "true", _MCP_ALL_FIX))
    return tuple(grants)


# The files that expand ${VAR}: `agentprint fix` moves an inline env key there to "${VAR}".
_ENV = EnvReference("mcpServers", "${{var}}", "the shell that starts Claude Code")
_ENV_FILES = {".mcp.json": _ENV, ".claude.json": replace(_ENV, projects=True)}


# History that is only a record (fix masks keys there): typed prompts, pasted text, backups of
# edited files, and the transcripts under projects/. projects/<slug>/memory/ is auto memory,
# read back into every session, so it is not a record.
_RECORDS = (
    re.compile(r"(^|/)\.claude/history\.jsonl$"),
    re.compile(r"(^|/)\.claude/(paste-cache|file-history)/"),
    re.compile(r"(^|/)\.claude/projects/[^/]+/(?!memory/)(?:[^/]+/)*[^/]+\.jsonl$"),
)


# What Claude Code loads from ~/.claude and a project's .claude/ as instructions or runs as hook
# scripts, one extension per entry (extension_paths).
_LOADED = ("skills", "commands", "agents", "rules", "hooks")


def _live(folder: Path) -> bool:
    """A cached plugin version still in use: an update or uninstall leaves `.orphaned_at` in
    the old version folder until a cleanup deletes it."""
    try:
        return folder.is_dir() and not (folder / ".orphaned_at").exists()
    except OSError:
        return False


class ClaudeCode:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.claude/settings\.local\.json$"),
            Severity.HIGH,
            "Personal permission allowlist; often holds pasted commands with keys.",
        ),
        PackageRule(
            NAME, re.compile(r"(^|/)\.claude/.+\.jsonl$"), Severity.HIGH, "Agent conversation log."
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.claude\.json$"),
            Severity.HIGH,
            "Global Claude Code state, including MCP server config.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)CLAUDE\.local\.md$"),
            Severity.MEDIUM,
            "Personal project notes meant to stay local.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".claude"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "settings.json",
            base / "settings.local.json",
            home / ".claude.json",
            base / "backups",
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            base / "history.jsonl",  # every prompt typed, plus pasted text
            base / "paste-cache",  # large pastes
            base / "projects",  # full transcripts, per project
            base / "file-history",  # backups of files Claude edited (can include .env)
        )
        # Login token on Linux and Windows (macOS uses the Keychain): permission check only.
        yield from artifacts(NAME, Kind.CREDENTIALS, base / ".credentials.json")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(NAME, Kind.SETTINGS, root / ".claude" / "settings.json")
        yield from artifacts(NAME, Kind.SETTINGS, root / ".mcp.json")
        # Personal by design: Claude Code keeps these out of git when it creates them itself.
        yield from artifacts(
            NAME, Kind.SETTINGS, root / ".claude" / "settings.local.json", personal=True
        )
        yield from artifacts(NAME, Kind.PERSONAL, root / "CLAUDE.local.md", personal=True)

    def extension_paths(self, home: Path | None, project: Path | None) -> list[Path]:
        # CLAUDE.md and .claude/rules/ (https://code.claude.com/docs/en/memory), skills/,
        # commands/, agents/ (.../skills, .../slash-commands, .../sub-agents), and each plugin
        # version Claude Code loads from ~/.claude/plugins/cache/<marketplace>/<plugin>/
        # <version>/ (.../plugins/loading.md; a version marked .orphaned_at is no longer
        # used). .claude/hooks/ is where the hooks docs keep the scripts a hook runs.
        paths: list[Path] = []
        if home is not None:
            base = home / ".claude"
            paths += [base / "CLAUDE.md", *children(*(base / name for name in _LOADED))]
            paths += [v for v in glob(base / "plugins" / "cache", "*/*/*") if _live(v)]
        if project is not None:
            base = project / ".claude"
            paths += [project / "CLAUDE.md", base / "CLAUDE.md"]
            paths += children(*(base / name for name in _LOADED))
        return paths

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def env_reference(self, path: Path) -> EnvReference | None:
        return _ENV_FILES.get(path.name)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("settings.json", "settings.local.json", ".claude.json", ".mcp.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name in ("settings.json", "settings.local.json"):
            return AgentConfig(grants=_grants(data), hooks=hook_commands(data.get("hooks")))
        servers = list(mcp_servers(data.get("mcpServers"), _SECRET_FIX))
        projects = data.get("projects") if name == ".claude.json" else None
        if isinstance(projects, dict):  # per-project ("local" scope) servers
            for project in projects.values():
                servers += mcp_servers(dig(project, "mcpServers"), _SECRET_FIX)
        return AgentConfig(mcp_servers=tuple(servers))
