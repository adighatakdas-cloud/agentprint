"""Gemini CLI, run for real against the mock model (headless `gemini -p`).

Custom endpoint: GOOGLE_GEMINI_BASE_URL (the mock speaks the Gemini API) with a dummy
GEMINI_API_KEY; no Google login. Headless runs refuse untrusted folders unless `--skip-trust`.
https://geminicli.com/docs/reference/configuration , https://geminicli.com/docs/cli/trusted-folders
The plugin variant installs the extension (gemini-extension.json, hooks/hooks.json and its
context file, laid out as at the repository root) with `gemini extensions install <path>
--consent`, and no `agentprint install`. https://geminicli.com/docs/extensions/reference/
"""

import json
import shutil

import pytest

from tests.live import harness
from tests.live.runner import (
    REPO,
    AgentCLI,
    Plugin,
    agent_command,
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


# What Gemini CLI's shell shows the commands it runs, started from a color terminal with color
# forced on (harness.color_terminal; harness.PROBE prints names only).
SHELL = {
    "markers": ["GEMINI_CLI"],
    "color": ["FORCE_COLOR", "GITHUB_ACTIONS"],
    "stdin_tty": False,
    "stdout_tty": False,
}


def test_the_shell_shows_the_agent_marker(tmp_path):
    assert shell_probe(run(GEMINI, tmp_path, "probe", leaky=False)) == SHELL


def test_scan_output_is_plain_text_for_the_model(tmp_path):
    outcome = run(GEMINI, tmp_path, "scan", leaky=False)
    assert_scan_output(outcome, agent_format=bool(SHELL["markers"]))


def test_bare_agentprint_does_not_hang(tmp_path):
    outcome = run(GEMINI, tmp_path, "bare", leaky=False)
    assert_help_not_menu(outcome)


# What a Gemini CLI extension install of this repository uses, at the same paths.
EXTENSION_FILES = (
    "gemini-extension.json",
    "hooks/hooks.json",
    "plugins/agentprint/gemini/GEMINI.md",
)


def _install_extension(home, env, exe, tmp):
    folder = tmp / "agentprint-extension"
    for rel in EXTENSION_FILES:
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, folder / rel)
    agent_command([str(exe), "extensions", "install", str(folder), "--consent"], env, tmp)


PLUGIN = Plugin(install=_install_extension)


def test_extension_blocks_a_leaky_publish(tmp_path):
    assert_blocked(run(GEMINI, tmp_path, "publish", leaky=True, plugin=PLUGIN), "publish")


def test_extension_lets_a_clean_publish_run(tmp_path):
    assert_ran(run(GEMINI, tmp_path, "publish", leaky=False, plugin=PLUGIN), "publish")
