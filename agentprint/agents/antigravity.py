"""Google Antigravity: the desktop app, the `agy` CLI and the IDE (Gemini CLI's successor for
individual users since 2026-06-18).

Layout verified on macOS on 2026-11-05 (names only): ~/.gemini/antigravity/{brain/<id>/
(task.md, implementation_plan.md, browser/, .system_generated/), conversations/*.pb, annotations/,
knowledge/, mcp_config.json (empty)}. ~/.gemini/config/ and ~/.gemini/antigravity-cli/ are not
present yet on the verification Mac; their names come from the docs:
~/.gemini/config/hooks.json ({<hook set>: {<event>: [{matcher, hooks: [{type, command}]}]}}),
~/.gemini/config/mcp_config.json (mcpServers.<name>.{command,args,env,serverUrl,headers}),
<app_data_dir>/brain/<conversation>/.system_generated/logs/transcript.jsonl for app_data_dir
~/.gemini/antigravity, antigravity-cli, antigravity-ide; ~/.gemini/antigravity-cli/settings.json
(toolPermission, permissions.allow); ~/.gemini/antigravity/mcp_oauth_tokens.json (plain file).
Workspace files live in .agents/.
Docs: https://antigravity.google/docs/hooks/, https://antigravity.google/docs/mcp,
https://antigravity.google/docs/permissions/, https://antigravity.google/docs/settings/.

Gemini CLI keeps ~/.gemini/settings.json and ~/.gemini/tmp/ (see gemini.py); nothing here
overlaps. Not covered: conversations/*.pb (binary protobuf). Global instruction files
(~/.gemini/AGENTS.md, GEMINI.md) are not scanned, as for every other agent.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import AgentConfig, Artifact, Grant, HookCommand, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    artifacts,
    children,
    dig,
    from_home,
    gemini_folder,
    hook_commands,
    is_blanket_command,
    matches_any,
    mcp_servers,
)

NAME = "antigravity"

_APP_DIRS = ("antigravity", "antigravity-cli", "antigravity-ide")
_COMMAND_RULE = re.compile(r"command\((.*)\)", re.DOTALL)
_REGEX_ALL = re.compile(r"regex:\^?\.[*+]\$?")
_TOOL_FIX = (
    'Set "toolPermission": "request-review" in ~/.gemini/antigravity-cli/settings.json; use '
    "--dangerously-skip-permissions only for a one-off run inside a container or VM."
)
_ALLOW_FIX = (
    "Delete this entry from permissions.allow in ~/.gemini/antigravity-cli/settings.json and "
    'allow exact command prefixes instead, e.g. "command(git status)".'
)
_SECRET_FIX = (
    "keep this server only in ~/.gemini/config/mcp_config.json (never in a workspace "
    ".agents/mcp_config.json), make that file private with chmod 600 "
    "~/.gemini/config/mcp_config.json, and put the new key there: Antigravity documents no "
    "environment-variable expansion for mcp_config.json, so <VAR> cannot be read from your shell."
)


def _grants(data: dict[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    if data.get("toolPermission") == "always-proceed":
        grants.append(Grant("toolPermission", "always-proceed", _TOOL_FIX))
    allow = dig(data, "permissions", "allow")
    for rule in allow if isinstance(allow, list) else []:
        if not isinstance(rule, str):
            continue
        rule_text = rule.strip()
        command = _COMMAND_RULE.fullmatch(rule_text)
        # command(x) matches command prefixes word by word, so command(bash) runs anything.
        if command and (
            _REGEX_ALL.fullmatch(command.group(1).strip())
            or is_blanket_command(command.group(1), prefix=True)
        ):
            grants.append(Grant("permissions.allow", rule_text, _ALLOW_FIX))
    return tuple(grants)


def _hooks(data: dict[str, Any]) -> tuple[HookCommand, ...]:
    """hooks.json is keyed by hook set, then by event; accept bare events too. A set with
    "enabled": false never runs, so its commands are skipped."""
    found = list(hook_commands(data))
    for value in data.values():
        if isinstance(value, dict) and value.get("enabled") is not False:
            found += hook_commands(value)
    return tuple(found)


# History that is only a record (fix masks keys there): the transcripts each conversation keeps
# under brain/<id>/.system_generated/logs/. The rest of brain/ (the task lists and plans the
# agent writes and re-reads) and knowledge/ (saved knowledge it reads back) are not.
_RECORDS = (
    re.compile(r"(^|/)\.gemini/antigravity(-cli|-ide)?/brain/[^/]+/\.system_generated/logs/"),
)


# A workspace's .agents/ folders of Antigravity extensions, one per entry (extension_paths).
_LOADED = ("rules", "workflows", "skills", "plugins")


class Antigravity:
    name = NAME
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.gemini/antigravity(-cli|-ide)?/"),
            Severity.HIGH,
            "Google Antigravity data: conversation transcripts, artifacts and MCP OAuth tokens.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.gemini/config/(mcp_config|hooks)\.json$"),
            Severity.MEDIUM,
            "Personal Antigravity MCP or hook config; may hold server tokens.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.agents/mcp_config\.json$"),
            Severity.LOW,
            "Workspace MCP config; rarely meant for a package (keys inside are reported "
            "separately).",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        gemini = gemini_folder(home)  # honours GEMINI_CLI_HOME
        apps = [gemini / name for name in _APP_DIRS]
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            gemini / "config" / "hooks.json",
            gemini / "config" / "mcp_config.json",
            gemini / "antigravity" / "mcp_config.json",  # older app versions
            gemini / "antigravity-cli" / "settings.json",
        )
        # brain/: transcripts and the artifacts the agent writes; knowledge/: saved knowledge.
        yield from artifacts(
            NAME, Kind.HISTORY, *(app / name for app in apps for name in ("brain", "knowledge"))
        )
        yield from artifacts(
            NAME, Kind.CREDENTIALS, *(app / "mcp_oauth_tokens.json" for app in apps)
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        agents = root / ".agents"
        yield from artifacts(NAME, Kind.SETTINGS, agents / "hooks.json", agents / "mcp_config.json")

    def extension_paths(self, home: Path | None, project: Path | None) -> list[Path]:
        # Global rules (~/.gemini/config/rules/), plugins (~/.gemini/config/plugins/<name>/),
        # and a workspace's .agents/rules/, .agents/workflows/, .agents/skills/ and plugin
        # folders in .agents/plugins/. https://antigravity.google/docs/rules/,
        # https://antigravity.google/docs/plugins
        paths: list[Path] = []
        if home is not None:
            config = gemini_folder(home) / "config"
            paths += children(config / "rules", config / "plugins")
        if project is not None:
            paths += children(*(project / ".agents" / name for name in _LOADED))
        return paths

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name not in ("hooks.json", "mcp_config.json", "settings.json"):
            return AgentConfig()
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        if name == "hooks.json":
            return AgentConfig(hooks=_hooks(data))
        if name == "mcp_config.json":
            return AgentConfig(mcp_servers=mcp_servers(data.get("mcpServers"), _SECRET_FIX))
        return AgentConfig(grants=_grants(data))
