"""Gemini CLI (Google). Checked against the official docs on 2026-11-05.

- Hook: BeforeTool with matcher "run_shell_command" in ~/.gemini/settings.json (`timeout` in
  milliseconds; `name` lets users `/hooks disable` it). Input has `tool_name`, `tool_input.command`,
  `tool_input.dir_path` and `cwd`. Block = exit 0 + `{"decision": "deny", "reason": ...}`, the
  reason goes to the agent; allow = `{}`. `systemMessage` is shown to the user. Hooks run with a
  sanitized environment, so the command is an absolute path.
  https://geminicli.com/docs/hooks , https://geminicli.com/docs/hooks/reference ,
  https://geminicli.com/docs/tools/shell
- MCP: `mcpServers` in the same settings.json (`command` + `args`, no `type`).
  https://geminicli.com/docs/tools/mcp-server
- Instructions: ~/.gemini/GEMINI.md. https://geminicli.com/docs/cli/gemini-md
- GEMINI_CLI_HOME is the folder that holds `.gemini`. https://geminicli.com/docs/reference/configuration
"""

from __future__ import annotations

import json
from pathlib import Path

from ..guard import Verdict
from .base import (
    INSTRUCTIONS,
    BlockEdit,
    Edit,
    Env,
    HookCall,
    HookReply,
    Integration,
    JsonEdit,
    Launch,
    cwd_from,
    drop_hook,
    drop_key,
    mcp_entry,
    put_hook,
    put_key,
)

NAME = "gemini"


def config_dir(home: Path, env: Env) -> Path:
    root = Path(env["GEMINI_CLI_HOME"]) if env.get("GEMINI_CLI_HOME") else home
    return root / ".gemini"


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    settings = config_dir(home, env) / "settings.json"
    entry = {
        "matcher": "run_shell_command",
        "hooks": [
            {
                "name": "agentprint-guard",
                "type": "command",
                "command": launch.hook_command(NAME, reply(None).stdout),
                "timeout": 60000,
            }
        ],
    }
    server = mcp_entry(launch)
    return [
        JsonEdit(
            settings,
            "hook: check publish/push before run_shell_command",
            add=lambda c: put_hook(c, "BeforeTool", entry),
            remove=lambda c: drop_hook(c, "BeforeTool"),
        ),
        JsonEdit(
            settings,
            "MCP server",
            add=lambda c: put_key(c, ("mcpServers", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcpServers", "agentprint")),
        ),
        BlockEdit(config_dir(home, env) / "GEMINI.md", "instructions", INSTRUCTIONS),
    ]


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    if payload.get("tool_name") != "run_shell_command":
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    cwd = cwd_from(payload.get("cwd"), env.get("GEMINI_CWD"), env.get("GEMINI_PROJECT_DIR"))
    dir_path = tool_input.get("dir_path")
    return HookCall(command, cwd / dir_path if isinstance(dir_path, str) and dir_path else cwd)


def reply(verdict: Verdict | None) -> HookReply:
    out: dict = {}
    if verdict and verdict.block:
        out.update(decision="deny", reason=verdict.reason)
    if verdict and verdict.warning:
        out["systemMessage"] = verdict.warning
    return HookReply(stdout=json.dumps(out))  # "{}" = allow; Gemini expects JSON on stdout


INTEGRATION = Integration(
    name=NAME,
    label="Gemini CLI",
    docs=(
        "https://geminicli.com/docs/hooks/reference",
        "https://geminicli.com/docs/tools/mcp-server",
        "https://geminicli.com/docs/cli/gemini-md",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
)
