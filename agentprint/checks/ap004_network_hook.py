"""AP004: an agent hook runs a command that talks to the network.

Hooks run automatically on agent events (every tool call, every prompt, session end), with the
user's full permissions. One that calls curl/wget/nc, pipes a download into a shell, or hits a
URL can quietly send code, prompts or keys off the machine, or pull in code to run. Hooks are
read from every agent that has them: Claude Code, Codex (hooks + notify), Cursor, Gemini CLI,
Windsurf.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from ..models import Artifact, Finding, HookCommand, Severity
from ..structured import find_line
from .base import agent_configs, safe_snippet

_PROGRAMS = re.compile(
    r"(?i)(?<![\w.-])(curl|wget|ncat|netcat|nc|socat|telnet"
    r"|invoke-webrequest|invoke-restmethod|iwr|irm)(?![\w.-])"
)
_PIPE_TO_SHELL = re.compile(r"\|\s*(?:sudo\s+)?((?:ba|z|da|k)?sh)\b")
_URL = re.compile(r"(?i)\b(https?)://")


def network_indicator(command: str) -> str | None:
    """What makes a command a network call ("curl", "| sh", "https://"), or None."""
    if match := _PROGRAMS.search(command):
        return match.group(1).lower()
    if match := _PIPE_TO_SHELL.search(command):
        return f"| {match.group(1)}"
    if match := _URL.search(command):
        return f"{match.group(1).lower()}://"
    return None


def _line(path: Path, hook: HookCommand) -> int | None:
    words = hook.command.split()
    for needle in (hook.command[:60], json.dumps(hook.command)[1:61], *words[:3], hook.event):
        line = find_line(path, needle)
        if line:
            return line
    return None


# How each finding's fix starts asking: an AI agent can't tell who added a hook.
ASK_FIRST = "Ask the human whether they added this hook; if not, delete it."


class NetworkHook:
    id = "AP004"
    severity = Severity.MEDIUM
    title = "Agent hook calls the network"
    fix = (
        "Delete the hook if you did not add it. If you did, call a pinned URL with a script you "
        "keep in the repo, never pipe a download into a shell, and read tokens from environment "
        "variables."
    )

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact, config in agent_configs(artifacts):
            for hook in config.hooks:
                indicator = network_indicator(hook.command)
                if indicator is None:
                    continue
                event = safe_snippet(hook.event, 40)
                command = safe_snippet(hook.command, 100)
                where = "the notify setting" if hook.event == "notify" else f"hooks.{event}"
                findings.append(
                    Finding(
                        check_id=self.id,
                        severity=self.severity,
                        title=self.title,
                        agent=artifact.agent,
                        location=str(artifact.path),
                        line=_line(artifact.path, hook),
                        detail=f'{event} hook runs {indicator}: "{command}"',
                        fix=(
                            f"In this file, find {where}. {ASK_FIRST} If it is wanted, move the "
                            "command into a script kept in the repo that calls one pinned URL, "
                            "never pipes a download into a shell, and reads tokens from "
                            "environment variables."
                        ),
                    )
                )
        return findings


CHECK = NetworkHook()
