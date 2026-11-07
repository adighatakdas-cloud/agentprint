"""Goose (AAIF / Linux Foundation, formerly Block): the CLI and the desktop app.

Not installed on the verification Mac. Paths from the docs, read 2026-11-05:
macOS and Linux: ~/.config/goose/{config.yaml, secrets.yaml, history.txt}, sessions in
~/.local/share/goose/sessions/ (sessions.db, SQLite; *.jsonl before v1.10), logs in
~/.local/state/goose/logs/ (llm_request.*.jsonl: raw LLM requests and replies), the desktop log
in ~/Library/Application Support/Goose/logs/. Windows: %APPDATA%\\Block\\goose\\config\\,
%APPDATA%\\Block\\goose\\data\\{sessions, logs, history.txt} and the desktop log in
%APPDATA%\\Block\\goose\\logs\\.
MCP servers are config.yaml `extensions:` (cmd, args, envs; remote: uri, headers). Hooks are
Claude-style hooks.json files in ~/.agents/plugins/<name>/hooks/.
Docs: https://goose-docs.ai/docs/guides/config-files, https://goose-docs.ai/docs/guides/logs,
https://goose-docs.ai/docs/guides/context-engineering/hooks.

secrets.yaml is Goose's own key store (used when no system keyring is reachable): never
secret-scanned, only checked for who can read it. Not covered: GOOSE_PATH_ROOT, which moves
everything; permission.yaml and GOOSE_MODE (AP003).
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
    glob,
    hook_commands,
    matches_any,
    read_mcp_servers,
)

NAME = "goose"
_SECRET_FIX = (
    'move the key out of envs: list "<VAR>" under the extension\'s env_keys instead, and Goose '
    "reads the value from its secret store (the system keyring, or secrets.yaml where no "
    "keyring is reachable)."
)

# History that is only a record (fix masks keys there): sessions (the database and the old
# JSONL files), logs (raw LLM requests included) and the typed-command history.
_RECORDS = (
    re.compile(r"(^|/)goose/(data/)?(sessions|logs)/"),
    re.compile(r"(^|/)goose/(data/)?history\.txt$"),
    re.compile(r"(^|/)Goose/logs/"),
)

DOC = AgentDoc(
    label="Goose",
    reads=(
        "sessions (`sessions.db`, SQLite, and older JSONL), logs including raw LLM requests, "
        "`history.txt`; `config.yaml` (MCP extensions); hooks in `~/.agents/plugins/*/hooks/`; "
        "on Windows the same under `%APPDATA%\\Block\\goose\\`"
    ),
    keys="`secrets.yaml`",
    fix="masks keys in logs, old sessions and history; MCP keys: needs you (use `env_keys`)",
    checked="docs, 2026-11-05",
)


class Goose:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(
                r"(^|/)\.config/goose/secrets\.yaml$|(^|/)\.local/share/goose/sessions/"
                r"|(^|/)\.local/state/goose/logs/"
            ),
            Severity.HIGH,
            "Goose plain-text secrets, sessions or raw LLM request logs.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        config = home / ".config" / "goose"
        windows = home / "AppData" / "Roaming" / "Block" / "goose"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            config / "config.yaml",
            windows / "config" / "config.yaml",
            *glob(home / ".agents" / "plugins", "*/hooks/hooks.json"),
        )
        yield from artifacts(
            NAME,
            Kind.HISTORY,
            home / ".local" / "share" / "goose" / "sessions",
            home / ".local" / "state" / "goose" / "logs",
            config / "history.txt",
            home / "Library" / "Application Support" / "Goose" / "logs",
            windows / "data" / "sessions",
            windows / "data" / "logs",
            windows / "data" / "history.txt",
            windows / "logs",
        )
        yield from artifacts(
            NAME, Kind.CREDENTIALS, config / "secrets.yaml", windows / "config" / "secrets.yaml"
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        return iter(())

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        path = artifact.path
        if path.name == "hooks.json":
            data = load(path)
            return AgentConfig(
                hooks=hook_commands(data.get("hooks")) if isinstance(data, dict) else ()
            )
        if path.name != "config.yaml":
            return AgentConfig()
        return AgentConfig(mcp_servers=read_mcp_servers(load(path), _SECRET_FIX, "extensions"))
