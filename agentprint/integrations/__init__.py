"""How agentprint plugs into each AI coding agent: hooks, MCP server entries, instructions."""

from .base import Integration
from .claude_code import INTEGRATION as CLAUDE_CODE
from .codex import INTEGRATION as CODEX
from .cursor import INTEGRATION as CURSOR
from .gemini import INTEGRATION as GEMINI
from .opencode import INTEGRATION as OPENCODE
from .windsurf import DEVIN_INTEGRATION as DEVIN
from .windsurf import WINDSURF_INTEGRATION as WINDSURF

ALL_INTEGRATIONS: dict[str, Integration] = {
    i.name: i for i in (CLAUDE_CODE, CODEX, CURSOR, GEMINI, WINDSURF, DEVIN, OPENCODE)
}
