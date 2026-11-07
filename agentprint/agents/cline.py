"""Cline (VS Code extension v4 and Cline CLI v3, one shared data folder).

Not installed on the verification Mac: every name comes from the docs and the official source,
read 2026-11-05. ~/.cline/data/ holds secrets.json (plain-text keys, mode 0600),
settings/providers.json (provider keys), settings/global-settings.json, globalState.json,
settings/cline_mcp_settings.json (mcpServers.<name>.{command, args, env, url, headers}),
sessions/, db/sessions.db (SQLite), logs/, and the legacy tasks/ and state/taskHistory.json.
The extension keeps its tasks in VS Code's globalStorage/saoudrizwan.claude-dev/ (tasks/<id>/
api_conversation_history.json, ui_messages.json, ...; state/taskHistory.json), and older
versions an MCP file in its settings/ folder and in ~/Documents/Cline/MCP/.
Docs: https://docs.cline.bot/getting-started/config.md, https://docs.cline.bot/mcp/mcp-overview.md,
github.com/cline/cline apps/vscode/src/shared/storage/storage-context.ts,
apps/vscode/src/hosts/vscode/vscode-to-file-migration.ts, apps/vscode/src/core/storage/disk.ts.
Per-OS globalStorage paths follow VS Code's usual layout (not confirmed in Cline's docs), and so
does ~/.cline on Windows (%USERPROFILE%\\.cline).

secrets.json and providers.json are Cline's own key stores: never secret-scanned, only checked
for who can read them (AP008). Hooks are executable files named after the event (PreToolUse),
not config, so AP004 does not read them. Rules (.clinerules/, ~/Documents/Cline/Rules) are
instructions, not leaks.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from .base import AgentDoc, app_dirs, artifacts, from_home, matches_any, read_mcp_file

NAME = "cline"
EXTENSION = "saoudrizwan.claude-dev"
_MCP_FILE = "cline_mcp_settings.json"
_SECRET_FIX = (
    "put the new key only in this file, never in a copy you share, and make it private with "
    "chmod 600 on it: Cline documents no environment-variable reference for "
    "cline_mcp_settings.json, so <VAR> cannot be read from your shell."
)

# History that is only a record (fix masks keys there): sessions, the session database, logs,
# and each task's conversation files and the task list, in the CLI folder and the extension's.
_RECORDS = (
    re.compile(r"(^|/)\.cline/data/(sessions|logs|tasks)/"),
    re.compile(r"(^|/)\.cline/data/(db/sessions\.db|state/taskHistory\.json)$"),
    re.compile(r"(^|/)saoudrizwan\.claude-dev/(tasks/|state/taskHistory\.json$)"),
)

DOC = AgentDoc(
    label="Cline",
    reads=(
        "`~/.cline/data/` sessions, `db/sessions.db` (SQLite), logs, legacy tasks; "
        "`settings/cline_mcp_settings.json` (MCP), `global-settings.json`, `globalState.json`; "
        "the extension's tasks in VS Code `globalStorage/saoudrizwan.claude-dev/`"
    ),
    keys="`secrets.json` (plain text), `settings/providers.json`",
    fix="masks keys in sessions, logs and task files; MCP keys: needs you (no env reference)",
    checked="docs and source, 2026-11-05",
    unverified="the extension folder on each OS (VS Code's usual layout); `~/.cline` on Windows",
)


class Cline:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(
                r"(^|/)\.cline/data/|(^|/)saoudrizwan\.claude-dev/(tasks|state|checkpoints)/"
            ),
            Severity.HIGH,
            "Cline data: secrets.json (plain-text keys), sessions and task history.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        data = home / ".cline" / "data"
        extensions = [user / "globalStorage" / EXTENSION for user in app_dirs(home, "Code", "User")]
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            data / "settings" / _MCP_FILE,
            data / "settings" / "global-settings.json",
            data / "globalState.json",
            *(ext / "settings" / _MCP_FILE for ext in extensions),  # before the move to ~/.cline
            home / "Documents" / "Cline" / "MCP" / _MCP_FILE,
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            data / "sessions",
            data / "db" / "sessions.db",  # SQLite
            data / "logs",
            data / "tasks",  # legacy CLI tasks
            data / "state" / "taskHistory.json",
            *(ext / "tasks" for ext in extensions),
            *(ext / "state" / "taskHistory.json" for ext in extensions),
        )
        yield from artifacts(
            NAME, Kind.CREDENTIALS, data / "secrets.json", data / "settings" / "providers.json"
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        return iter(())

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name != _MCP_FILE:
            return AgentConfig()
        return read_mcp_file(artifact.path, _SECRET_FIX, "mcpServers")
