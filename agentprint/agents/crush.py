"""Crush (Charm).

Not installed on the verification Mac. Paths from the README, the config docs and the source,
read 2026-11-05. Transcripts live in the project: .crush/crush.db (SQLite) and .crush/logs/; Crush
writes .crush/.gitignore (`*`) itself. Keys pasted into Crush are saved as
providers.<id>.api_key in the global data file ~/.local/share/crush/crush.json
(%LOCALAPPDATA%\\crush\\crush.json on Windows) or the project's .crush/crush.json, mode 0600.
Config: ~/.config/crush/crushrc (shell-like; may hold a literal --api-key) and the deprecated JSON
crush.json / .crush.json (mcp.<name>.{command, args, env, url, headers}, hooks), in the same
folder or at the project root.
Docs: https://github.com/charmbracelet/crush (README), docs/config/README.md,
internal/config/store.go, internal/db/connect.go.

The data files are Crush's own key stores: never secret-scanned, only checked for who can read
them. Not covered: CRUSH_GLOBAL_CONFIG / CRUSH_GLOBAL_DATA, which move them; crushrc `mcp add`
lines (read only for keys, AP002).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from ..structured import load
from .base import (
    AgentDoc,
    EnvReference,
    artifacts,
    children,
    from_home,
    hook_commands,
    matches_any,
    read_mcp_servers,
)

NAME = "crush"
_JSON_NAMES = ("crush.json", ".crush.json")
_RC_NAMES = ("crushrc", ".crushrc")
_SECRET_FIX = (
    'replace the value with "$<VAR>" (Crush expands $NAME in its config files), then export '
    "<VAR> in the shell that starts Crush."
)
ENV = EnvReference("mcp", "${var}", "the shell that starts Crush")  # writes "$NAME"

# History that is only a record (fix leaves the database to the human; logs are masked).
_RECORDS = (re.compile(r"(^|/)\.crush/(crush\.db$|logs/)"),)

DOC = AgentDoc(
    label="Crush",
    reads=(
        "project `.crush/crush.db` (SQLite) and `.crush/logs/`; `~/.config/crush/crushrc` and "
        "`crush.json`, project `crushrc` and `crush.json` (MCP, hooks)"
    ),
    keys=(
        "`~/.local/share/crush/crush.json` (pasted keys; `%LOCALAPPDATA%\\crush\\` on Windows); "
        "project `.crush/crush.json`"
    ),
    fix=(
        'masks keys in logs; MCP `env` keys in `crush.json` become `"$NAME"` (headers: needs '
        "you); keys in the database and in `crushrc`: needs you"
    ),
    checked="README, config docs and source, 2026-11-05",
)


class Crush:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.crush/"),
            Severity.HIGH,
            "Crush data folder: crush.db transcripts, logs, overrides that can hold keys.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.local/share/crush/"),
            Severity.HIGH,
            "Crush global data: provider API keys.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        config = home / ".config" / "crush"
        yield from artifacts(
            NAME, Kind.SETTINGS, *(config / name for name in (*_RC_NAMES, *_JSON_NAMES))
        )
        yield from artifacts(
            NAME,
            Kind.CREDENTIALS,
            home / ".local" / "share" / "crush" / "crush.json",
            home / "AppData" / "Local" / "crush" / "crush.json",
        )

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        data = root / ".crush"
        yield from artifacts(NAME, Kind.HISTORY, data / "crush.db", personal=True)
        yield from artifacts(NAME, Kind.HISTORY, data / "logs", personal=True)
        yield from artifacts(NAME, Kind.CREDENTIALS, data / "crush.json", personal=True)
        yield from artifacts(
            NAME, Kind.SETTINGS, *(root / name for name in (*_RC_NAMES, *_JSON_NAMES))
        )

    def extension_paths(self, home: Path | None, project: Path | None) -> list[Path]:
        # Global instructions ~/.config/crush/CRUSH.md and ~/.config/AGENTS.md, and skills in
        # ~/.config/crush/skills/, ~/.config/agents/skills/, the agents and crush skills folders in
        # %LOCALAPPDATA% and a project's .crush/skills/ and .cursor/skills/ (the README, read
        # 2026-11-07; .agents/skills/ and .claude/skills/ are read through Codex and Claude Code).
        paths: list[Path] = []
        if home is not None:
            config, local = home / ".config", home / "AppData" / "Local"
            paths += [config / "crush" / "CRUSH.md", config / "AGENTS.md"]
            paths += children(config / "crush" / "skills", config / "agents" / "skills")
            paths += children(local / "agents" / "skills", local / "crush" / "skills")
        if project is not None:
            paths += children(project / ".crush" / "skills", project / ".cursor" / "skills")
        return paths

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def env_reference(self, path: Path) -> EnvReference | None:
        return ENV if path.name in _JSON_NAMES else None

    def read_config(self, artifact: Artifact) -> AgentConfig:
        if artifact.path.name not in _JSON_NAMES:
            return AgentConfig()  # crushrc is read line by line for keys (AP002) only
        data = load(artifact.path)
        if not isinstance(data, dict):
            return AgentConfig()
        return AgentConfig(
            hooks=hook_commands(data.get("hooks")),
            mcp_servers=read_mcp_servers(data, _SECRET_FIX, "mcp"),
        )
