"""Who is running agentprint: a human, an AI agent, or CI.

AI agents and CI always get plain text and never an interactive menu.
"""

from collections.abc import Mapping
from typing import TextIO

# Env vars an agent sets in the shells it runs. Only markers with evidence are listed.
# "Confirmed live" = tests/live (the real agent CLI at its pinned version, driven by a mock
# model, with color forced on) saw the marker set, by name, in the agent's shell tool, and
# what agentprint printed there was plain text: the agent format for `agentprint scan`, the
# help (never the menu) for bare `agentprint`. All six live-tested agents set a marker.
AGENT_MARKERS: tuple[str, ...] = (
    # Claude Code: "Set to 1 in subprocesses Claude Code spawns"
    # https://code.claude.com/docs/en/env-vars
    # Confirmed live in CI on 2026-11-06 (Claude Code 2.1.318).
    "CLAUDECODE",
    # Gemini CLI: run_shell_command "sets the GEMINI_CLI=1 environment variable"
    # https://geminicli.com/docs/tools/shell/
    # Confirmed live in CI on 2026-11-06 (Gemini CLI 0.67.0).
    "GEMINI_CLI",
    # Codex: set to "1" on child processes of shell tool calls when the network is restricted
    # https://github.com/openai/codex/blob/main/codex-rs/core/src/spawn.rs
    # Not seen live: the CI run bypasses Codex's sandbox, so the network is not restricted.
    "CODEX_SANDBOX_NETWORK_DISABLED",
    # Codex thread id, first found in a third-party detection list
    # https://github.com/sdairs/is-ai-agent
    # Confirmed live in CI on 2026-11-06 (Codex 0.166.0, sandbox bypassed). Codex also sets
    # NO_COLOR in its shells.
    "CODEX_THREAD_ID",
    # Cursor (CLI and IDE agent terminals); third-party detection list, no first-party doc.
    # Not live-tested (no headless CLI in tests/live).
    # https://github.com/sdairs/is-ai-agent
    "CURSOR_AGENT",
    # OpenCode; third-party list, which warns it may not propagate to spawned shells
    # https://github.com/sdairs/is-ai-agent
    # Confirmed live in CI on 2026-11-06 (OpenCode 1.19.5): it does reach bash tool commands.
    "OPENCODE",
    # GitHub Copilot CLI; third-party list, no first-party doc found
    # https://github.com/sdairs/is-ai-agent
    # Confirmed live in CI on 2026-11-06 (Copilot CLI 1.0.105).
    "COPILOT_CLI",
    # Hermes Agent: "exported automatically into every tool subprocess Hermes spawns"
    # https://hermes-agent.nousresearch.com/docs/reference/environment-variables
    # Confirmed live in CI on 2026-11-06 (Hermes Agent 0.19.0).
    "HERMES_SESSION_ID",
    # Antigravity; third-party list only, weakest evidence. Not live-tested.
    # https://github.com/sdairs/is-ai-agent
    "ANTIGRAVITY_AGENT",
    # No documented env marker found: Windsurf (none found), Devin (detected via the file
    # /opt/.devin, not an env var). Neither has a headless CLI to test live. With no marker,
    # agentprint goes by the pipe: when the agent captures a command's output (all six
    # live-tested agents do; none gives commands a terminal) the model gets plain text and
    # never the menu, but the person's layout rather than the agent format. An agent that ran
    # commands in a real terminal without a marker would look like a person.
)


def is_agent(env: Mapping[str, str]) -> bool:
    return any(env.get(m) for m in AGENT_MARKERS)


def _dumb(env: Mapping[str, str]) -> bool:
    return env.get("TERM") == "dumb"


def _tty(stream: TextIO | None) -> bool:
    """A real terminal. `agentprint <&-` leaves sys.stdin as None; a closed file raises."""
    try:
        return stream is not None and stream.isatty()
    except (ValueError, OSError):
        return False


def should_animate(env: Mapping[str, str], stdout: TextIO | None, plain: bool = False) -> bool:
    return (
        _tty(stdout)
        and not plain
        and not env.get("CI")
        and not env.get("NO_COLOR")
        and not _dumb(env)
        and not is_agent(env)
    )


def plain_help(env: Mapping[str, str], stdout: TextIO | None) -> bool:
    """Help text without escape codes: for an AI agent (even in a real terminal) and for any
    pipe. Only a person at a terminal gets typer's colored help."""
    return is_agent(env) or not _tty(stdout)


def is_interactive(env: Mapping[str, str], stdin: TextIO | None, stdout: TextIO | None) -> bool:
    return (
        _tty(stdin) and _tty(stdout) and not env.get("CI") and not _dumb(env) and not is_agent(env)
    )
