"""OpenAI Codex, run for real against the mock model (headless `codex exec`).

Custom endpoint: a `[model_providers.mock]` entry in ~/.codex/config.toml (Responses API) with a
dummy key in an env var. https://learn.chatgpt.com/docs/config-file/config-reference
Hook trust: Codex runs a new hook only after the user trusts it in /hooks; `codex exec
--dangerously-bypass-hook-trust` ("Run enabled hooks without requiring persisted hook trust for
this invocation ... for automation") is the documented way to run it unattended.
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


# What Codex's shell shows the commands it runs, started from a color terminal with color
# forced on (harness.color_terminal; harness.PROBE prints names only).
# Codex sets NO_COLOR itself; CODEX_SANDBOX_NETWORK_DISABLED only under a network-off sandbox,
# which this run bypasses.
SHELL = {
    "markers": ["CODEX_THREAD_ID"],
    "color": ["NO_COLOR", "FORCE_COLOR", "GITHUB_ACTIONS"],
    "stdin_tty": False,
    "stdout_tty": False,
}


def test_the_shell_shows_the_agent_marker(tmp_path):
    assert shell_probe(run(CODEX, tmp_path, "probe", leaky=False)) == SHELL


def test_scan_output_is_plain_text_for_the_model(tmp_path):
    outcome = run(CODEX, tmp_path, "scan", leaky=False)
    assert_scan_output(outcome, agent_format=bool(SHELL["markers"]))


def test_bare_agentprint_does_not_hang(tmp_path):
    outcome = run(CODEX, tmp_path, "bare", leaky=False)
    assert_help_not_menu(outcome)
