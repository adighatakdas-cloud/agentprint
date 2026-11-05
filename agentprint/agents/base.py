"""What every agent module provides, plus parsing helpers shared by several agents."""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..files import walk_files
from ..models import AgentConfig, Artifact, HookCommand, Kind, McpServer, PackageRule


@dataclass(frozen=True)
class HomeArtifact(Artifact):
    """A file found by discover_home: the user's own agent data, outside any project.

    `home` is the home folder it was found under. Project files stay plain Artifacts, so a
    check can tell a private history file from shared config committed with a repo (AP008).
    """

    home: Path | None = None


def from_home(
    discover: Callable[[Any, Path], Iterator[Artifact]],
) -> Callable[[Any, Path], Iterator[Artifact]]:
    """Decorator for discover_home: tags every artifact it yields as a HomeArtifact."""

    @functools.wraps(discover)
    def wrapper(self: Any, home: Path) -> Iterator[Artifact]:
        for a in discover(self, home):
            yield HomeArtifact(a.agent, a.kind, a.path, a.personal, home=home)

    return wrapper


class Agent(Protocol):
    name: str
    package_rules: tuple[PackageRule, ...]

    def discover_home(self, home: Path) -> Iterator[Artifact]: ...

    def discover_project(self, root: Path) -> Iterator[Artifact]: ...

    def read_config(self, artifact: Artifact) -> AgentConfig:
        """Grants, hooks and MCP servers in one of this agent's config files. Never raises;
        a file the agent doesn't understand (or a broken one) gives an empty AgentConfig."""
        ...


def artifacts(agent: str, kind: Kind, *paths: Path, personal: bool = False) -> Iterator[Artifact]:
    """Every existing file at or under each path, tagged with its agent and kind."""
    for path in paths:
        for file in walk_files(path):
            yield Artifact(agent=agent, kind=kind, path=file, personal=personal)


def gemini_folder(home: Path) -> Path:
    """~/.gemini, or $GEMINI_CLI_HOME/.gemini when that is set (shared by Gemini CLI and
    Antigravity; install uses the same rule). https://geminicli.com/docs/reference/configuration
    """
    root = os.environ.get("GEMINI_CLI_HOME")
    return (Path(root) if root else home) / ".gemini"


def dig(data: Any, *keys: str) -> Any:
    """data[k1][k2]... or None when any level is missing or not a dict."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


# --- command allowlists ---------------------------------------------------------------------

# Programs that run whatever command follows them: allowing one allows everything.
_RUNS_ANYTHING = frozenset(
    {"bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd"}
    | {"sudo", "doas", "eval", "exec", "env", "xargs", "nohup"}
)
_WILDCARD_TAIL = re.compile(r"(?::\*|\s*\*+)$")


def is_blanket_command(pattern: str, *, prefix: bool) -> bool:
    """True if a command allow-rule lets the agent run any command at all.

    `prefix` says whether the agent matches rules as command prefixes (Cursor, Gemini CLI,
    Windsurf) rather than exactly unless a trailing wildcard is given (Claude Code, OpenCode).
    Blanket: only a wildcard ("*", ":*"), or a shell/runner such as "bash -c" that is open-ended.
    """
    text = pattern.strip()
    wild = bool(_WILDCARD_TAIL.search(text))
    core = _WILDCARD_TAIL.sub("", text).strip()
    if not core:
        return wild or prefix
    if not (wild or prefix):
        return False
    first, *rest = core.split()
    program = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
    return program in _RUNS_ANYTHING and all(word.startswith(("-", "/")) for word in rest)


# --- hooks ----------------------------------------------------------------------------------


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value if isinstance(part, str | int | float))
    return ""


def hook_commands(hooks: Any) -> tuple[HookCommand, ...]:
    """Commands from a hooks object in either common shape:

    nested (Claude Code, Codex, Gemini CLI): {event: [{matcher, hooks: [{command}]}]}
    flat (Cursor, Windsurf):                 {event: [{command}]}

    A Claude Code "http" handler ({type: "http", url}) POSTs to its URL; it is reported as the
    command "POST <url>".
    """
    found: list[HookCommand] = []
    if not isinstance(hooks, dict):
        return ()
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            handlers = inner if isinstance(inner, list) else [entry]
            for handler in handlers:
                if not isinstance(handler, dict) or handler.get("enabled") is False:
                    continue  # enabled: false (Antigravity) means the hook never runs
                # commandWindows: Codex; bash / powershell: Copilot's per-platform commands.
                for key in ("command", "commandWindows", "command_windows", "bash", "powershell"):
                    text = _command_text(handler.get(key))
                    if text:
                        found.append(HookCommand(str(event), text))
                if isinstance(handler.get("exec"), str):  # Copilot's shell-free exec + args
                    args = handler.get("args")
                    argv = [handler["exec"], *(args if isinstance(args, list) else [])]
                    found.append(HookCommand(str(event), _command_text(argv)))
                if handler.get("type") == "http" and isinstance(handler.get("url"), str):
                    found.append(HookCommand(str(event), f"POST {handler['url']}"))
    return tuple(found)


# --- MCP servers ----------------------------------------------------------------------------

_ENV_KEYS = ("env", "environment")  # environment: OpenCode
_HEADER_KEYS = ("headers", "http_headers")  # http_headers: Codex
_URL_KEYS = ("url", "httpUrl", "serverUrl")  # httpUrl: Gemini CLI, serverUrl: Windsurf
_TOKEN_KEYS = ("bearer_token",)  # older Codex inline token


def _argv_fields(field: str, argv: list[str]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for i, arg in enumerate(argv):
        fields.append((field, arg))
        # "--api-key VALUE" reads like "--api-key=VALUE" to the secret patterns.
        if arg.startswith("-") and "=" not in arg and i + 1 < len(argv):
            fields.append((field, f"{arg}={argv[i + 1]}"))
    return fields


def mcp_servers(servers: Any, secret_fix: str) -> tuple[McpServer, ...]:
    """Flatten a {name: entry} map of MCP servers (every agent uses this outer shape)."""
    if not isinstance(servers, dict):
        return ()
    found: list[McpServer] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        fields: list[tuple[str, str]] = []
        command = entry.get("command")
        if isinstance(command, str):
            fields.append(("command", command))
        elif isinstance(command, list):  # OpenCode: ["npx", "-y", "pkg", ...]
            fields += _argv_fields("command", [str(c) for c in command if isinstance(c, str)])
        args = entry.get("args")
        if isinstance(args, list):
            fields += _argv_fields("args", [str(a) for a in args if isinstance(a, str)])
        for key in _ENV_KEYS:
            values = entry.get(key)
            if isinstance(values, dict):
                fields += [
                    (f"{key}.{k}", f"{k}={v}") for k, v in values.items() if isinstance(v, str)
                ]
        for key in _HEADER_KEYS:
            values = entry.get(key)
            if isinstance(values, dict):
                fields += [
                    (f"{key}.{k}", f"{k}: {v}") for k, v in values.items() if isinstance(v, str)
                ]
        for key in _URL_KEYS:
            if isinstance(entry.get(key), str):
                fields.append((key, entry[key]))
        for key in _TOKEN_KEYS:
            if isinstance(entry.get(key), str):
                fields.append((key, f"{key} = {entry[key]}"))
        found.append(McpServer(str(name), tuple(fields), secret_fix))
    return tuple(found)
