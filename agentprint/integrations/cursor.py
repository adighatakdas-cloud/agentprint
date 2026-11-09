"""Cursor. Checked against the official docs on 2026-11-05.

- Hook: `beforeShellExecution` in ~/.cursor/hooks.json (`{"version": 1, "hooks": {...}}`); the
  entry's `matcher` is a regex on the full command string, so agentprint only runs for commands
  that start a publish or push, or that run a file a publish can hide in (a script, a make,
  just or task recipe, `npm version`). Input has `command` and `cwd`. Answer is JSON with
  `permission` ("allow" | "deny" | "ask") plus snake_case `user_message` / `agent_message`;
  results merge as "deny wins over ask, ask wins over allow"; exit 0 with invalid or empty JSON
  blocks, exit 2 blocks, other non-zero codes fail open. https://cursor.com/docs/hooks
  Ruling (rechecked 2026-11-05): a clean result answers "allow". The docs don't say whether
  "allow" skips Cursor's own approval; Cursor staff replies on its forum say hook permissions
  and the allowlist/approval flow are independent, so "allow" never removes a prompt the user
  set up (forum.cursor.com threads 144244 "beforeShellExecution hook permissions (allow/ask)
  ignored - allow-list takes precedence" and 155434 "Hooks return ALLOW but MCP tool still
  requires manual approval").
  Answering "ask" instead put a prompt on every matched command. Cost if wrong: a Cursor
  version where hook "allow" auto-approves would skip the prompt for checked, clean publishes.
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
# Cursor matches this regex against the whole command line (it's JavaScript; this pattern uses
# only syntax shared with Python's `re`, which the tests use). The tool must sit where a
# command starts, so `git stash push`, `echo npm publish` and quoted text don't fire the hook.
_PROGRAM_START = (
    r"(?:^|[;&|({!`\n]|\$\(|\b(?:do|then|else|xargs|sudo|env|exec|eval|nohup|time|npx|pnpx|"
    r"bunx|uvx|corepack)\s|\btimeout\s+\S+\s|\bpython[0-9.]*\s+-m\s)\s*(?:\w+=\S*\s+)*"
)
_COMMAND_START = _PROGRAM_START + r"(?:\S*/)?"
_PUBLISHES = (
    r"(?:(?:npm|pnpm|yarn|bun)\b[^;&|\n]*?\b(?:publish|release|deploy|ship)\b"
    r"|(?:uv|poetry|hatch|flit)\b[^;&|\n]*?\bpublish\b"
    r"|twine\b[^;&|\n]*?\bupload\b"
    r"|(?:changeset|lerna)\b[^;&|\n]*?\bpublish\b"
    r"|(?:np|semantic-release|release-it)(?:\s|$)"
    r"|git(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+push\b)"
)
# Commands that can publish from a file the guard reads (Ruling X7): every form
# agentprint.following follows. make, just and task recipes, `npm version` hooks, and scripts
# run as ./x, `bash x`, `python x`, `tsx x`, `deno run x`, `uv run x`, `pdm run x`...
_RUNS_A_FILE = (
    r"(?:(?:g?make|just|go-task|task)(?:\s|$)"
    r"|(?:npm|pnpm|yarn|bun)(?:\s+-\S+)*\s+version\b"
    r"|\S*/\S"
    r"|(?:sh|bash|zsh|dash|ksh|fish|python[0-9.]*|node|nodejs|tsx|ts-node|bun|ruby|perl)"
    r"(?:\s+-\S+)*\s+[^-\s]"
    r"|deno\s+(?:run|eval)\b"
    r"|(?:source|\.)\s+\S"
    r"|(?:uv|poetry|pdm|pipenv|hatch|rye)\s+run\s+\S)"
)
MATCHER = rf"(?:{_COMMAND_START}{_PUBLISHES}|{_PROGRAM_START}{_RUNS_A_FILE})"
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
    entry = {
        "command": launch.hook_command(NAME, reply(None).stdout),
        "timeout": 60,
        "matcher": MATCHER,
    }
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
    out: dict = {"permission": "allow"}  # Cursor's own approval still applies (see above)
    if verdict and verdict.warning:
        out["user_message"] = verdict.warning
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
