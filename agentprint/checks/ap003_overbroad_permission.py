"""AP003: an agent setting lets it run any command (or skip every prompt) without asking.

Which settings count is decided by each agent module (it knows its own syntax); this check only
turns them into findings: Claude Code Bash(*) / bypassPermissions, Codex never +
danger-full-access, Cursor Shell(*) / approvalMode unrestricted, Gemini CLI bare
run_shell_command / trusted MCP servers, Windsurf allow-listed shells, OpenCode "allow".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ..models import Artifact, Finding, Grant, Severity
from ..structured import find_line
from .base import agent_configs, safe_snippet

_GENERIC_VALUES = {"true", "allow"}


def _line(path: Path, grant: Grant) -> int | None:
    """Best-effort line of the setting: the quoted value, the value, then the key names."""
    candidates = [] if grant.value in _GENERIC_VALUES else [f'"{grant.value}"', grant.value]
    candidates += reversed(re.findall(r"\w+", grant.setting))
    for needle in candidates:
        line = find_line(path, needle)
        if line:
            return line
    return None


class OverbroadPermission:
    id = "AP003"
    severity = Severity.MEDIUM
    title = "Over-broad agent permission"
    fix = (
        "Remove the blanket grant and allow only the specific commands or tools you need; "
        "keep approve-everything modes for throwaway containers or VMs."
    )

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact, config in agent_configs(artifacts):
            for grant in config.grants:
                setting, value = safe_snippet(grant.setting, 80), safe_snippet(grant.value, 80)
                findings.append(
                    Finding(
                        check_id=self.id,
                        severity=self.severity,
                        title=self.title,
                        agent=artifact.agent,
                        location=str(artifact.path),
                        line=_line(artifact.path, grant),
                        detail=f'{setting} grants "{value}": the agent acts without asking',
                        fix=grant.fix,
                    )
                )
        return findings


CHECK = OverbroadPermission()
