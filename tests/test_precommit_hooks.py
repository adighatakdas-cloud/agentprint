import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentprint.cli import app
from tests import fakes

HOOKS_FILE = Path(__file__).resolve().parent.parent / ".pre-commit-hooks.yaml"
pytestmark = pytest.mark.skipif(not HOOKS_FILE.is_file(), reason="not in the sdist")


def _hooks() -> dict[str, dict]:
    return {hook["id"]: hook for hook in yaml.safe_load(HOOKS_FILE.read_text())}


def test_the_hooks_exist_and_run_once_per_commit_or_push():
    hooks = _hooks()
    assert set(hooks) == {"agentprint-pack", "agentprint-scan", "agentprint-pre-push"}
    for hook in hooks.values():
        assert hook["language"] == "python"
        assert hook["pass_filenames"] is False  # agentprint checks the project, not single files


def test_the_scan_hook_never_scans_the_home_directory():
    assert "--no-home" in shlex.split(_hooks()["agentprint-scan"]["entry"])


def test_pre_push_hook_runs_the_push_check_at_push_even_with_no_changed_files():
    hook = _hooks()["agentprint-pre-push"]
    assert hook["stages"] == ["pre-push"]
    assert hook["always_run"] is True  # a push may change no file pre-commit would match
    assert shlex.split(hook["entry"]) == ["agentprint", "push-check"]  # what is pushed, no wolf


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_gitignored_local_file_with_a_key_never_blocks_the_pre_push_hook(tmp_path, monkeypatch):
    """I5: `scan --no-home` blocked every push when a gitignored .claude/settings.local.json
    held a key, though that file never leaves the machine. push-check reads what is pushed."""
    allow = [f"Bash(export K={fakes.ANTHROPIC})"]
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": allow}})
    )
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\n")
    (tmp_path / "index.js").write_text("module.exports = 1;\n")
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, *ident, "commit", "-qm", "x"], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTPRINT_STRICT", raising=False)
    for name in [n for n in os.environ if n.startswith("PRE_COMMIT_")]:
        monkeypatch.delenv(name)
    _, *args = shlex.split(_hooks()["agentprint-pre-push"]["entry"])
    done = CliRunner().invoke(app, args, input="")
    assert done.exit_code == 0, done.output
    assert done.output.startswith("agentprint: OK")
    scan = CliRunner().invoke(app, ["scan", "--no-home", str(tmp_path), "-f", "agent"])
    assert scan.exit_code == 1  # the old entry: blocked a push that uploads no key


@pytest.mark.parametrize("hook_id", ["agentprint-pack", "agentprint-scan", "agentprint-pre-push"])
def test_hook_entry_is_a_valid_agentprint_command(hook_id):
    program, *args = shlex.split(_hooks()[hook_id]["entry"])
    assert program == "agentprint"
    result = CliRunner().invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
