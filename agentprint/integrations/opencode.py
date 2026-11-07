"""OpenCode (v1 and v2). Checked against the official docs on 2026-11-05.

- Hook: OpenCode has no JSON hook config; it loads JS plugins from ~/.config/opencode/plugins/.
  A plugin blocks a tool call by throwing from `tool.execute.before` (v1, tool "bash",
  `output.args.command`) or `ctx.tool.hook("execute.before")` (v2, tool "shell",
  `event.input.command` / `workdir`). The file exports both shapes, which v2's docs describe
  for plugins that must run on v1 >= 1.18.29 too. The plugin pipes `{command, cwd}` to
  `agentprint guard --agent opencode` and throws only on a "block" answer; any failure lets the
  command run. https://opencode.ai/docs/plugins , https://opencode.ai/v2/docs/plugins/ ,
  https://opencode.ai/v2/docs/build/plugins/migrate-v1/ ,
  https://opencode.ai/v2/docs/build/plugins/server/
- MCP: `"mcp": {"agentprint": {"type": "local", "command": [...], "enabled": true}}` in
  ~/.config/opencode/opencode.json (v2 still reads this v1 shape). The file may be JSONC; one
  with comments is left alone. https://opencode.ai/docs/mcp-servers , https://opencode.ai/v2/docs/mcp-servers/
- Instructions: ~/.config/opencode/AGENTS.md. In v1, ~/.claude/CLAUDE.md is used only when that
  file does not exist (https://opencode.ai/docs/rules), so agentprint never creates it while a
  ~/.claude/CLAUDE.md exists: that would silently switch off the user's Claude rules in OpenCode.
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
    put_key,
)
from .claude_code import config_dir as claude_dir

NAME = "opencode"

PLUGIN = """\
// agentprint guard for OpenCode. Written by `agentprint install`; `agentprint uninstall`
// deletes it. Before a shell command runs, it asks agentprint whether the command publishes a
// package or pushes with problems. It blocks only on a "block" answer; if anything goes wrong
// the command runs as usual.
import { spawnSync } from "node:child_process"

const HOOK = __HOOK__

function guard(command, cwd) {
  if (typeof command !== "string" || !command) return
  let reply = {}
  try {
    const run = spawnSync(HOOK[0], HOOK.slice(1), {
      input: JSON.stringify({ command, cwd }),
      encoding: "utf8",
      timeout: 60000,
    })
    if (run.status !== 0 && run.stderr) console.warn(run.stderr.trim())
    reply = JSON.parse(run.stdout || "{}")
  } catch (err) {
    console.warn("agentprint: check failed, command allowed:", err && err.message)
    return
  }
  if (reply.warning) console.warn(reply.warning)
  if (reply.decision === "block") throw new Error(reply.reason || "Blocked by agentprint")
}

export default {
  id: "agentprint",
  // OpenCode v2
  async setup(ctx) {
    await ctx.tool.hook("execute.before", (event) => {
      if (event.tool !== "shell") return
      const input = event.input || {}
      guard(input.command, input.workdir || (ctx.location && ctx.location.directory))
    })
  },
  // OpenCode v1
  async server(ctx = {}) {
    return {
      "tool.execute.before": async (input, output) => {
        if (input.tool !== "bash") return
        const args = (output && output.args) || {}
        guard(args.command, args.workdir || ctx.directory)
      },
    }
  },
}
"""


def config_dir(home: Path, env: Env) -> Path:
    root = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    return root / "opencode"


def _config_file(base: Path) -> Path:
    for name in ("opencode.json", "opencode.jsonc"):
        if (base / name).is_file():
            return base / name
    return base / "opencode.json"


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    hook = [launch.hook, "--agent", NAME, *launch.hook_flags()]
    server = {"type": "local", "command": list(launch.mcp), "enabled": True}
    changes: list[Edit] = [
        FileEdit(
            base / "plugins" / "agentprint.js",
            "hook plugin: check publish/push before a shell command runs",
            PLUGIN.replace("__HOOK__", json.dumps(hook)),
        ),
        JsonEdit(
            _config_file(base),
            "MCP server",
            add=lambda c: put_key(c, ("mcp", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcp", "agentprint")),
        ),
    ]
    rules = base / "AGENTS.md"
    if rules.exists() or not (claude_dir(home, env) / "CLAUDE.md").exists():
        changes.append(BlockEdit(rules, "instructions", INSTRUCTIONS))
    return changes


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    command = payload.get("command")
    if not isinstance(command, str):
        return None
    return HookCall(command, cwd_from(payload.get("cwd")))


def reply(verdict: Verdict | None) -> HookReply:
    out: dict = {"decision": "block" if verdict and verdict.block else "allow"}
    if verdict and verdict.block:
        out["reason"] = verdict.reason
    if verdict and verdict.warning:
        out["warning"] = verdict.warning
    return json_reply(out)


INTEGRATION = Integration(
    name=NAME,
    label="OpenCode",
    docs=(
        "https://opencode.ai/docs/plugins",
        "https://opencode.ai/v2/docs/plugins/",
        "https://opencode.ai/docs/mcp-servers",
        "https://opencode.ai/docs/rules",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
)
