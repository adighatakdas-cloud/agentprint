"""GitHub Copilot CLI, run for real against the mock model (headless `copilot -p`).

Custom endpoint: BYOK via COPILOT_PROVIDER_BASE_URL / COPILOT_PROVIDER_API_KEY / COPILOT_MODEL
(OpenAI Chat Completions). "GitHub authentication is not required when using a custom
provider" (`copilot help providers`); COPILOT_OFFLINE keeps it from contacting GitHub at all.
https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
"""

import pytest

from tests.live import harness
from tests.live.runner import AgentCLI, assert_blocked, assert_ran, run, uninstall_leaves_nothing

pytestmark = pytest.mark.live

COPILOT = AgentCLI(
    name="copilot",
    cli="copilot",
    argv=lambda exe, prompt: [
        str(exe),
        "-p",
        prompt,
        "--allow-all-tools",
        "--no-ask-user",
        "--no-auto-update",
    ],  # fmt: skip
    env=lambda home, url: {
        "COPILOT_HOME": str(home / ".copilot"),
        "COPILOT_PROVIDER_BASE_URL": f"{url}/v1",
        "COPILOT_PROVIDER_API_KEY": harness.DUMMY_KEY,
        "COPILOT_MODEL": "mock-model",
        "COPILOT_OFFLINE": "true",
        "COPILOT_AUTO_UPDATE": "false",
    },
    folders=(".copilot",),
)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_leaky_action_is_blocked_and_the_model_sees_why(tmp_path, action):
    outcome = run(COPILOT, tmp_path, action, leaky=True)
    assert_blocked(outcome, action)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_clean_action_runs(tmp_path, action):
    outcome = run(COPILOT, tmp_path, action, leaky=False)
    assert_ran(outcome, action)
    assert uninstall_leaves_nothing(COPILOT, outcome.home) == []
