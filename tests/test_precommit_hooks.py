import shlex
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentprint.cli import app

HOOKS_FILE = Path(__file__).resolve().parent.parent / ".pre-commit-hooks.yaml"
pytestmark = pytest.mark.skipif(not HOOKS_FILE.is_file(), reason="not in the sdist")


def _hooks() -> dict[str, dict]:
    return {hook["id"]: hook for hook in yaml.safe_load(HOOKS_FILE.read_text())}


def test_both_hooks_exist_and_run_once_per_commit():
    hooks = _hooks()
    assert set(hooks) == {"agentprint-pack", "agentprint-scan"}
    for hook in hooks.values():
        assert hook["language"] == "python"
        assert hook["pass_filenames"] is False  # agentprint checks the project, not single files


def test_scan_hook_never_scans_the_home_directory():
    assert "--no-home" in shlex.split(_hooks()["agentprint-scan"]["entry"])


@pytest.mark.parametrize("hook_id", ["agentprint-pack", "agentprint-scan"])
def test_hook_entry_is_a_valid_agentprint_command(hook_id):
    program, *args = shlex.split(_hooks()[hook_id]["entry"])
    assert program == "agentprint"
    result = CliRunner().invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
