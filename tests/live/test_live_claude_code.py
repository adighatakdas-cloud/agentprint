"""Claude Code, run for real against the mock model (headless `claude -p`).

Custom endpoint: ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY (dummy).
https://code.claude.com/docs/en/settings (environment variables), https://code.claude.com/docs/en/headless
"""

import pytest

from tests.live import harness
from tests.live.runner import AgentCLI, assert_blocked, assert_ran, run, uninstall_leaves_nothing

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
