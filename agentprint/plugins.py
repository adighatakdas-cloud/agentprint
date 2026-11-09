"""agentprint as a native plugin or extension: the commands a plugin runs, and whether each agent
has the agentprint plugin installed and enabled (read-only, for `agentprint status`).

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

Ruling (status): a plugin counts as arming an agent only when the agent's own records say it is
installed and enabled (Claude Code, Codex, Hermes Agent). A plugin folder alone, where the
agent's enable switch is not documented or not readable, is "unknown": reported, never counted
as armed, so status never says "armed twice" and tells someone to uninstall their only guard.
Cost if wrong: an enabled plugin in Copilot, Gemini CLI, Cursor or Antigravity shows as
"not checked" and status still suggests `install`.
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


# ---- is the plugin installed and enabled? (read-only) --------------------------------------------

ENABLED, DISABLED, UNKNOWN = "enabled", "disabled", "unknown"
MAX_CONFIG = 1024 * 1024  # never read a bigger settings file
MAX_MANIFEST = 64 * 1024
# Copilot CLI's manifest lookup order inside a plugin folder.
COPILOT_MANIFESTS = (
    ".plugin/plugin.json",
    "plugin.json",
    ".github/plugin/plugin.json",
    ".claude-plugin/plugin.json",
)
_MISSING = object()  # a settings file that is not there (unlike one that cannot be read)


def _glob(base: Path, pattern: str) -> list[Path]:
    try:
        return sorted(path for path in base.glob(pattern) if path.is_dir())
    except OSError:
        return []


def _text(path: Path, limit: int = MAX_CONFIG) -> str | None | object:
    """The file's text; _MISSING if it isn't there; None if it can't be read."""
    try:
        if not path.exists():
            return _MISSING
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _load(path: Path, parse: Callable[[str], object], limit: int = MAX_CONFIG) -> object:
    """The parsed file as a mapping; _MISSING if it isn't there; None if it can't be read."""
    text = _text(path, limit)
    if not isinstance(text, str):
        return text
    try:
        data = parse(text)
    except (ValueError, RecursionError, TypeError, ArithmeticError):
        return None
    except Exception as exc:  # noqa: BLE001 - YAML's own errors; status must never crash
        if type(exc).__module__.startswith("yaml"):
            return None
        raise
    return data if isinstance(data, dict) else None


def _yaml(text: str) -> object:
    import yaml

    return yaml.safe_load(text)


def _best(states: list[str]) -> str | None:
    for state in (ENABLED, UNKNOWN, DISABLED):
        if state in states:
            return state
    return None


def _live(folder: Path) -> bool:
    """A cached plugin version Claude Code still uses: updates and uninstalls leave an
    `.orphaned_at` marker in the old version folder until a cleanup deletes it."""
    try:
        return folder.is_dir() and not (folder / ".orphaned_at").exists()
    except OSError:
        return False


def _switch(settings: object, plugin_id: str) -> str:
    """What ~/.claude/settings.json `enabledPlugins` says about `plugin_id` (unset means on)."""
    if settings is _MISSING:
        return ENABLED
    enabled = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else None
    if not isinstance(enabled, dict):
        return UNKNOWN
    value = enabled.get(plugin_id)
    return DISABLED if value is False else ENABLED if value in (True, None) else UNKNOWN


def _claude(home: Path, env: Env) -> str | None:
    """installed_plugins.json records each install (`scope`, `installPath`, `version`), and
    `enabledPlugins` in ~/.claude/settings.json turns `<name>@<marketplace>` on or off (unset
    means on, unless the manifest says `defaultEnabled: false`, which agentprint's does not).
    https://code.claude.com/docs/en/plugins/loading.md"""
    base = claude_code.config_dir(home, env)
    root = (
        Path(env["CLAUDE_CODE_PLUGIN_CACHE_DIR"])
        if env.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
        else base / "plugins"
    )
    cached = {
        folder.parent.name: [v for v in _glob(folder, "*") if _live(v)]
        for folder in _glob(root / "cache", f"*/{NAME}")
    }
    records = _load(root / "installed_plugins.json", json.loads)
    plugins = records.get("plugins", {}) if isinstance(records, dict) else None
    if not isinstance(plugins, dict):  # no readable record: a live cached copy is all there is
        return UNKNOWN if any(cached.values()) else None
    settings = _load(base / "settings.json", json.loads)
    states = []
    for plugin_id, entries in plugins.items():
        name, _, market = str(plugin_id).partition("@")
        if name != NAME:
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("installPath")
            live = _live(Path(path)) if isinstance(path, str) else bool(cached.get(market))
            if live:  # a project or local install is on in that repository only: unknown here
                user = entry.get("scope", "user") == "user"
                states.append(_switch(settings, str(plugin_id)) if user else UNKNOWN)
    return _best(states)


def _codex(home: Path, env: Env) -> str | None:
    """`[plugins."<plugin>@<marketplace>"] enabled = true` in config.toml turns a plugin on;
    installs are cached in ~/.codex/plugins/cache/$MARKETPLACE/$PLUGIN/$VERSION/.
    https://developers.openai.com/codex/plugins/build"""
    import tomllib

    base = codex.config_dir(home, env)
    markets = [folder.parent.name for folder in _glob(base / "plugins" / "cache", f"*/{NAME}")]
    if not markets:
        return None
    config = _load(base / "config.toml", tomllib.loads)
    table = config.get("plugins") if isinstance(config, dict) else None
    states = []
    for market in markets:
        entry = table.get(f"{NAME}@{market}") if isinstance(table, dict) else None
        switch = entry.get("enabled") if isinstance(entry, dict) else None
        states.append(ENABLED if switch is True else DISABLED if switch is False else UNKNOWN)
    return _best(states)


def _hermes(home: Path, env: Env) -> str | None:
    """Plugins in ~/.hermes/plugins/<name>/ are opt-in: on only when `plugins.enabled` in
    config.yaml lists them (`hermes plugins disable` adds them to `plugins.disabled`).
    https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins"""
    base = hermes.config_dir(home, env)
    if not _glob(base / "plugins", NAME):
        return None
    config = _load(base / "config.yaml", _yaml)
    if not isinstance(config, dict):
        return UNKNOWN
    section = config.get("plugins", {})
    if not isinstance(section, dict):
        return UNKNOWN
    enabled, disabled = section.get("enabled", []), section.get("disabled", [])
    if not isinstance(enabled, list | None) or not isinstance(disabled, list | None):
        return UNKNOWN
    if NAME in (disabled or []):
        return DISABLED
    return ENABLED if NAME in (enabled or []) else DISABLED


def _named(folder: Path) -> bool:
    """True when a Copilot plugin folder's manifest names agentprint."""
    for name in COPILOT_MANIFESTS:
        data = _load(folder / name, json.loads, MAX_MANIFEST)
        if data is not _MISSING:
            return isinstance(data, dict) and data.get("name") == NAME
    return False


def _folder_only(found: Callable[[Path, Env], list[Path]]) -> Callable[[Path, Env], str | None]:
    """For agents whose enable switch is not documented: a plugin folder is UNKNOWN."""

    def state(home: Path, env: Env) -> str | None:
        return UNKNOWN if any(path.is_dir() for path in found(home, env)) else None

    return state


def _copilot_folders(home: Path, env: Env) -> list[Path]:
    # ~/.copilot/installed-plugins/MARKETPLACE/PLUGIN-NAME, direct installs in _direct/SOURCE-ID/
    # https://docs.github.com/en/copilot/reference/cli-plugin-reference
    base = copilot.config_dir(home, env) / "installed-plugins"
    found = [path for path in _glob(base, f"*/{NAME}") if path.parent.name != "_direct"]
    return found + [path for path in _glob(base / "_direct", "*") if _named(path)]


def _gemini_folders(home: Path, env: Env) -> list[Path]:
    # <home>/.gemini/extensions; where `gemini extensions disable` keeps its state is not
    # documented. https://geminicli.com/docs/extensions/reference/
    return [gemini.config_dir(home, env) / "extensions" / NAME]


def _cursor_folders(home: Path, env: Env) -> list[Path]:
    # Only the local-plugin folder is documented: https://cursor.com/docs/plugins
    return [home / ".cursor" / "plugins" / "local" / NAME]


def _antigravity_folders(home: Path, env: Env) -> list[Path]:
    # ~/.gemini/config/plugins/<plugin_name>/ ; enable state only through `agy plugin list`.
    # https://antigravity.google/docs/plugins
    return [antigravity.config_dir(home, env) / "plugins" / NAME]


PLUGIN_STATE: dict[str, Callable[[Path, Env], str | None]] = {
    "claude-code": _claude,
    "codex": _codex,
    "copilot": _folder_only(_copilot_folders),
    "gemini": _folder_only(_gemini_folders),
    "cursor": _folder_only(_cursor_folders),
    "antigravity": _folder_only(_antigravity_folders),
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


def plugin_state(agent: str, home: Path, env: Env) -> str | None:
    """None when the agent has no agentprint plugin; ENABLED or DISABLED as the agent's own
    records say; UNKNOWN when a plugin is there but agentprint cannot tell if it is on.
    Only reads files, and never fails."""
    state = PLUGIN_STATE.get(agent)
    if state is None:
        return None
    try:
        return state(home, env)
    except (OSError, ValueError):
        return UNKNOWN
