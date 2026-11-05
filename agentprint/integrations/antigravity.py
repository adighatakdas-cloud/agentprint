"""Google Antigravity (app, IDE and CLI share ~/.gemini/config). Checked 2026-11-05.

Gemini CLI's individual users moved to Antigravity CLI on 2026-06-18
(https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/).

- Hook: ~/.gemini/config/hooks.json maps a hook-set name to events:
  `{"agentprint": {"PreToolUse": [{"matcher": "run_command", "hooks": [{"type": "command",
  "command": ..., "timeout": ...}]}]}}`. Input: `toolCall.name` ("run_command"),
  `toolCall.args.CommandLine`, `toolCall.args.Cwd`, `workspacePaths`. Block = stdout
  `{"decision": "deny", "reason": ...}`; `"allow"` defers to the next hook or the default policy
  (it does not approve anything); exit codes are not documented, so agentprint answers in JSON
  only. Hooks in ~/.gemini/antigravity-cli/settings.json are not loaded.
  https://antigravity.google/docs/hooks/
- MCP: `mcpServers` in ~/.gemini/config/mcp_config.json ("applies to all your projects").
  https://antigravity.google/docs/mcp/
- Instructions: ~/.gemini/config/rules/agentprint.md, a file of agentprint's own; global rules
  files there need `trigger:` frontmatter. https://antigravity.google/docs/rules/
- Ruling (shared ~/.gemini): Gemini CLI's integration writes ~/.gemini/settings.json and
  ~/.gemini/GEMINI.md; Antigravity's writes only under ~/.gemini/config/. No file is shared, so
  installing or uninstalling one never touches the other's. Antigravity also reads
  ~/.gemini/GEMINI.md, so with both installed it sees the note twice; that is harmless.
- Found by ~/.gemini/antigravity, ~/.gemini/antigravity-cli, ~/.gemini/antigravity-ide or
  ~/.gemini/config.
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
    drop_key,
    json_reply,
    mcp_entry,
    put_key,
)

NAME = "antigravity"
RULE = f"""\
---
trigger: always_on
---
<!-- Written by `agentprint install`; `agentprint uninstall` deletes this file. -->

{INSTRUCTIONS}"""


def _gemini(home: Path, env: Env) -> Path:
    root = Path(env["GEMINI_CLI_HOME"]) if env.get("GEMINI_CLI_HOME") else home
    return root / ".gemini"


def config_dir(home: Path, env: Env) -> Path:
    return _gemini(home, env) / "config"


def found_by(home: Path, env: Env) -> list[Path]:
    gemini = _gemini(home, env)
    names = ("antigravity", "antigravity-cli", "antigravity-ide", "config")
    return [gemini / name for name in names]


def edits(home: Path, env: Env, launch: Launch) -> list[Edit]:
    base = config_dir(home, env)
    hook_set = {
        "PreToolUse": [
            {
                "matcher": "run_command",
                "hooks": [
                    {
                        "type": "command",
                        "command": launch.hook_command(NAME, reply(None).stdout),
                        "timeout": 60,
                    }
                ],
            }
        ]
    }
    server = mcp_entry(launch)
    return [
        JsonEdit(
            base / "hooks.json",
            "hook: check publish/push before run_command",
            add=lambda c: put_key(c, ("agentprint",), hook_set),
            remove=lambda c: drop_key(c, ("agentprint",)),
        ),
        JsonEdit(
            base / "mcp_config.json",
            "MCP server",
            add=lambda c: put_key(c, ("mcpServers", "agentprint"), server),
            remove=lambda c: drop_key(c, ("mcpServers", "agentprint")),
        ),
        FileEdit(base / "rules" / "agentprint.md", "instructions (global rule)", RULE),
    ]


def parse_hook(payload: dict, env: Env) -> HookCall | None:
    call = payload.get("toolCall") or {}
    if call.get("name") != "run_command":
        return None
    args = call.get("args") or {}
    command = args.get("CommandLine")
    if not isinstance(command, str):
        return None
    roots = payload.get("workspacePaths") or [None]
    return HookCall(command, cwd_from(args.get("Cwd"), roots[0]))


def reply(verdict: Verdict | None) -> HookReply:
    if verdict and verdict.block:
        return json_reply({"decision": "deny", "reason": verdict.reason})
    out: dict = {"decision": "allow"}  # defers to Antigravity's own policy; approves nothing
    if verdict and verdict.warning:
        out["reason"] = verdict.warning
    return json_reply(out)


INTEGRATION = Integration(
    name=NAME,
    label="Antigravity (Google)",
    docs=(
        "https://antigravity.google/docs/hooks/",
        "https://antigravity.google/docs/mcp/",
        "https://antigravity.google/docs/rules/",
    ),
    config_dir=config_dir,
    edits=edits,
    parse_hook=parse_hook,
    reply=reply,
    found_by=found_by,
)
