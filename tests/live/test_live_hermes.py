"""Hermes Agent, run for real against the mock model (headless `hermes chat -q`).

Custom endpoint: `model: {provider: custom, base_url, default, api_key}` in ~/.hermes/config.yaml
(an OpenAI-compatible Chat Completions server). Hermes asks before running a new hook; for an
unattended run `hermes chat --accept-hooks` ("Auto-approve any unseen shell hooks declared in
config.yaml without a TTY prompt") is the documented way. agentprint itself never approves it.
https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
"""

import pytest

from tests.live import harness
from tests.live.runner import AgentCLI, assert_blocked, assert_ran, run, uninstall_leaves_nothing

pytestmark = pytest.mark.live


def _setup(home, url):
    config = home / ".hermes" / "config.yaml"  # Hermes has made its config: agentprint needs it
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "# Hermes settings (test)\n"
        "model:\n"
        "  provider: custom\n"
        f"  base_url: {url}/v1\n"
        "  default: mock-model\n"
        f"  api_key: {harness.DUMMY_KEY}\n"
        "  api_mode: chat_completions\n"
    )


HERMES = AgentCLI(
    name="hermes",
    cli="hermes",
    argv=lambda exe, prompt: [
        str(exe),
        "chat",
        "-q",
        prompt,
        "-Q",
        "--accept-hooks",
        "--yolo",
        "--provider",
        "custom",
        "--max-turns",
        "3",
    ],  # fmt: skip
    env=lambda home, url: {"HERMES_HOME": str(home / ".hermes")},
    setup=_setup,
    folders=(".hermes",),
)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_leaky_action_is_blocked_and_the_model_sees_why(tmp_path, action):
    outcome = run(HERMES, tmp_path, action, leaky=True)
    assert_blocked(outcome, action)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_clean_action_runs(tmp_path, action):
    outcome = run(HERMES, tmp_path, action, leaky=False)
    assert_ran(outcome, action)
    # shell-hooks-allowlist.json is Hermes's own record that the hook was approved; agentprint
    # never writes or edits it (`hermes hooks revoke` is how a user forgets an approval).
    left = uninstall_leaves_nothing(HERMES, outcome.home)
    assert left in ([], [".hermes/shell-hooks-allowlist.json"])
