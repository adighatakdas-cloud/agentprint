"""Hermes Agent (Nous Research). Checked against the official docs on 2026-11-05.

- Hook: a shell hook under `hooks: pre_tool_call:` in ~/.hermes/config.yaml, matcher
  "terminal" (a regex on the tool name); `command` runs via shlex.split, shell=False. Input:
  `hook_event_name`, `tool_name`, `tool_input` (`{"command": ...}` for terminal), `cwd`, ...
  Block = stdout `{"decision": "block", "reason": ...}` (or exit 2); allow = exit 0 with empty
  output; other exits, bad output and timeouts fail open (no `fail_closed`).
  https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
  Consent: Hermes asks the user to approve each new (event, command) the first time, in a
  foreground `hermes chat`; gateway, cron, ACP and CI runs never ask, so the hook stays off
  there until approved. agentprint never writes Hermes's approval list itself
  (shell-hooks-allowlist.json): approving code that runs on every command is the human's call.
  The hook command is the stable launcher path, so an approval survives agentprint upgrades.
- MCP: `mcp_servers: {agentprint: {command, args, enabled}}` in the same config.yaml.
  https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference
- Ruling (YAML): config.yaml is edited with a round-trip YAML parser (ruamel.yaml), which keeps
  the user's comments, key order and quoting; a file it can't parse is left alone and the
  snippet is printed. Cost: a few unusual layouts can be re-indented on the lines near ours.
- Ruling (instructions): none. The only global instructions file is ~/.hermes/SOUL.md, the
  agent's persona ("Global personality and tone", injected verbatim as slot #1; Hermes writes a
  default one when it is missing). Creating it would replace that default persona, and security
  rules don't belong in a persona. The MCP server's own instructions and the hook carry the
  same guidance. https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files
- HERMES_HOME replaces ~/.hermes. https://hermes-agent.nousresearch.com/docs/user-guide/configuration/
"""

from __future__ import annotations

from pathlib import Path

from ..guard import Verdict
from .base import (
    Edit,
    Env,
    HookCall,
    HookReply,
    Integration,
    Launch,
    YamlEdit,
    cwd_from,
    drop_hook,
    drop_key,
    json_reply,
    mcp_entry,
    put_hook,
    put_key,
)

NAME = "hermes"


def config_dir(home: Path, env: Env) -> Path:
    return Path(env["HERMES_HOME"]) if env.get("HERMES_HOME") else home / ".hermes"


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    config = config_dir(home, env) / "config.yaml"
    entry = {"matcher": "terminal", "command": launch.hook_command(NAME), "timeout": 60}
    server = mcp_entry(launch)
    server["enabled"] = True
    return [
        YamlEdit(
            config,
            "hook: check publish/push before the terminal tool runs (needs your approval)",
            add=lambda c: put_hook(c, "pre_tool_call", entry),
            remove=lambda c: drop_hook(c, "pre_tool_call"),
        ),
        YamlEdit(
            config,
            "MCP server",
            add=lambda c: put_key(c, ("mcp_servers", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcp_servers", "agentprint")),
        ),
    ]


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    if payload.get("tool_name") != "terminal":
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    return HookCall(command, cwd_from(tool_input.get("workdir"), payload.get("cwd")))


def reply(verdict: Verdict | None) -> HookReply:
    if verdict and verdict.block:
        out = json_reply({"decision": "block", "reason": verdict.reason})
        return HookReply(stdout=out.stdout, stderr=verdict.warning or "")
    return HookReply(stderr=verdict.warning if verdict and verdict.warning else "")


INTEGRATION = Integration(
    name=NAME,
    label="Hermes Agent",
    docs=(
        "https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks",
        "https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
    notes=(
        "the Hermes hook is inactive until you approve it. Run `hermes chat` once and accept "
        "the agentprint hook when Hermes asks (`hermes hooks list` shows it). Gateway and cron "
        "runs never ask, so the hook stays off there until you approve it in a chat.",
    ),
)
