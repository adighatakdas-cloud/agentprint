"""OpenAI Codex, run for real against the mock model (headless `codex exec`).

Custom endpoint: a `[model_providers.mock]` entry in ~/.codex/config.toml (Responses API) with a
dummy key in an env var. https://learn.chatgpt.com/docs/config-file/config-reference
Hook trust: Codex runs a new hook only after the user trusts it in /hooks; `codex exec
--dangerously-bypass-hook-trust` ("Run enabled hooks without requiring persisted hook trust for
this invocation ... for automation") is the documented way to run it unattended.
"""

import pytest

from tests.live import harness
from tests.live.runner import AgentCLI, assert_blocked, assert_ran, run, uninstall_leaves_nothing

pytestmark = pytest.mark.live


def _setup(home, url):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'model = "mock-model"\n'
        'model_provider = "mock"\n\n'
        "[model_providers.mock]\n"
        'name = "Mock"\n'
        f'base_url = "{url}/v1"\n'
        'env_key = "MOCK_API_KEY"\n'
        'wire_api = "responses"\n'
    )


CODEX = AgentCLI(
    name="codex",
    cli="codex",
    argv=lambda exe, prompt: [
        str(exe),
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-hook-trust",
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    ],
    env=lambda home, url: {
        "CODEX_HOME": str(home / ".codex"),
        "MOCK_API_KEY": harness.DUMMY_KEY,
    },
    setup=_setup,
    folders=(".codex",),
)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_leaky_action_is_blocked_and_the_model_sees_why(tmp_path, action):
    outcome = run(CODEX, tmp_path, action, leaky=True)
    assert_blocked(outcome, action)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_clean_action_runs(tmp_path, action):
    outcome = run(CODEX, tmp_path, action, leaky=False)
    assert_ran(outcome, action)
    assert uninstall_leaves_nothing(CODEX, outcome.home) == []
