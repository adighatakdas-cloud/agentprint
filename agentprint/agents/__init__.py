"""Registry of supported agents. Add a new agent here; checks pick it up automatically."""

from .base import Agent
from .claude_code import ClaudeCode
from .codex import Codex
from .copilot import Copilot
from .cursor import Cursor
from .gemini import Gemini
from .hermes import Hermes
from .opencode import OpenCode
from .windsurf import Windsurf

ALL_AGENTS: tuple[Agent, ...] = (
    ClaudeCode(),
    Codex(),
    Cursor(),
    Gemini(),
    Windsurf(),
    OpenCode(),
    Copilot(),
    Hermes(),
)


def by_name(name: str) -> Agent | None:
    return next((agent for agent in ALL_AGENTS if agent.name == name), None)
