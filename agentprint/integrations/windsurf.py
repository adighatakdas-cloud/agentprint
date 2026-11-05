"""Windsurf, and its successor Devin Desktop (Devin Local / Devin CLI). Checked 2026-11-05.

Since 2 Jun 2026 Windsurf ships as Devin Desktop; new conversations use Devin Local, which takes
Devin CLI's config format, and Cascade is legacy (https://docs.devin.ai/desktop/devin-desktop-faq,
https://docs.devin.ai/desktop/devin-local/customizations). Both are supported:

Windsurf / Cascade (~/.codeium/windsurf), docs archived at
https://web.archive.org/web/20260516140338/https://docs.windsurf.com/windsurf/cascade/hooks.md
- Hook: `pre_run_command` in ~/.codeium/windsurf/hooks.json; input `tool_info.command_line` and
  `tool_info.cwd`. Exit code 2 blocks and the agent sees stderr; any other code proceeds.
- MCP: `mcpServers` in ~/.codeium/windsurf/mcp_config.json.
- Instructions: ~/.codeium/windsurf/memories/global_rules.md.

Devin Local / Devin CLI (~/.config/devin)
- Hook: PreToolUse with matcher "^exec$" under `hooks` in ~/.config/devin/config.json; input
  `tool_name` "exec" and `tool_input.command`, no cwd field (DEVIN_PROJECT_DIR is set). Block =
  `{"decision": "block", "reason": ...}` ("shown to the agent"); allow = exit 0, no output (never
  "approve", which would skip the user's approval). Other exit codes are logged, never block.
  https://docs.devin.ai/cli/extensibility/hooks/overview ,
  https://docs.devin.ai/cli/extensibility/hooks/lifecycle-hooks
- MCP: `mcpServers` in ~/.config/devin/mcp_config.json.
  https://docs.devin.ai/cli/extensibility/mcp/configuration
- Instructions: ~/.config/devin/AGENTS.md. https://docs.devin.ai/cli/extensibility/rules
- Devin's config files allow comments; a file with comments is left alone and the snippet is
  printed for the user to paste.
"""

from __future__ import annotations

from pathlib import Path

from ..guard import Verdict
from .base import (
    BLOCK_EXIT,
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
    json_reply,
    mcp_entry,
    put_hook,
    put_key,
)

WINDSURF = "windsurf"
DEVIN = "devin"


def _mcp(path: Path, launch: Launch) -> JsonEdit:
    server = mcp_entry(launch)
    return JsonEdit(
        path,
        "MCP server",
        add=lambda c: put_key(c, ("mcpServers", "agentprint"), server),
        remove=lambda c: drop_key(c, ("mcpServers", "agentprint")),
    )


# ---- Windsurf (Cascade) -----------------------------------------------------------------------


def windsurf_dir(home: Path, env: Env) -> Path:
    return home / ".codeium" / "windsurf"


def windsurf_edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = windsurf_dir(home, env)
    entry = {"command": launch.hook_command(WINDSURF), "show_output": True}
    return [
        JsonEdit(
            base / "hooks.json",
            "hook: check publish/push before a command runs",
            add=lambda c: put_hook(c, "pre_run_command", entry),
            remove=lambda c: drop_hook(c, "pre_run_command"),
        ),
        _mcp(base / "mcp_config.json", launch),
        BlockEdit(base / "memories" / "global_rules.md", "instructions", INSTRUCTIONS),
    ]


def windsurf_parse(payload: dict, env: Env) -> HookCall | None:
    if payload.get("agent_action_name") != "pre_run_command":
        return None
    info = payload.get("tool_info") or {}
    command = info.get("command_line")
    if not isinstance(command, str):
        return None
    return HookCall(command, cwd_from(info.get("cwd")))


def windsurf_reply(verdict: Verdict | None) -> HookReply:
    if verdict and verdict.block:
        # The launcher turns BLOCK_EXIT into exit 2, Cascade's "block"; the agent sees stderr.
        return HookReply(stderr=verdict.reason, stdout=verdict.warning or "", exit_code=BLOCK_EXIT)
    return HookReply(stdout=verdict.warning if verdict and verdict.warning else "")


WINDSURF_INTEGRATION = Integration(
    name=WINDSURF,
    label="Windsurf (Cascade)",
    docs=(
        "https://web.archive.org/web/20260516140338/https://docs.windsurf.com/windsurf/cascade/hooks.md",
    ),
    config_dir=windsurf_dir,
    edits=windsurf_edits,
    parse_hook=windsurf_parse,
    reply=windsurf_reply,
)


# ---- Devin Desktop / Devin CLI ----------------------------------------------------------------


def devin_dir(home: Path, env: Env) -> Path:
    root = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    return root / "devin"


def devin_edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = devin_dir(home, env)
    entry = {
        "matcher": "^exec$",
        "hooks": [{"type": "command", "command": launch.hook_command(DEVIN), "timeout": 60}],
    }
    return [
        JsonEdit(
            base / "config.json",
            "hook: check publish/push before exec runs a command",
            add=lambda c: put_hook(c, "PreToolUse", entry),
            remove=lambda c: drop_hook(c, "PreToolUse"),
        ),
        _mcp(base / "mcp_config.json", launch),
        BlockEdit(base / "AGENTS.md", "instructions", INSTRUCTIONS),
    ]


def devin_parse(payload: dict, env: Env) -> HookCall | None:
    if payload.get("tool_name") != "exec":
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    cwd = cwd_from(tool_input.get("cwd"), tool_input.get("workdir"), env.get("DEVIN_PROJECT_DIR"))
    return HookCall(command, cwd)


def devin_reply(verdict: Verdict | None) -> HookReply:
    if verdict and verdict.block:
        reply = json_reply({"decision": "block", "reason": verdict.reason})
        return HookReply(stdout=reply.stdout, stderr=verdict.warning or "")
    return HookReply(stderr=verdict.warning if verdict and verdict.warning else "")


DEVIN_INTEGRATION = Integration(
    name=DEVIN,
    label="Devin Desktop / Devin CLI (Windsurf's successor)",
    docs=(
        "https://docs.devin.ai/cli/extensibility/hooks/overview",
        "https://docs.devin.ai/cli/extensibility/mcp/configuration",
        "https://docs.devin.ai/cli/extensibility/rules",
    ),
    config_dir=devin_dir,
    edits=devin_edits,
    parse_hook=devin_parse,
    reply=devin_reply,
)
