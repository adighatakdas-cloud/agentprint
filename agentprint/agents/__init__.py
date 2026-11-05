"""Registry of supported agents. Add a new agent here; checks pick it up automatically."""

from .base import Agent
from .claude_code import ClaudeCode
from .codex import Codex
from .cursor import Cursor
from .gemini import Gemini

ALL_AGENTS: tuple[Agent, ...] = (ClaudeCode(), Codex(), Cursor(), Gemini())


def by_name(name: str) -> Agent | None:
    return next((agent for agent in ALL_AGENTS if agent.name == name), None)
