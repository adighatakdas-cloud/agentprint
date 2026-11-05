"""`agentprint guard`: read an agent's hook payload, answer in that agent's native format.

Fails open everywhere: bad input, an unknown agent or a bug in agentprint lets the command run,
with a warning where the agent can show one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from .guard import Verdict, evaluate
from .integrations import ALL_INTEGRATIONS
from .integrations.base import HookReply
from .report import FailOn

__all__ = ["HookReply", "run_hook"]


def run_hook(
    agent: str, raw: str, env: Mapping[str, str], fail_on: FailOn = FailOn.high
) -> HookReply:
    integration = ALL_INTEGRATIONS.get(agent)
    if integration is None:
        # Exit 0 = allow everywhere; non-zero means "deny" to some agents (GitHub Copilot).
        return HookReply(stderr=f"agentprint: unknown agent {agent!r}; command allowed")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
    except ValueError:
        return integration.reply(
            Verdict(False, warning="agentprint could not read the hook input; command allowed.")
        )
    try:
        call = integration.parse_hook(payload, env)
        verdict = None if call is None else evaluate(call.command, call.cwd, fail_on)
        return integration.reply(verdict)
    except Exception as exc:  # noqa: BLE001 - never block work because of an agentprint bug
        warning = (
            f"agentprint hit an internal error ({type(exc).__name__}) and allowed the command "
            "without checking it."
        )
        try:
            return integration.reply(Verdict(False, warning=warning))
        except Exception:  # noqa: BLE001
            return HookReply(stderr=warning)
