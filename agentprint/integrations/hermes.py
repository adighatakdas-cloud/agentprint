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
  The hook command is the stable launcher path, so an approval survives agentprint upgrades,
  but not a switch of strict mode: `--strict` is part of the command, so turning it on or off
  makes a new (event, command) that Hermes asks about again.
- MCP: `mcp_servers: {agentprint: {command, args, enabled}}` in the same config.yaml.
  https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference
- Ruling (YAML): config.yaml is edited with a round-trip YAML parser (ruamel.yaml). The user's
  own lines keep their text, comments, indentation, CRLF line ends and BOM (tested for the
  common layouts); agentprint's entries go first in each section so comments written after the
  user's entries stay after them. agentprint refuses (prints the snippet instead) when the
  section it would write into is shared through a YAML anchor, alias or merge (&, *, <<), when
  anchors expand the file past a size cap, or when the file can't be parsed. Cost: rare layouts
  (flow-style sections, unusual comment placement) can still be tidied near agentprint's lines.
- Ruling (instructions): none. The only global instructions file is ~/.hermes/SOUL.md, the
  agent's persona ("Global personality and tone", injected verbatim as slot #1; Hermes writes a
  default one when it is missing). Creating it would replace that default persona, and security
  rules don't belong in a persona. The MCP server's own instructions and the hook carry the
  same guidance. https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files
- HERMES_HOME replaces ~/.hermes. https://hermes-agent.nousresearch.com/docs/user-guide/configuration/
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..guard import Verdict
from .base import (
    Edit,
    Env,
    HookCall,
    HookReply,
    Integration,
    Launch,
    Unsupported,
    YamlEdit,
    cwd_from,
    drop_hook,
    drop_key,
    is_ours,
    json_reply,
    mcp_entry,
)

NAME = "hermes"
# Ruling: never create config.yaml. It's unverified whether Hermes still applies its own
# defaults when a config.yaml appears holding only agentprint's keys, and silently changing how
# Hermes behaves is worse than a missing hook. Cost: one extra run of install after Hermes's.
NO_CONFIG = (
    "Hermes hasn't created its config.yaml yet: run Hermes once (or `hermes config`) so it "
    "makes one, then run agentprint install again"
)


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
            add=lambda c: _add_hook(c, entry),
            remove=lambda c: drop_hook(c, "pre_tool_call"),
            missing=NO_CONFIG,
        ),
        YamlEdit(
            config,
            "MCP server",
            add=lambda c: _add_server(c, server),
            remove=lambda c: drop_key(c, ("mcp_servers", "agentprint")),
            missing=NO_CONFIG,
        ),
    ]


# ---- YAML sections agentprint may write into --------------------------------------------------


def _shared(node: object) -> bool:
    """True for a YAML node defined with an anchor (&x), used through an alias (*x) or merging
    another mapping (<<: *x). Writing into it would change every place that shares it."""
    anchor = getattr(node, "anchor", None)
    return bool(getattr(anchor, "value", None)) or bool(getattr(node, "merge", None))


def _section(parent: Any, key: str, kind: type) -> Any:  # ruamel CommentedMap/CommentedSeq
    """parent[key] as a mapping (or list) agentprint may add to. A new one goes first in its
    parent, so comments the user wrote after their own entries stay after them."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if _shared(parent):
        raise Unsupported("the section it would go in is shared through a YAML anchor (&, *, <<)")
    fresh = CommentedMap() if kind is dict else CommentedSeq()
    if key not in parent:
        parent.insert(0, key, fresh) if hasattr(parent, "insert") else parent.update({key: fresh})
        return fresh
    value = parent[key]
    if value is None:  # `hooks:` with nothing (or only comments) under it
        parent[key] = fresh
        return fresh
    if not isinstance(value, kind):
        what = "a set of named settings" if kind is dict else "a list"
        raise Unsupported(f'its "{key}" value is not {what}')
    if _shared(value):
        raise Unsupported(f'its "{key}" section is shared through a YAML anchor (&, *, <<)')
    return value


def _add_hook(config: dict, entry: dict) -> None:
    entries = _section(_section(config, "hooks", dict), "pre_tool_call", list)
    ours = [i for i, item in enumerate(entries) if is_ours(item)]
    if not ours:
        entries.insert(0, entry)
        return
    entries[ours[0]] = entry
    for i in reversed(ours[1:]):
        del entries[i]


def _add_server(config: dict, server: dict) -> None:
    servers = _section(config, "mcp_servers", dict)
    if "agentprint" in servers or not hasattr(servers, "insert"):
        servers["agentprint"] = server
    else:
        servers.insert(0, "agentprint", server)


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
