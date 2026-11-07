"""agentprint as a native plugin or extension: the commands a plugin runs, and where each agent
keeps the plugins it installed (read-only, so `agentprint status` can see a double install).

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

import json
import shlex
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .integrations import antigravity, claude_code, codex, copilot, gemini, hermes
from .integrations.base import BLOCK_EXIT, Env

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


# ---- where agents keep installed plugins (read-only) --------------------------------------------

MAX_MANIFEST = 64 * 1024
# Copilot CLI's manifest lookup order inside a plugin folder.
COPILOT_MANIFESTS = (
    ".plugin/plugin.json",
    "plugin.json",
    ".github/plugin/plugin.json",
    ".claude-plugin/plugin.json",
)


def _glob(base: Path, pattern: str) -> list[Path]:
    try:
        return sorted(base.glob(pattern))
    except OSError:
        return []


def _named(folder: Path) -> bool:
    """True when a Copilot plugin folder's manifest names agentprint."""
    for name in COPILOT_MANIFESTS:
        path = folder / name
        try:
            if path.is_file() and path.stat().st_size <= MAX_MANIFEST:
                data = json.loads(path.read_text(encoding="utf-8"))
                return isinstance(data, dict) and data.get("name") == NAME
        except (OSError, ValueError):
            return False
    return False


def _claude(home: Path, env: Env) -> list[Path]:
    # ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/
    # https://code.claude.com/docs/en/plugin-marketplaces
    return _glob(claude_code.config_dir(home, env) / "plugins" / "cache", f"*/{NAME}")


def _codex(home: Path, env: Env) -> list[Path]:
    # ~/.codex/plugins/cache/$MARKETPLACE_NAME/$PLUGIN_NAME/$VERSION/
    # https://developers.openai.com/codex/plugins/build
    return _glob(codex.config_dir(home, env) / "plugins" / "cache", f"*/{NAME}")


def _copilot(home: Path, env: Env) -> list[Path]:
    # ~/.copilot/installed-plugins/MARKETPLACE/PLUGIN-NAME, direct installs in _direct/SOURCE-ID/
    # https://docs.github.com/en/copilot/reference/cli-plugin-reference
    base = copilot.config_dir(home, env) / "installed-plugins"
    found = [path for path in _glob(base, f"*/{NAME}") if path.parent.name != "_direct"]
    return found + [path for path in _glob(base / "_direct", "*") if _named(path)]


def _gemini(home: Path, env: Env) -> list[Path]:
    # ~/.gemini/extensions/<name>/ https://geminicli.com/docs/extensions/
    return [gemini.config_dir(home, env) / "extensions" / NAME]


def _cursor(home: Path, env: Env) -> list[Path]:
    # Only the local-plugin folder is documented: https://cursor.com/docs/plugins
    return [home / ".cursor" / "plugins" / "local" / NAME]


def _antigravity(home: Path, env: Env) -> list[Path]:
    # ~/.gemini/config/plugins/<plugin_name>/ https://antigravity.google/docs/plugins
    return [antigravity.config_dir(home, env) / "plugins" / NAME]


def _hermes(home: Path, env: Env) -> list[Path]:
    # ~/.hermes/plugins/<name>/ https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
    return [hermes.config_dir(home, env) / "plugins" / NAME]


PLUGIN_FOLDERS: dict[str, Callable[[Path, Env], list[Path]]] = {
    "claude-code": _claude,
    "codex": _codex,
    "copilot": _copilot,
    "gemini": _gemini,
    "cursor": _cursor,
    "antigravity": _antigravity,
    "hermes": _hermes,
}

# How to take the plugin out again, per agent (shown when both a plugin and `install` are in).
REMOVE_PLUGIN = {
    "claude-code": "claude plugin uninstall agentprint@agentprint",
    "codex": "uninstall agentprint in Codex's /plugins menu",
    "copilot": "copilot plugin uninstall agentprint",
    "gemini": "gemini extensions uninstall agentprint",
    "cursor": "delete ~/.cursor/plugins/local/agentprint, then reload the Cursor window",
    "antigravity": "agy plugin uninstall agentprint",
    "hermes": "hermes plugins disable agentprint, then delete ~/.hermes/plugins/agentprint",
}


def plugin_found(agent: str, home: Path, env: Env) -> bool:
    """True when the agent's own plugin folder holds an agentprint plugin. Only looks."""
    folders = PLUGIN_FOLDERS.get(agent)
    if folders is None:
        return False
    try:
        return any(path.is_dir() for path in folders(home, env))
    except OSError:
        return False
