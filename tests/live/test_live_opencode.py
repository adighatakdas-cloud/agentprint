"""OpenCode, run for real against the mock model (headless `opencode run`).

Custom endpoint: a provider using `@ai-sdk/openai-compatible` with `options.baseURL` and a dummy
`apiKey` in ~/.config/opencode/opencode.json. https://opencode.ai/docs/providers
agentprint's hook here is its JS plugin in ~/.config/opencode/plugins/, so this test is the
one that proves the plugin loads and blocks inside OpenCode itself.
"""

import json

import pytest

from tests.live import harness
from tests.live.runner import (
    AgentCLI,
    assert_blocked,
    assert_help_not_menu,
    assert_ran,
    assert_scan_output,
    run,
    shared_cache,
    shell_probe,
    uninstall_leaves_nothing,
)

pytestmark = pytest.mark.live


def _setup(home, url):
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Mock",
        "options": {"baseURL": f"{url}/v1", "apiKey": harness.DUMMY_KEY},
        "models": {"mock-model": {"name": "Mock model", "tool_call": True}},
    }
    data = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"mock": provider},
        "model": "mock/mock-model",
        "permission": {"bash": "allow"},
        "autoupdate": False,
        "share": "disabled",
    }
    config.write_text(json.dumps(data, indent=2) + "\n")


OPENCODE = AgentCLI(
    name="opencode",
    cli="opencode",
    argv=lambda exe, prompt: [str(exe), "run", "--model", "mock/mock-model", prompt],
    env=lambda home, url: {
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",  # no models.dev download
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",  # no auth plugins fetched from npm
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        # The provider package OpenCode installs on first use is cached next to the CLIs, so
        # it is fetched once per run of the suite rather than once per test.
        "XDG_CACHE_HOME": shared_cache("opencode") or str(home / ".cache"),
    },
    setup=_setup,
    folders=(".config/opencode",),
)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_leaky_action_is_blocked_and_the_model_sees_why(tmp_path, action):
    outcome = run(OPENCODE, tmp_path, action, leaky=True)
    assert_blocked(outcome, action)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_clean_action_runs(tmp_path, action):
    outcome = run(OPENCODE, tmp_path, action, leaky=False)
    assert_ran(outcome, action)
    assert uninstall_leaves_nothing(OPENCODE, outcome.home) == []


# What OpenCode's shell shows the commands it runs, started from a color terminal with color
# forced on (harness.color_terminal; harness.PROBE prints names only).
SHELL = {
    "markers": ["OPENCODE"],
    "color": ["FORCE_COLOR", "GITHUB_ACTIONS"],
    "stdin_tty": False,
    "stdout_tty": False,
}


def test_the_shell_shows_the_agent_marker(tmp_path):
    assert shell_probe(run(OPENCODE, tmp_path, "probe", leaky=False)) == SHELL


def test_scan_output_is_plain_text_for_the_model(tmp_path):
    outcome = run(OPENCODE, tmp_path, "scan", leaky=False)
    assert_scan_output(outcome, agent_format=bool(SHELL["markers"]))


def test_bare_agentprint_does_not_hang(tmp_path):
    outcome = run(OPENCODE, tmp_path, "bare", leaky=False)
    assert_help_not_menu(outcome)
