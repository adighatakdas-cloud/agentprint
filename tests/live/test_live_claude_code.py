"""Claude Code, run for real against the mock model (headless `claude -p`).

Custom endpoint: ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY (dummy).
https://code.claude.com/docs/en/settings (environment variables), https://code.claude.com/docs/en/headless
The plugin variant loads plugins/agentprint with `--plugin-dir` (no `agentprint install`), and
`claude plugin validate` checks the marketplace and the plugin offline.
https://code.claude.com/docs/en/plugins/loading.md
"""

import pytest

from tests.live import harness
from tests.live.runner import (
    PLUGINS,
    REPO,
    AgentCLI,
    Plugin,
    agent_command,
    assert_blocked,
    assert_help_not_menu,
    assert_ran,
    assert_scan_output,
    find_cli,
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


# What Claude Code's shell shows the commands it runs, started from a color terminal with color
# forced on (harness.color_terminal; harness.PROBE prints names only).
SHELL = {
    "markers": ["CLAUDECODE"],
    "color": ["FORCE_COLOR", "GITHUB_ACTIONS"],
    "stdin_tty": False,
    "stdout_tty": False,
}


def test_the_shell_shows_the_agent_marker(tmp_path):
    assert shell_probe(run(CLAUDE, tmp_path, "probe", leaky=False)) == SHELL


def test_scan_output_is_plain_text_for_the_model(tmp_path):
    outcome = run(CLAUDE, tmp_path, "scan", leaky=False)
    assert_scan_output(outcome, agent_format=bool(SHELL["markers"]))


def test_bare_agentprint_does_not_hang(tmp_path):
    outcome = run(CLAUDE, tmp_path, "bare", leaky=False)
    assert_help_not_menu(outcome)


PLUGIN = Plugin(args=("--plugin-dir", str(PLUGINS)))


def test_plugin_blocks_a_leaky_publish(tmp_path):
    assert_blocked(run(CLAUDE, tmp_path, "publish", leaky=True, plugin=PLUGIN), "publish")


def test_plugin_lets_a_clean_publish_run(tmp_path):
    assert_ran(run(CLAUDE, tmp_path, "publish", leaky=False, plugin=PLUGIN), "publish")


@pytest.mark.parametrize("target", [REPO, PLUGINS], ids=["marketplace", "plugin"])
def test_claude_plugin_validate_passes(tmp_path, target):
    exe = find_cli("claude-code")
    home = tmp_path / "home"
    home.mkdir()
    env = harness.agent_env(
        home, tmp_path / "shims", [exe.parent], **CLAUDE.env(home, "http://127.0.0.1:9")
    )
    out = agent_command([str(exe), "plugin", "validate", str(target)], env, tmp_path)
    assert "Validation passed" in out, out
