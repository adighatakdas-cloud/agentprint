"""GitHub Copilot CLI, run for real against the mock model (headless `copilot -p`).

Custom endpoint: BYOK via COPILOT_PROVIDER_BASE_URL / COPILOT_PROVIDER_API_KEY / COPILOT_MODEL
(OpenAI Chat Completions). "GitHub authentication is not required when using a custom
provider" (`copilot help providers`); COPILOT_OFFLINE keeps it from contacting GitHub at all.
https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
Path check: Copilot asks before a command touches a path outside the project and the temp
folder, and with no one to ask it refuses. agentprint runs from this checkout's virtualenv, so
`--allow-all-paths` ("disable file path verification") lets the model run it, as it would run
an agentprint on the person's PATH.
"""

import pytest

from tests.live import harness
from tests.live.runner import (
    AgentCLI,
    assert_blocked,
    assert_help_not_menu,
    assert_ran,
    assert_scan_output,
    run,
    shell_probe,
    uninstall_leaves_nothing,
)

pytestmark = pytest.mark.live

COPILOT = AgentCLI(
    name="copilot",
    cli="copilot",
    argv=lambda exe, prompt: [
        str(exe),
        "-p",
        prompt,
        "--allow-all-tools",
        "--allow-all-paths",
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


# What Copilot CLI's shell shows the commands it runs, as a person starts it from a terminal
# (harness.PROBE prints names only).
SHELL = {"markers": ["COPILOT_CLI"], "color": [], "stdin_tty": False, "stdout_tty": False}


def test_the_shell_shows_the_agent_marker(tmp_path):
    assert shell_probe(run(COPILOT, tmp_path, "probe", leaky=False)) == SHELL


def test_scan_output_is_plain_text_for_the_model(tmp_path):
    outcome = run(COPILOT, tmp_path, "scan", leaky=False)
    assert_scan_output(outcome, agent_format=bool(SHELL["markers"]))


def test_bare_agentprint_does_not_hang(tmp_path):
    outcome = run(COPILOT, tmp_path, "bare", leaky=False)
    assert_help_not_menu(outcome)
