"""`agentprint guard`: read an agent's hook payload, answer in that agent's native format.

Fails open everywhere unless strict mode is on (`AGENTPRINT_STRICT=1` or `--strict`): bad
input, an unknown agent or a bug in agentprint lets the command run, with a warning where the
agent can show one. Strict mode blocks those cases (not an unknown agent) instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .commands import detect
from .guard import Verdict, evaluate, strict_block, strict_requested
from .integrations import ALL_INTEGRATIONS
from .integrations.base import BLOCK_EXIT, HookReply
from .report import FailOn

__all__ = ["HookReply", "run_hook"]

NO_FOLDER = "the agent did not say which folder the command runs in"


def _folder_unknown(command: str, cwd: Path, strict: bool) -> Verdict | None:
    """The payload named no folder: a publish or push "could not be checked" (blocked in strict
    mode); any other command passes as usual."""
    if not detect(command, cwd):
        return None
    if strict:
        return strict_block(f"agentprint could not check this command: {NO_FOLDER}.")
    return Verdict(False, warning=f"agentprint could not check this command ({NO_FOLDER}).")


def run_hook(
    agent: str,
    raw: str,
    env: Mapping[str, str],
    fail_on: FailOn = FailOn.high,
    strict: bool = False,
) -> HookReply:
    integration = ALL_INTEGRATIONS.get(agent)
    if integration is None:
        # Exit 0 = allow everywhere; non-zero means "deny" to some agents (GitHub Copilot).
        return HookReply(stderr=f"agentprint: unknown agent {agent!r}; command allowed")
    strict = strict or strict_requested(env)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
    except ValueError:
        if strict:
            return integration.reply(strict_block("agentprint could not read the hook input."))
        return integration.reply(
            Verdict(False, warning="agentprint could not read the hook input; command allowed.")
        )
    try:
        call = integration.parse_hook(payload, env)
        if call is None:
            verdict = None
        elif not call.cwd_known:
            verdict = _folder_unknown(call.command, call.cwd, strict)
        else:
            verdict = evaluate(call.command, call.cwd, fail_on, strict)
        return integration.reply(verdict)
    except Exception as exc:  # noqa: BLE001 - never block work because of an agentprint bug
        if strict:
            try:
                return integration.reply(
                    strict_block(
                        f"agentprint hit an internal error ({type(exc).__name__}) and could not "
                        "check this command."
                    )
                )
            except Exception:  # noqa: BLE001
                # BLOCK_EXIT, which the hook launcher turns into exit 2, the agents' "block"; any
                # other code is read by the launcher as agentprint failing, and allowed.
                return HookReply(
                    stderr="agentprint: internal error (strict mode); command blocked",
                    exit_code=BLOCK_EXIT,
                )
        warning = (
            f"agentprint hit an internal error ({type(exc).__name__}) and allowed the command "
            "without checking it."
        )
        try:
            return integration.reply(Verdict(False, warning=warning))
        except Exception:  # noqa: BLE001
            return HookReply(stderr=warning)
