"""Claude Code, run for real against the mock model (headless `claude -p`).

Custom endpoint: ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY (dummy).
https://code.claude.com/docs/en/settings (environment variables), https://code.claude.com/docs/en/headless
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

CLAUDE = AgentCLI(
    name="claude-code",
    cli="claude-code",
    argv=lambda exe, prompt: [
        str(exe),
        "-p",
        prompt,
        "--allowedTools",
        "Bash",
        "--output-format",
        "text",
    ],  # fmt: skip
    env=lambda home, url: {
        "ANTHROPIC_BASE_URL": url,
        "ANTHROPIC_API_KEY": harness.DUMMY_KEY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    },
    folders=(".claude",),
)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_leaky_action_is_blocked_and_the_model_sees_why(tmp_path, action):
    outcome = run(CLAUDE, tmp_path, action, leaky=True)
    assert_blocked(outcome, action)


@pytest.mark.parametrize("action", ["publish", "push"])
def test_clean_action_runs(tmp_path, action):
    outcome = run(CLAUDE, tmp_path, action, leaky=False)
    assert_ran(outcome, action)
    assert uninstall_leaves_nothing(CLAUDE, outcome.home) == []


# What Claude Code's shell shows the commands it runs, as a person starts it from a terminal
# (harness.PROBE prints names only).
SHELL = {"markers": ["CLAUDECODE"], "color": [], "stdin_tty": False, "stdout_tty": False}


def test_the_shell_shows_the_agent_marker(tmp_path):
    assert shell_probe(run(CLAUDE, tmp_path, "probe", leaky=False)) == SHELL


def test_scan_output_is_plain_text_for_the_model(tmp_path):
    outcome = run(CLAUDE, tmp_path, "scan", leaky=False)
    assert_scan_output(outcome, agent_format=bool(SHELL["markers"]))


def test_bare_agentprint_does_not_hang(tmp_path):
    outcome = run(CLAUDE, tmp_path, "bare", leaky=False)
    assert_help_not_menu(outcome)
