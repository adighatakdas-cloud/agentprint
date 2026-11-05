"""Which AI agents are on this machine, and which have agentprint armed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .install import State
from .integrations import ALL_INTEGRATIONS


@dataclass(frozen=True)
class AgentState:
    name: str
    label: str
    found: bool
    armed: bool


SHORT_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "windsurf": "Windsurf",
    "devin": "Devin",
    "opencode": "OpenCode",
    "copilot": "Copilot",
    "hermes": "Hermes",
    "antigravity": "Antigravity",
}


def roster(home: Path, env: Mapping[str, str]) -> list[AgentState]:
    try:
        files = State.load(home).files
        armed = {entry.get("agent") for entry in files.values() if isinstance(entry, dict)}
    except (AttributeError, TypeError, ValueError):  # state.json is JSON of the wrong shape
        armed = set()
    return [
        AgentState(
            name, SHORT_NAMES.get(name, integ.label), integ.installed(home, env), name in armed
        )
        for name, integ in ALL_INTEGRATIONS.items()
    ]


def summary_line(states: list[AgentState]) -> str:
    found = [s for s in states if s.found]
    if not found:
        return "no AI agents found on this machine"
    armed = sum(s.armed for s in found)
    line = f"{armed} of {len(found)} agents armed"
    unarmed = [s.label for s in found if not s.armed]
    if unarmed:
        line += f" · {', '.join(unarmed)} {'is' if len(unarmed) == 1 else 'are'} not"
    return line
