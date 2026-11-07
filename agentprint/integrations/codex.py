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
- Windows: "`commandWindows` is an optional Windows-only command override" (hooks page, checked
  2026-11-07). Codex runs it through %COMSPEC% (cmd.exe) as `/C "<command>"` with the payload on
  stdin, and uses `command` only when `commandWindows` is absent (codex-rs/hooks
  engine/command_runner.rs and engine/discovery.rs, read 2026-11-07). `command` is required, and
  a file written on Windows is for Windows, so both hold the Windows command.
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


# Ruling: the server is written as dotted keys at the very top of config.toml, not as an
# [mcp_servers.agentprint] table at the end. Top-level keys must come before any table, so
# nothing the user or Codex appends can land inside agentprint's block, and a `[mcp_servers.x]`
# table further down stays valid TOML. Lines between the markers that aren't agentprint's
# (in case Codex's writer puts entries there) are kept on uninstall. Cost if wrong: a file
# with a bare `[mcp_servers]` table can't take the dotted keys; it is skipped with a snippet.
OWN_PREFIX = "mcp_servers.agentprint."


def _toml_body(launch: Launch) -> str:
    args = ", ".join(json.dumps(arg) for arg in launch.mcp[1:])
    return f"{OWN_PREFIX}command = {json.dumps(launch.mcp[0])}\n{OWN_PREFIX}args = [{args}]\n"


def _owned(line: str) -> bool:
    return line.lstrip().startswith(OWN_PREFIX)


def _valid_toml(text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Unsupported(
            "adding mcp_servers.agentprint would not be valid TOML (is it already defined, or "
            "is there a bare [mcp_servers] table?)"
        ) from exc


def _instructions_file(base: Path) -> Path:
    override = base / "AGENTS.override.md"
    try:
        if override.is_file() and override.read_text(encoding="utf-8").strip():
            return override
    except OSError:
        pass
    return base / "AGENTS.md"


def _handler(launch: Launch) -> dict:
    if launch.windows:
        command = launch.windows_command(NAME)
        return {"type": "command", "command": command, "commandWindows": command, "timeout": 60}
    return {"type": "command", "command": launch.hook_command(NAME), "timeout": 60}


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    entry = {"matcher": "^Bash$", "hooks": [_handler(launch)]}
    return [
        JsonEdit(
            base / "hooks.json",
            "hook: check publish/push before a shell command runs",
            add=lambda c: put_hook(c, "PreToolUse", entry),
            remove=lambda c: drop_hook(c, "PreToolUse"),
        ),
        BlockEdit(
            base / "config.toml",
            "MCP server",
            _toml_body(launch),
            "toml",
            validate=_valid_toml,
            top=True,
            owns=_owned,
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
    windows_hooks=True,
)
