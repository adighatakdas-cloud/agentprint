"""Registry of supported agents. Add a new agent here; checks pick it up automatically."""

from .amp import Amp
from .antigravity import Antigravity
from .augment import Augment
from .base import Agent
from .claude_code import ClaudeCode
from .claude_desktop import ClaudeDesktop
from .cline import Cline
from .codex import Codex
from .continue_dev import Continue
from .copilot import Copilot
from .cursor import Cursor
from .factory_droid import FactoryDroid
from .gemini import Gemini
from .hermes import Hermes
from .junie import Junie
from .kiro import Kiro
from .opencode import OpenCode
from .qwen_code import QwenCode
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
    ClaudeDesktop(),
    Continue(),
    QwenCode(),
    Kiro(),
    FactoryDroid(),
    Augment(),
    Amp(),
    Junie(),
    *SCAN_ONLY_AGENTS,
)


def by_name(name: str) -> Agent | None:
    return next((agent for agent in ALL_AGENTS if agent.name == name), None)
