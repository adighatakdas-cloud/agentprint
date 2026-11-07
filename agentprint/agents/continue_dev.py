"""Continue (the VS Code / JetBrains extension and the `cn` CLI).

Not installed on the verification Mac. Paths from the docs and the official source, read
2026-11-05: everything is in ~/.continue (%USERPROFILE%\\.continue on Windows): sessions/*.json
(and the sessions.json index), logs/prompt.log (full prompts) and core.log, dev_data/ (JSONL
events and devdata.sqlite), config.yaml (mcpServers: a list of {name, command, args, env};
models may carry a raw apiKey), the deprecated config.json, mcpServers/*.yaml|json, .env
(secrets read through ${{ secrets.NAME }}) and auth.json (cn login). In a project:
.continue/mcpServers/, .continue/.env and the personal .continue/settings.local.json.
Docs: https://docs.continue.dev/faqs, https://docs.continue.dev/customize/deep-dives/mcp,
github.com/continuedev/continue core/util/paths.ts, extensions/cli/src/auth/authEnv.ts.

.env and auth.json are Continue's own key stores: never secret-scanned, only checked for who can
read them. Not covered: CONTINUE_GLOBAL_DIR, which moves the folder; the code index (index/);
cn hooks (the code exists but was not seen firing).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from .base import AgentDoc, artifacts, from_home, matches_any, read_mcp_file

NAME = "continue"
_SECRET_FIX = (
    'replace the value with "${{ secrets.<VAR> }}" (Continue reads secrets from .env files), '
    "put <VAR>=... in ~/.continue/.env, then run chmod 600 ~/.continue/.env."
)

# History that is only a record (fix masks keys there): sessions, logs, usage events.
_RECORDS = (re.compile(r"(^|/)\.continue/(sessions|logs|dev_data)/"),)

DOC = AgentDoc(
    label="Continue",
    reads=(
        "`~/.continue/` sessions, `logs/prompt.log` (full prompts), `dev_data/`; `config.yaml`, "
        "`config.json` and `mcpServers/` (MCP, raw `apiKey`); project `.continue/mcpServers/`"
    ),
    keys="`~/.continue/.env`, `auth.json`; project `.continue/.env`",
    fix="masks keys in sessions and logs; MCP keys: needs you (use `${{ secrets.NAME }}`)",
    checked="docs and source, 2026-11-05",
)


class Continue:
    name = NAME
    doc = DOC
    package_rules = (
        PackageRule(
            NAME, re.compile(r"(^|/)\.continue/\.env$"), Severity.HIGH, "Continue secrets env file."
        ),
        PackageRule(
            NAME,
            re.compile(
                r"(^|/)\.continue/(sessions|logs|dev_data|index)/|(^|/)\.continue/auth\.json$"
            ),
            Severity.HIGH,
            "Continue sessions, full prompt logs or login.",
        ),
        PackageRule(
            NAME,
            re.compile(r"(^|/)\.continue/settings\.local\.json$"),
            Severity.MEDIUM,
            "Personal Continue settings.",
        ),
    )

    @from_home
    def discover_home(self, home: Path) -> Iterator[Artifact]:
        base = home / ".continue"
        yield from artifacts(
            NAME,
            Kind.SETTINGS,
            base / "config.yaml",
            base / "config.json",
            base / "mcpServers",
        )
        yield from artifacts(
            NAME, Kind.HISTORY, base / "sessions", base / "logs", base / "dev_data"
        )
        yield from artifacts(NAME, Kind.CREDENTIALS, base / ".env", base / "auth.json")

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        base = root / ".continue"
        yield from artifacts(NAME, Kind.SETTINGS, base / "mcpServers")
        yield from artifacts(NAME, Kind.CREDENTIALS, base / ".env", personal=True)
        yield from artifacts(NAME, Kind.SETTINGS, base / "settings.local.json", personal=True)

    def pure_history(self, path: Path) -> bool:
        return matches_any(path, _RECORDS)

    def read_config(self, artifact: Artifact) -> AgentConfig:
        path = artifact.path
        in_folder = path.parent.name == "mcpServers"
        if not (in_folder or path.name in ("config.yaml", "config.json")):
            return AgentConfig()
        return read_mcp_file(path, _SECRET_FIX, "mcpServers")
