"""Cursor. Checked against the official docs on 2026-11-05.

- Hook: `beforeShellExecution` in ~/.cursor/hooks.json (`{"version": 1, "hooks": {...}}`); the
  entry's `matcher` is a regex on the full command string, so agentprint only runs for commands
  that mention publish/upload/push. Input has `command` and `cwd`. Answer is JSON with
  `permission` ("allow" | "deny" | "ask") plus snake_case `user_message` / `agent_message`; exit
  0 with invalid or empty JSON blocks, other non-zero codes (not 2) fail open.
  https://cursor.com/docs/hooks
  Ruling: when nothing is wrong we answer "ask", never "allow": the docs don't say whether
  "allow" skips Cursor's own approval prompt, and a security tool must not remove one.
  Cursor also runs Claude Code hooks from ~/.claude/settings.json by default
  (https://cursor.com/docs/reference/third-party-hooks), so with both installed the check can
  run twice for a publish; that is slower, never weaker.
- MCP: `mcpServers` in ~/.cursor/mcp.json; `type: "stdio"`, `command` on PATH or absolute.
  https://cursor.com/docs/mcp
- Instructions: User Rules live in Cursor's settings (not a file); "user rule files in
  ~/.cursor/rules ... stay on the machine". agentprint writes ~/.cursor/rules/agentprint.mdc
  with `alwaysApply: true` (the documented project-rule format; not documented for the user
  folder). https://cursor.com/help/customization/rules
"""

from __future__ import annotations

from pathlib import Path

from ..guard import Verdict
from .base import (
    INSTRUCTIONS,
    Edit,
    Env,
    FileEdit,
    HookCall,
    HookReply,
    Integration,
    JsonEdit,
    Launch,
    cwd_from,
    drop_hook,
    drop_key,
    json_reply,
    mcp_entry,
    put_hook,
    put_key,
)

NAME = "cursor"
MATCHER = (
    r"\b(npm|pnpm|yarn|bun|uv|uvx|poetry|hatch|flit)\b.*\bpublish\b"
    r"|\btwine\b.*\bupload\b|\bgit\b.*\bpush\b"
)
RULE = f"""\
---
description: agentprint security check before publishing a package or pushing
alwaysApply: true
---
<!-- Written by `agentprint install`; `agentprint uninstall` deletes this file. -->

{INSTRUCTIONS}"""


def config_dir(home: Path, env: Env) -> Path:
    return home / ".cursor"


def _add_hook(config: dict, entry: dict) -> None:
    config.setdefault("version", 1)
    put_hook(config, "beforeShellExecution", entry)


def _drop_hook(config: dict) -> None:
    drop_hook(config, "beforeShellExecution")
    if set(config) == {"version"}:
        config.clear()


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    entry = {"command": launch.hook_command(NAME), "timeout": 60, "matcher": MATCHER}
    server = mcp_entry(launch, type="stdio")
    return [
        JsonEdit(
            base / "hooks.json",
            "hook: check publish/push before a shell command runs",
            add=lambda c: _add_hook(c, entry),
            remove=_drop_hook,
        ),
        JsonEdit(
            base / "mcp.json",
            "MCP server",
            add=lambda c: put_key(c, ("mcpServers", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcpServers", "agentprint")),
        ),
        FileEdit(base / "rules" / "agentprint.mdc", "instructions (user rule)", RULE),
    ]


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    command = payload.get("command")
    if not isinstance(command, str):
        return None
    roots = payload.get("workspace_roots") or [None]
    return HookCall(command, cwd_from(payload.get("cwd"), roots[0]))


def reply(verdict: Verdict | None) -> HookReply:
    if verdict and verdict.block:
        verdict_line = verdict.reason.splitlines()[0]
        return json_reply(
            {
                "permission": "deny",
                "user_message": f"{verdict_line}. The agent was told how to fix it.",
                "agent_message": verdict.reason,
            }
        )
    message = verdict.warning if verdict and verdict.warning else None
    if message is None and verdict is not None:
        message = "agentprint checked this command: OK"
    out: dict = {"permission": "ask"}
    if message:
        out["user_message"] = message
    return json_reply(out)


INTEGRATION = Integration(
    name=NAME,
    label="Cursor",
    docs=("https://cursor.com/docs/hooks", "https://cursor.com/docs/mcp"),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
)
