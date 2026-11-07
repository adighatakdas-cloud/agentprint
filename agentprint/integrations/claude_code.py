"""Claude Code (Anthropic). Checked against the official docs on 2026-11-05.

- Hook: PreToolUse with matcher "Bash" in ~/.claude/settings.json. Input has `tool_name`,
  `tool_input.command` and `cwd`. Block = exit 0 + JSON `hookSpecificOutput.permissionDecision:
  "deny"` with `permissionDecisionReason` ("shown to Claude"); allow = exit 0, no output (we never
  return "allow", which would skip the user's own permission prompt). `systemMessage` warns the
  user. Other exit codes are non-blocking errors. https://code.claude.com/docs/en/hooks
- MCP: user-scope servers live in ~/.claude.json (`mcpServers`; the docs name the file, the
  top-level key matches what `claude mcp add --scope user` writes). Entries without `type` are
  stdio. https://code.claude.com/docs/en/mcp
- Instructions: ~/.claude/CLAUDE.md, loaded in every session. https://code.claude.com/docs/en/memory
- CLAUDE_CONFIG_DIR moves ~/.claude and .claude.json. https://code.claude.com/docs/en/claude-directory
- Windows (hooks page, checked 2026-11-07): a hook with `"shell": "powershell"` runs its command
  in PowerShell whether or not Git Bash is installed. Without Git Bash, Claude Code registers
  only its PowerShell tool (input `tool_input.command`, like Bash), so a shell hook matches
  `Bash|PowerShell`; and any exit code other than 0 or 2 counts as a hook failure.
"""

from __future__ import annotations

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
    json_reply,
    mcp_entry,
    put_hook,
    put_key,
)

NAME = "claude-code"
SHELL_TOOLS = ("Bash", "PowerShell")  # PowerShell: Windows, where it can be the only shell tool


def config_dir(home: Path, env: Env) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude"


def _state_file(home: Path, env: Env) -> Path:
    if env.get("CLAUDE_CONFIG_DIR"):
        return Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return home / ".claude.json"


def _hook(launch: Launch) -> dict:
    if launch.windows:
        command = launch.powershell_command(NAME)
        handler = {"type": "command", "command": command, "shell": "powershell", "timeout": 60}
        return {"matcher": "|".join(SHELL_TOOLS), "hooks": [handler]}
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": launch.hook_command(NAME), "timeout": 60}],
    }


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    entry = _hook(launch)
    server = mcp_entry(launch, type="stdio")
    return [
        JsonEdit(
            base / "settings.json",
            "hook: check publish/push before Bash runs it",
            add=lambda c: put_hook(c, "PreToolUse", entry),
            remove=lambda c: drop_hook(c, "PreToolUse"),
        ),
        JsonEdit(
            _state_file(home, env),
            "MCP server (user scope)",
            add=lambda c: put_key(c, ("mcpServers", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcpServers", "agentprint")),
        ),
        BlockEdit(base / "CLAUDE.md", "instructions", INSTRUCTIONS),
    ]


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    if payload.get("tool_name") not in SHELL_TOOLS:
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return None
    return HookCall(command, cwd_from(payload.get("cwd"), env.get("CLAUDE_PROJECT_DIR")))


def deny_reply(verdict: Verdict | None) -> dict:
    """Claude Code's PreToolUse answer; Codex accepts the same shape."""
    out: dict = {}
    if verdict and verdict.block:
        out["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": verdict.reason,
        }
    if verdict and verdict.warning:
        out["systemMessage"] = verdict.warning
    return out


def reply(verdict: Verdict | None) -> HookReply:
    return json_reply(deny_reply(verdict))


INTEGRATION = Integration(
    name=NAME,
    label="Claude Code",
    docs=(
        "https://code.claude.com/docs/en/hooks",
        "https://code.claude.com/docs/en/mcp",
        "https://code.claude.com/docs/en/memory",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
    windows_hooks=True,
)
