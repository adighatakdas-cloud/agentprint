"""Registry of supported agents. Add a new agent here; checks pick it up automatically."""

from .base import Agent
from .claude_code import ClaudeCode
from .codex import Codex

ALL_AGENTS: tuple[Agent, ...] = (ClaudeCode(), Codex())
