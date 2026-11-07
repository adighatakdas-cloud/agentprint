"""Which AI agents are on this machine, and which have agentprint armed (by `agentprint
install`, or as the agent's own plugin)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .install import State
from .integrations import ALL_INTEGRATIONS
from .plugins import ENABLED, plugin_state
from .report import _plural


@dataclass(frozen=True)
class AgentState:
    name: str
    label: str
    found: bool
    armed: bool  # by `agentprint install`
    # The agentprint plugin: None (not there), or plugins.ENABLED / DISABLED / UNKNOWN.
    plugin: str | None = None

    @property
    def guarded(self) -> bool:
        """agentprint is in this agent: through install, or as a plugin known to be enabled."""
        return self.armed or self.plugin == ENABLED

    @property
    def twice(self) -> bool:
        """In both as an enabled plugin and through install: one should go."""
        return self.armed and self.plugin == ENABLED


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
            name,
            SHORT_NAMES.get(name, integ.label),
            integ.installed(home, env),
            name in armed,
            plugin_state(name, home, env),
        )
        for name, integ in ALL_INTEGRATIONS.items()
    ]


def summary_line(states: list[AgentState]) -> str:
    found = [s for s in states if s.found]
    if not found:
        return "no AI agents found on this machine"
    armed = sum(s.guarded for s in found)
    line = f"{armed} of {_plural(len(found), 'agent')} armed"
    unarmed = [s.label for s in found if not s.guarded]
    if unarmed:
        line += f" · {', '.join(unarmed)} {'is' if len(unarmed) == 1 else 'are'} not"
    return line
