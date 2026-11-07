"""Kilo Code (the VS Code extension and the Kilo CLI share one core, derived from OpenCode).

Not installed on the verification Mac. Paths from the docs, read 2026-11-05: the session database
~/.local/share/kilo/kilo.db (SQLite; the same folder under %USERPROFILE% on Windows) next to
auth.json (provider keys, mode 0600); global config ~/.config/kilo/kilo.jsonc or kilo.json (also
config.json, opencode.json[c] there), with MCP servers under "mcp"
(local: command[], environment; remote: url, headers); project config kilo.json[c] or
.kilo/kilo.json[c] at the repo root. Before April 2026 the extension was a Cline fork and kept
settings/cline_mcp_settings.json in VS Code's globalStorage/kilocode.kilo-code/.
Docs: https://kilo.ai/docs/code-with-ai/agents/session-history,
https://kilo.ai/docs/automate/mcp/using-in-kilo-code, https://kilo.ai/docs/contributing/architecture/
cli-runtime (auth.json under the data folder; its exact path is inferred from kilo.db's).

auth.json is Kilo's own key store: never secret-scanned, only checked for who can read it.
Not covered: plugins (JavaScript), permission rules (AP003), KILO_DB / KILO_CONFIG_DIR /
XDG_DATA_HOME, which move the folders.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from .base import AgentDoc, app_dirs, artifacts, from_home, matches_any, read_mcp_file

NAME = "kilo"
MCP_WHY = "MCP server config; rarely meant for a package (keys inside are reported separately)."
_CONFIG_NAMES = ("kilo.jsonc", "kilo.json", "config.json", "opencode.json", "opencode.jsonc")
_PROJECT_NAMES = ("kilo.jsonc", "kilo.json")
_LEGACY_MCP = ("cline_mcp_settings.json", "mcp.json")
_SECRET_FIX = (
    "put the new key only in your own ~/.config/kilo/ config (never in a project's kilo.jsonc) "
    "and make that file private with chmod 600: Kilo's MCP docs name no environment-variable "
    "reference, so <VAR> cannot be read from your shell."
)

# History that is only a record (fix leaves SQLite to the human; this is for completeness).
_RECORDS = (re.compile(r"(^|/)\.local/share/kilo/kilo\.db$"),)

DOC = AgentDoc(
    label="Kilo Code",
    reads=(
        "session database `~/.local/share/kilo/kilo.db` (SQLite); `~/.config/kilo/kilo.json(c)` "
        "and project `kilo.json(c)`, `.kilo/kilo.json(c)` (MCP); the old extension's MCP file"
    ),
    keys="`~/.local/share/kilo/auth.json`",
    fix="keys in the database: needs you (agentprint never edits a database); MCP keys: needs you",
    checked="docs, 2026-11-05",
    unverified=(
        "`auth.json` (inferred from `kilo.db`); the config folder on Windows; the old extension "
        "folder on each OS"
    ),
)


class Kilo:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME,
            re.compile(
                r"(^|/)\.local/share/kilo/"
            ),  # kilo.db, auth.json; not any file named kilo.db
            Severity.HIGH,
            "Kilo Code session database, snapshots or auth.json.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.kilo/kilo\.jsonc?$|(^|/)\.kilocode/mcp\.json$"),
            Severity.LOW,
            MCP_WHY,
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        config = home / ".config" / "kilo"
        data = home / ".local" / "share" / "kilo"
        legacy = [
            user / "globalStorage" / "kilocode.kilo-code" / "settings"
            for user in app_dirs(home, "Code", "User")
        ]
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            *(config / name for name in _CONFIG_NAMES),
            *(folder / "cline_mcp_settings.json" for folder in legacy),
        )
        yield from artifacts(NAME, Kind.HISTORY, data / "kilo.db")  # SQLite: every session
        yield from artifacts(NAME, Kind.CREDENTIALS, data / "auth.json")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            *(root / name for name in _PROJECT_NAMES),
            *(root / ".kilo" / name for name in _PROJECT_NAMES),
            root / ".kilocode" / "mcp.json",
        )

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        name = artifact.path.name
        if name in _LEGACY_MCP:
            return read_mcp_file(artifact.path, _SECRET_FIX, "mcpServers")
        if name in _CONFIG_NAMES:
            # One page of the docs still wraps servers in "mcpServers"; read both.
            return read_mcp_file(artifact.path, _SECRET_FIX, "mcp", "mcpServers")
        return AgentConfig()
