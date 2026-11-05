"""Gemini CLI, run for real against the mock model (headless `gemini -p`).

Custom endpoint: GOOGLE_GEMINI_BASE_URL (the mock speaks the Gemini API) with a dummy
GEMINI_API_KEY; no Google login. Headless runs refuse untrusted folders unless `--skip-trust`.
https://geminicli.com/docs/reference/configuration , https://geminicli.com/docs/cli/trusted-folders
"""

import json

import pytest

from tests.live import harness
from tests.live.runner import AgentCLI, assert_blocked, assert_ran, run, uninstall_leaves_nothing

pytestmark = pytest.mark.live


def _setup(home, url):
    settings = home / ".gemini" / "settings.json"  # also how agentprint knows Gemini CLI is here
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "security": {"auth": {"selectedType": "gemini-api-key"}},  # a key, not a Google login
        "privacy": {"usageStatisticsEnabled": False},
        "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
    }
    settings.write_text(json.dumps(data, indent=2) + "\n")


GEMINI = AgentCLI(
    name="gemini",
    cli="gemini",
    argv=lambda exe, prompt: [
        str(exe),
        "-p",
        prompt,
        "--approval-mode=yolo",
        "--skip-trust",
        "-m",
        "mock-model",
    ],
    env=lambda home, url: {
        "GOOGLE_GEMINI_BASE_URL": url,
        "GEMINI_API_KEY": harness.DUMMY_KEY,
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
    },
    setup=_setup,
)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_leaky_action_is_blocked_and_the_model_sees_why(tmp_path, action):
    outcome = run(GEMINI, tmp_path, action, leaky=True)
    assert_blocked(outcome, action)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_clean_action_runs(tmp_path, action):
    outcome = run(GEMINI, tmp_path, action, leaky=False)
    assert_ran(outcome, action)
    assert uninstall_leaves_nothing(GEMINI, outcome.home) == []
