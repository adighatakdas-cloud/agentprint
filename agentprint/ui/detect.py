"""Who is running agentprint: a human, an AI agent, or CI.

AI agents and CI always get plain text and never an interactive menu.
"""

from collections.abc import Mapping
from typing import TextIO

# Env vars an agent sets in the shells it runs. Only markers with evidence are listed.
AGENT_MARKERS: tuple[str, ...] = (
    # Claude Code: "Set to 1 in subprocesses Claude Code spawns"
    # https://code.claude.com/docs/en/env-vars
    "CLAUDECODE",
    # Gemini CLI: run_shell_command "sets the GEMINI_CLI=1 environment variable"
    # https://geminicli.com/docs/tools/shell/
    "GEMINI_CLI",
    # Codex: set to "1" on child processes of shell tool calls when the network is restricted
    # https://github.com/openai/codex/blob/main/codex-rs/core/src/spawn.rs
    "CODEX_SANDBOX_NETWORK_DISABLED",
    # Codex thread id (third-party detection list, not checked against Codex source)
    # https://github.com/sdairs/is-ai-agent
    "CODEX_THREAD_ID",
    # Cursor (CLI and IDE agent terminals); third-party detection list, no first-party doc
    # https://github.com/sdairs/is-ai-agent
    "CURSOR_AGENT",
    # OpenCode; third-party list, which warns it may not propagate to spawned shells
    # https://github.com/sdairs/is-ai-agent
    "OPENCODE",
    # GitHub Copilot CLI; third-party list, no first-party doc found
    # https://github.com/sdairs/is-ai-agent
    "COPILOT_CLI",
    # Hermes Agent: "exported automatically into every tool subprocess Hermes spawns"
    # https://hermes-agent.nousresearch.com/docs/reference/environment-variables
    "HERMES_SESSION_ID",
    # Antigravity; third-party list only, weakest evidence
    # https://github.com/sdairs/is-ai-agent
    "ANTIGRAVITY_AGENT",
    # No documented env marker found: Windsurf (none found), Devin (detected via the file
    # /opt/.devin, not an env var). Task 9 covers both at runtime.
)


def is_agent(env: Mapping[str, str]) -> bool:
    return any(env.get(m) for m in AGENT_MARKERS)


def _dumb(env: Mapping[str, str]) -> bool:
    return env.get("TERM") == "dumb"


def should_animate(env: Mapping[str, str], stdout: TextIO, plain: bool = False) -> bool:
    return (
        stdout.isatty()
        and not plain
        and not env.get("CI")
        and not env.get("NO_COLOR")
        and not _dumb(env)
        and not is_agent(env)
    )


def is_interactive(env: Mapping[str, str], stdin: TextIO, stdout: TextIO) -> bool:
    return (
        stdin.isatty()
        and stdout.isatty()
        and not env.get("CI")
        and not _dumb(env)
        and not is_agent(env)
    )
