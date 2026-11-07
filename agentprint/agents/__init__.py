"""Registry of supported agents. Add a new agent here; checks pick it up automatically."""

from .antigravity import Antigravity
from .base import Agent
from .claude_code import ClaudeCode
from .cline import Cline
from .codex import Codex
from .copilot import Copilot
from .cursor import Cursor
from .gemini import Gemini
from .hermes import Hermes
from .opencode import OpenCode
from .scan_only import SCAN_ONLY_AGENTS
from .windsurf import Windsurf

# Agents agentprint can also arm (install) first, then agents whose home folders a sweep reads
# in full, then agents known only by their package and project files. When two agents name the
# same file, the first one keeps it.
ALL_AGENTS: tuple[Agent, ...] = (
    ClaudeCode(),
    Codex(),
    Cursor(),
    Gemini(),
    Windsurf(),
    OpenCode(),
    Copilot(),
    Hermes(),
    Antigravity(),
    Cline(),
    *SCAN_ONLY_AGENTS,
)


def by_name(name: str) -> Agent | None:
    return next((agent for agent in ALL_AGENTS if agent.name == name), None)
