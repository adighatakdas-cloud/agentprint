"""GitHub Copilot. Checked against the Copilot CLI docs on 2026-11-05.

Verified for the Copilot CLI. VS Code says its "Copilot" agent runs the same runtime and reads
~/.copilot too, but its shell tool name and payload there are not documented: unverified.

- Hook: a file of agentprint's own, ~/.copilot/hooks/agentprint.json
  (`{"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": ..., "timeoutSec": ...,
  "matcher": "bash"}]}}`; the matcher is anchored as ^(?:PATTERN)$ against `toolName`). Input is
  camelCase: `toolName` ("bash"), `toolArgs` (its shape isn't documented: read as a JSON string
  or an object with `command`), `cwd`. Block = exit 0 + `{"permissionDecision": "deny",
  "permissionDecisionReason": ...}`. Exit 2 also denies and, unlike most agents, ANY other
  non-zero exit denies too; timeouts fail open. So allow is exit 0 with no output (never
  "allow", which would skip the user's own approval), and the launcher exits 0 on failure.
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference
- MCP: `mcpServers` in ~/.copilot/mcp-config.json, entries `{"type": "local", "command",
  "args", "tools": ["*"]}`; VS Code reads this file too ("GitHub Copilot CLI discovery is on by
  default").
  https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
  https://code.visualstudio.com/docs/agents/reference/mcp-configuration
- Instructions: ~/.copilot/copilot-instructions.md, "personal custom instructions that apply to
  all your sessions". COPILOT_HOME replaces ~/.copilot.
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
- Ruling (Copilot + Claude Code): Copilot also runs hooks from a *repository's*
  .claude/settings.json, but agentprint only writes the user-level ~/.claude/settings.json,
  which Copilot CLI doesn't read. In VS Code's older "Local" agent with `chat.useClaudeHooks`
  on, both hooks can run for one command: both give the same verdict and "the most
  restrictive wins", so the agent sees the same BLOCK twice at worst. Cost: one duplicate check.
- Windows: a hook's `powershell` field is the "Shell command for Windows" (`bash` is for Unix),
  and the CLI's Windows shell tool is `powershell` ("Execute shell commands (Windows)"), so the
  Windows entry has `powershell` and matches `bash|powershell` (same page, checked 2026-11-07).
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
    FileEdit,
    HookCall,
    HookReply,
    Integration,
    JsonEdit,
    Launch,
    cwd_from,
    drop_key,
    json_reply,
    mcp_entry,
    put_key,
)

NAME = "copilot"
SHELL_TOOLS = {"bash", "Bash", "powershell"}  # powershell: the shell tool on Windows


def config_dir(home: Path, env: Env) -> Path:
    return Path(env["COPILOT_HOME"]) if env.get("COPILOT_HOME") else home / ".copilot"


def _hook_file(launch: Launch) -> str:
    if launch.windows:
        entry = {
            "type": "command",
            "powershell": launch.powershell_command(NAME),
            "timeoutSec": 60,
            "matcher": "bash|powershell",
        }
    else:
        entry = {
            "type": "command",
            "bash": launch.hook_command(NAME),
            "timeoutSec": 60,
            "matcher": "bash",
        }
    return json.dumps({"version": 1, "hooks": {"preToolUse": [entry]}}, indent=2) + "\n"


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    server = mcp_entry(launch, type="local")
    server["tools"] = ["*"]
    return [
        FileEdit(
            base / "hooks" / "agentprint.json",
            "hook: check publish/push before a shell command runs",
            _hook_file(launch),
        ),
        JsonEdit(
            base / "mcp-config.json",
            "MCP server",
            add=lambda c: put_key(c, ("mcpServers", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcpServers", "agentprint")),
        ),
        BlockEdit(base / "copilot-instructions.md", "instructions", INSTRUCTIONS),
    ]


def _command(args: object) -> str | None:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args or None  # a bare command line
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return args["command"]
    return None


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    tool = payload.get("toolName", payload.get("tool_name"))
    if tool not in SHELL_TOOLS:
        return None
    command = _command(payload.get("toolArgs", payload.get("tool_input")))
    if command is None:
        return None
    return HookCall(command, cwd_from(payload.get("cwd")))


def reply(verdict: Verdict | None) -> HookReply:
    if verdict and verdict.block:
        out = {"permissionDecision": "deny", "permissionDecisionReason": verdict.reason}
        return HookReply(stdout=json_reply(out).stdout, stderr=verdict.warning or "")
    return HookReply(stderr=verdict.warning if verdict and verdict.warning else "")


INTEGRATION = Integration(
    name=NAME,
    label="GitHub Copilot",
    docs=(
        "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference",
        "https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers",
        "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
    windows_hooks=True,
)
