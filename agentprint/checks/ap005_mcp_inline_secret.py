"""AP005: a key is written inline in an MCP server entry (env, headers, args, command or URL).

MCP configs are the most copied-around agent files: README snippets say "paste your token
here", and .mcp.json / .cursor/mcp.json are often committed. Every agent can read the value from
the environment instead, and the fix says exactly how for that agent.

Overlap with AP002: AP002 reads every settings file line by line, so it sees these keys too. The
scanner asks AP005 which AP002 findings it `claims` (same file, same key) and keeps only the
AP005 one, whose fix is MCP-specific. Keys elsewhere in the same file stay AP002.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..models import Artifact, Finding, McpServer, Severity
from ..secrets import find_secrets, redact
from ..structured import find_line
from .base import agent_configs, safe_snippet

_MARKER = " in MCP server "
# How each finding's fix starts: rotating is the human's (advice words it for a person).
ASK_FIRST = "Ask the human to rotate this key at its provider (an AI agent can't). Then "


def _variable(field: str, server: McpServer) -> str:
    """A sensible environment variable name for the fix: the env key, else SERVER_API_KEY."""
    head, _, key = field.partition(".")
    if head in ("env", "environment") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
        return key
    stem = re.sub(r"[^A-Za-z0-9]+", "_", server.name).strip("_").upper()[:40] or "MCP"
    return f"{stem}_TOKEN" if head in ("headers", "http_headers") else f"{stem}_API_KEY"


class McpInlineSecret:
    id = "AP005"
    severity = Severity.CRITICAL  # a key written into config: same risk as AP002
    title = "Secret inline in MCP server config"
    fix = (
        "Rotate the key, then make the MCP server read it from an environment variable "
        "instead of the config file."
    )

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact, config in agent_configs(artifacts):
            seen: set[str] = set()
            for server in config.mcp_servers:
                for field, text in server.fields:
                    for match in find_secrets(text):
                        if match.value in seen:
                            continue
                        seen.add(match.value)
                        name, where = safe_snippet(server.name, 60), safe_snippet(field, 60)
                        var = _variable(field, server)
                        findings.append(
                            Finding(
                                check_id=self.id,
                                severity=self.severity,
                                title=self.title,
                                agent=artifact.agent,
                                location=str(artifact.path),
                                line=find_line(artifact.path, match.value),
                                detail=f"{match.name}: {redact(match.value)}{_MARKER}"
                                f"{name!r} ({where})",
                                fix=ASK_FIRST + server.secret_fix.replace("<VAR>", var),
                            )
                        )
        return findings

    def claims(self, finding: Finding, mine: list[Finding]) -> bool:
        """True for an AP002 finding that reports the same key in the same file as one of ours."""
        return finding.check_id == "AP002" and any(
            own.location == finding.location and own.detail.startswith(finding.detail + _MARKER)
            for own in mine
        )


CHECK = McpInlineSecret()
