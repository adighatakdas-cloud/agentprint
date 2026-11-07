"""Agents agentprint knows only by the files they leave in projects and packages.

Each entry gives package rules (AP006: these files must not ship in an npm/PyPI package) and,
where the agent writes inside the project itself (Aider's transcripts in the repo root, Crush's
.crush/), those project files. Home folders and config are not read yet; an agent graduates to its
own module when they are. Adding an entry here never touches a check.

Rule of thumb, as for the full modules: transcripts, logs and key stores are flagged; personal
settings are flagged; shared-by-design config, rules and instruction files (AGENTS.md,
.clinerules/, .goosehints, .kiro/steering/, .agents/plugins/) are not.

Paths from each agent's docs, checked 2026-11-05 (none of these agents is installed on the
verification Mac except Warp, whose database name was checked by name only):
aider  https://aider.chat/docs/config/aider_conf.html, github.com/Aider-AI/aider aider/args.py
kilo   https://kilo.ai/docs/code-with-ai/agents/session-history, https://kilo.ai/docs/automate/mcp
crush  github.com/charmbracelet/crush README and schema.json (data_directory ".crush")
goose  https://goose-docs.ai/docs/guides/config-files, https://goose-docs.ai/docs/guides/logs
zed    github.com/zed-industries/zed crates/agent/src/db.rs (threads/threads.db)
pi     github.com/earendil-works/pi packages/coding-agent/docs/{configuration,mcp}.md
openclaw https://docs.openclaw.ai/help/environment, .../gateway/security/secrets-and-storage
warp   ~/Library/Group Containers/<team>.dev.warp/.../warp.sqlite (name verified on macOS)
roo    https://roocodeinc.github.io/Roo-Code/features/settings-management, .../using-mcp-in-roo

Ruling (Zed): .zed/settings.json is not flagged. It can hold context_servers keys, but a path
rule can't see that, and editor settings are committed on purpose in many repos.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..models import AgentConfig, Artifact, Kind, PackageRule, Severity
from .base import artifacts

H, M, L = Severity.HIGH, Severity.MEDIUM, Severity.LOW
MCP_WHY = "MCP server config; rarely meant for a package (keys inside are reported separately)."


@dataclass(frozen=True)
class ScanOnlyAgent:
    name: str
    package_rules: tuple[PackageRule, ...]
    # (project-relative path, kind, personal): files the agent writes inside the project.
    project_files: tuple[tuple[str, Kind, bool], ...] = ()

    def discover_home(self, home: Path) -> Iterator[Artifact]:
        return iter(())

    def discover_project(self, root: Path) -> Iterator[Artifact]:
        for rel, kind, personal in self.project_files:
            yield from artifacts(self.name, kind, root / rel, personal=personal)

    def pure_history(self, path: Path) -> bool:
        """Every history file listed here is a transcript or a log (the rule of thumb above)."""
        text = path.as_posix()
        return any(
            text.endswith(f"/{rel}") or f"/{rel}/" in text
            for rel, kind, _ in self.project_files
            if kind is Kind.HISTORY
        )

    def read_config(self, artifact: Artifact) -> AgentConfig:
        return AgentConfig()


def _agent(
    name: str,
    rules: list[tuple[str, Severity, str]],
    project_files: tuple[tuple[str, Kind, bool], ...] = (),
) -> ScanOnlyAgent:
    compiled = tuple(PackageRule(name, re.compile(p), sev, why) for p, sev, why in rules)
    return ScanOnlyAgent(name, compiled, project_files)


SCAN_ONLY_AGENTS: tuple[ScanOnlyAgent, ...] = (
    _agent(
        "aider",
        [
            (
                r"(^|/)\.aider\.(chat\.history\.md|input\.history|llm\.history)$",
                H,
                "Aider transcript or typed-input history (written to the repo root).",
            ),
            (r"(^|/)\.aider/oauth-keys\.env$", H, "Aider's saved OpenRouter key."),
            (r"(^|/)\.aider\.conf\.yml$", M, "Aider config; may hold API keys."),
            (r"(^|/)\.aider\.tags\.cache\.v\d+/", L, "Aider repo-map cache."),
        ],
        (
            (".aider.chat.history.md", Kind.HISTORY, True),
            (".aider.input.history", Kind.HISTORY, True),
            (".aider.llm.history", Kind.HISTORY, True),
            (".aider.conf.yml", Kind.SETTINGS, True),
        ),
    ),
    _agent(
        "kilo",
        [
            (
                r"(^|/)\.local/share/kilo/",  # kilo.db, auth.json; not any file named kilo.db
                H,
                "Kilo Code session database, snapshots or auth.json.",
            ),
            (r"(^|/)\.kilo/kilo\.jsonc?$|(^|/)\.kilocode/mcp\.json$", L, MCP_WHY),
        ],
    ),
    _agent(
        "crush",
        [
            (
                r"(^|/)\.crush/",
                H,
                "Crush data folder: crush.db transcripts, logs, overrides that can hold keys.",
            ),
            (r"(^|/)\.local/share/crush/", H, "Crush global data: provider API keys."),
        ],
        ((".crush/crush.db", Kind.HISTORY, True), (".crush/logs", Kind.HISTORY, True)),
    ),
    _agent(
        "goose",
        [
            (
                r"(^|/)\.config/goose/secrets\.yaml$|(^|/)\.local/share/goose/sessions/"
                r"|(^|/)\.local/state/goose/logs/",
                H,
                "Goose plain-text secrets, sessions or raw LLM request logs.",
            ),
        ],
    ),
    _agent("zed", [(r"(?i)(^|/)zed/threads/threads\.db$", H, "Zed agent thread database.")]),
    _agent(
        "pi",
        [
            (r"(^|/)\.pi/agent/", H, "Pi agent home: sessions, auth.json keys, MCP tokens."),
            (r"(^|/)\.pi/mcp\.json$", L, MCP_WHY),
        ],
    ),
    _agent(
        "openclaw",
        [
            (
                r"(^|/)\.(openclaw|clawdbot|moltbot)/(agents|credentials|state|secrets|sessions"
                r"|sandboxes)/|(^|/)\.(openclaw|clawdbot|moltbot)/(\.env|openclaw\.json"
                r"|clawdbot\.json|moltbot\.json|exec-approvals\.json)$",
                H,
                "OpenClaw state: .env and config with keys, credentials, session databases.",
            ),
        ],
    ),
    _agent(
        "warp",
        [
            (
                # Only inside Warp's own data folder (dev.warp.Warp-Stable, -Preview, ...).
                r"(^|/)dev\.warp\.Warp-[^/]+/warp\.sqlite(-wal|-shm)?$",
                H,
                "Warp terminal database: commands, terminal output and AI conversations.",
            ),
        ],
    ),
    _agent(
        "roo-code",
        [
            (
                r"(^|/)roo-code-settings\.json$|(^|/)rooveterinaryinc\.roo-cline/(tasks|settings)/",
                H,
                "Roo Code settings export (plain-text API keys) or task history.",
            ),
            (r"(^|/)\.roo/mcp\.json$", L, MCP_WHY),
        ],
    ),
)
