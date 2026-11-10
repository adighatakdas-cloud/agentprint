"""Which AI agents are on this machine, and which have agentprint armed (by `agentprint
install`, or as the agent's own plugin).

`roster` lists the agents agentprint can arm. `scanned` lists the agents a sweep reads in full
but install can't arm yet (their modules carry an AgentDoc): found when a sweep would read at
least one of their files under this home.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import platform
from .agents import ALL_AGENTS
from .agents.base import Agent, AgentDoc
from .install import State
from .integrations import ALL_INTEGRATIONS
from .plugins import ENABLED, plugin_state
from .report import _plural


@dataclass(frozen=True)
class AgentState:
    name: str
    label: str
    found: bool
    armed: bool  # by `agentprint install`: its hook (the tripwire), MCP server and note
    # The agentprint plugin: None (not there), or plugins.ENABLED / DISABLED / UNKNOWN.
    plugin: str | None = None
    # On Windows install adds only the MCP server and the note (no hook yet): never "armed".
    mcp_only: bool = False

    @property
    def guarded(self) -> bool:
        """agentprint's tripwire is in this agent: through install, or as a plugin known to be
        enabled."""
        return self.armed or self.plugin == ENABLED

    @property
    def unarmed(self) -> bool:
        """Found, and `agentprint install` would arm it (it has nothing of agentprint's)."""
        return self.found and not self.guarded and not self.mcp_only

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
        installed = {entry.get("agent") for entry in files.values() if isinstance(entry, dict)}
    except (AttributeError, TypeError, ValueError):  # state.json is JSON of the wrong shape
        installed = set()
    windows = platform.is_windows()  # install adds no hook there: nothing is armed by it
    return [
        AgentState(
            name,
            SHORT_NAMES.get(name, integ.label),
            integ.installed(home, env),
            name in installed and not windows,
            plugin_state(name, home, env),
            mcp_only=name in installed and windows,
        )
        for name, integ in ALL_INTEGRATIONS.items()
    ]


def swept_agents() -> list[tuple[Agent, AgentDoc]]:
    """Agents a sweep reads in full that install can't arm yet, with their AgentDoc: modules
    with a doc and no integration, in registry order (`status` and the docs' agents table)."""
    found = []
    for agent in ALL_AGENTS:
        doc = getattr(agent, "doc", None)
        if isinstance(doc, AgentDoc) and agent.name not in ALL_INTEGRATIONS:
            found.append((agent, doc))
    return found


def scanned(home: Path) -> list[AgentState]:
    """swept_agents() as roster entries. Never armed; found when discover_home yields a file
    (it stops at the first one)."""
    states = []
    for agent, doc in swept_agents():
        try:
            found = next(iter(agent.discover_home(home)), None) is not None
        except Exception:  # an agent module bug must never break `status`
            found = False
        states.append(AgentState(agent.name, str(doc.label), found, False))
    return states


def summary_line(states: list[AgentState], scanned: list[AgentState] | None = None) -> str:
    found = [s for s in states if s.found]
    if not found:
        also = [s for s in scanned or [] if s.found]
        if also:
            return f"{_plural(len(also), 'agent')} scanned · none can be armed yet"
        return "no AI agents found on this machine"
    armed = sum(s.guarded for s in found)
    line = f"{armed} of {_plural(len(found), 'agent')} armed"
    mcp_only = [s for s in found if s.mcp_only and not s.guarded]
    if mcp_only:
        line += f" · {len(mcp_only)} with {MCP_ONLY}"
    unarmed = [s.label for s in found if s.unarmed]
    if unarmed:
        line += f" · {', '.join(unarmed)} {'is' if len(unarmed) == 1 else 'are'} not"
    return line


MCP_ONLY = "MCP and note; no hook on Windows yet"
