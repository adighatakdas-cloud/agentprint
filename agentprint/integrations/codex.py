"""OpenAI Codex (CLI, IDE extension and app share this config). Checked 2026-11-05.

- Hook: PreToolUse in ~/.codex/hooks.json, same JSON shape as Claude Code; shell and unified
  exec calls are matched as "Bash" and carry `tool_input.command`. Block = the same
  `hookSpecificOutput` deny; allow = exit 0, no output. Output is schema-checked, so only
  `hookSpecificOutput` and `systemMessage` are ever printed. Codex runs a new hook only after the
  user trusts it in /hooks. https://learn.chatgpt.com/docs/hooks
  (was developers.openai.com/codex/hooks), schema:
  https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated
- MCP: `[mcp_servers.<name>]` with `command` and `args` in ~/.codex/config.toml.
  https://learn.chatgpt.com/docs/extend/mcp
- Instructions: ~/.codex/AGENTS.md, unless a non-empty AGENTS.override.md takes its place.
  https://learn.chatgpt.com/docs/agent-configuration/agents-md
- CODEX_HOME moves ~/.codex. https://learn.chatgpt.com/docs/config-file/environment-variables
"""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path

from .base import (
    INSTRUCTIONS,
    BlockEdit,
    Edit,
    Env,
    HookCall,
    Integration,
    JsonEdit,
    Launch,
    Unsupported,
    cwd_from,
    drop_hook,
    put_hook,
)
from .claude_code import reply

NAME = "codex"
SHELL_TOOLS = {"Bash", "shell", "local_shell", "exec_command"}


def config_dir(home: Path, env: Env) -> Path:
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"


def _toml_body(launch: Launch) -> str:
    args = ", ".join(json.dumps(arg) for arg in launch.mcp[1:])
    return f"[mcp_servers.agentprint]\ncommand = {json.dumps(launch.mcp[0])}\nargs = [{args}]\n"


def _valid_toml(text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Unsupported(
            "adding [mcp_servers.agentprint] would not be valid TOML (is it already defined?)"
        ) from exc


def _instructions_file(base: Path) -> Path:
    override = base / "AGENTS.override.md"
    try:
        if override.is_file() and override.read_text(encoding="utf-8").strip():
            return override
    except OSError:
        pass
    return base / "AGENTS.md"


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    entry = {
        "matcher": "^Bash$",
        "hooks": [{"type": "command", "command": launch.hook_command(NAME), "timeout": 60}],
    }
    return [
        JsonEdit(
            base / "hooks.json",
            "hook: check publish/push before a shell command runs",
            add=lambda c: put_hook(c, "PreToolUse", entry),
            remove=lambda c: drop_hook(c, "PreToolUse"),
        ),
        BlockEdit(
            base / "config.toml", "MCP server", _toml_body(launch), "toml", validate=_valid_toml
        ),
        BlockEdit(_instructions_file(base), "instructions", INSTRUCTIONS),
    ]


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    if payload.get("tool_name") not in SHELL_TOOLS:
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        command = shlex.join(command)
    if not isinstance(command, str):
        return None
    cwd = cwd_from(payload.get("cwd"))
    workdir = tool_input.get("workdir")
    return HookCall(command, cwd / workdir if isinstance(workdir, str) and workdir else cwd)


INTEGRATION = Integration(
    name=NAME,
    label="Codex",
    docs=(
        "https://learn.chatgpt.com/docs/hooks",
        "https://learn.chatgpt.com/docs/extend/mcp",
        "https://learn.chatgpt.com/docs/agent-configuration/agents-md",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
    notes=(
        "Codex runs a new hook only after you trust it: open Codex, type /hooks and trust the "
        "agentprint hook.",
    ),
)
