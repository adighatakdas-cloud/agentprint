"""Which AI agents are on this machine, and which have agentprint armed.

`roster` lists the agents `agentprint install` can arm. `scanned` lists the agents a sweep reads
in full but install can't arm yet (their modules carry an AgentDoc): found when a sweep would
read at least one of their files under this home.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .agents import ALL_AGENTS
from .agents.base import Agent, AgentDoc
from .install import State
from .integrations import ALL_INTEGRATIONS
from .report import _plural


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
    armed = sum(s.armed for s in found)
    line = f"{armed} of {_plural(len(found), 'agent')} armed"
    unarmed = [s.label for s in found if not s.armed]
    if unarmed:
        line += f" · {', '.join(unarmed)} {'is' if len(unarmed) == 1 else 'are'} not"
    return line
