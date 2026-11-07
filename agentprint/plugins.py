"""agentprint as a native plugin or extension: the commands a plugin runs.

The plugin files live in the repository under plugins/agentprint/, plus the few manifests an
ecosystem requires at the repository root. tools/plugins.py writes them (through
tools/gen_docs.py, which also drift-tests them) from this module and from each integration's own
`edits()`: a plugin hook is the very entry `agentprint install` writes for that agent, in that
agent's hook protocol, with a command that starts this exact version through uvx instead of the
launcher `install` puts in ~/.agentprint.

Ruling (no launcher in a plugin): the command is a one-line /bin/sh script that does what the
launcher does. A missing uvx, or uvx failing (offline with an empty cache, say: uv's own errors
exit 2, which Claude Code and Codex read as "block"), lets the command run: exit 0 with the
agent's own allow output and one line on stderr, never a retry. `AGENTPRINT_STRICT=1` reaches
`agentprint guard` where the agent passes its environment on. Cost if wrong: like the launcher,
strict mode cannot block when agentprint never starts. POSIX only, like `install`'s hooks.
"""

from __future__ import annotations

import shlex

from . import __version__
from .integrations.base import BLOCK_EXIT

NAME = "agentprint"  # the plugin, extension and marketplace name in every ecosystem
PLUGIN_MARK = "agentprint-plugin"  # $0 of every plugin hook command
# Where uv's installer puts uvx. Hooks of GUI apps and sanitized hook environments may lack them.
UV_DIRS = ("$HOME/.local/bin", "$HOME/.cargo/bin")
NO_UVX = "agentprint: uvx not found, so this command was not checked (install uv to turn it on)"
FAILED = "agentprint: hook check failed (exit $s); the command was allowed"


def guard_argv(agent: str, version: str = __version__) -> tuple[str, ...]:
    return ("uvx", "--from", f"agentprint=={version}", "agentprint", "guard", "--agent", agent)


def mcp_argv(version: str = __version__) -> tuple[str, ...]:
    return ("uvx", "--from", f"agentprint=={version}", "agentprint", "mcp")


def hook_command(agent: str, allow: str = "", version: str = __version__) -> str:
    """The command a plugin hook runs: `uvx --from agentprint==<version> agentprint guard
    --agent <agent>`, with the launcher's exit-code handling around it (see the ruling above).
    `allow` is the agent's own "allow" output, printed when agentprint could not run."""
    allowed = f"printf '%s\\n' {shlex.quote(allow.strip())}; " if allow.strip() else ""
    script = (
        f'PATH="$PATH:{":".join(UV_DIRS)}"; '
        f'command -v uvx >/dev/null 2>&1 || {{ echo "{NO_UVX}" >&2; {allowed}exit 0; }}; '
        f"out=$({' '.join(guard_argv(agent, version))}); s=$?; "
        f'case "$s" in 0) [ -n "$out" ] && printf "%s\\n" "$out"; exit 0 ;; '
        f'{BLOCK_EXIT}) [ -n "$out" ] && printf "%s\\n" "$out"; exit 2 ;; esac; '
        f'echo "{FAILED}" >&2; {allowed}exit 0'
    )
    return " ".join(shlex.quote(part) for part in ("/bin/sh", "-c", script, PLUGIN_MARK))
