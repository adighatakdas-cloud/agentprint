"""End to end: hostile file names must never turn a suggested fix into code that runs.

An AI agent follows agentprint's fix steps literally, so every command-shaped line in every
output format is executed here (with the command itself swapped for the no-op `:`) and must
not run the payload hidden in the file names.
"""

import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from agentprint.checks import ap008_readable_by_others as ap008
from agentprint.cli import app
from tests import fakes

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, non-root"
)

PAYLOAD = "$(touch PWNED)"
HOSTILE = [
    f"a'{PAYLOAD}'b",
    f"x\\'{PAYLOAD}",
    f'"{PAYLOAD}"',
    "`touch PWNED`",
    f"n\n{PAYLOAD}",
    f"{PAYLOAD} " + "z" * 150,
]
COMMANDS = ("chmod", "find", "git", "rm", "mv", "cp", "printf", "echo", "cd", "sh", "bash")


@pytest.fixture
def box(tmp_path, monkeypatch):
    top = tmp_path.resolve()
    root = top / "box"
    root.mkdir()
    for folder in (top, root):
        folder.chmod(0o755)
    monkeypatch.setattr(
        ap008, "_parents", lambda path: [p for p in path.parents if p == top or top in p.parents]
    )
    return root


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o644)


def _setup(box):
    home, project = box / "home", box / f"proj {PAYLOAD}"
    for i, name in enumerate(HOSTILE):
        _write(home / ".copilot" / "hooks" / f"{name}{i}.json", '{"version": 1}\n')
        _write(home / ".claude" / "projects" / "p" / f"{name}{i}.jsonl", f"key {fakes.GITHUB}\n")
    _write(project / ".claude" / "settings.local.json", "{}\n")
    for folder in [home, project, *home.rglob("*"), *project.rglob("*")]:
        if folder.is_dir():
            folder.chmod(0o755)
    return home, project


def _command_lines(text):
    for line in text.splitlines():
        stripped = line.strip().lstrip("-•* ").strip()
        stripped = stripped.removeprefix("fix:").strip()
        word = stripped.split(" ", 1)[0] if stripped else ""
        if word in COMMANDS:
            yield stripped


def _json_steps(data):
    for finding in data["findings"]:
        yield finding.get("fix", "")
        yield from finding.get("fix_steps", [])


def _runs_payload(command, cwd):
    word, _, rest = command.partition(" ")
    subprocess.run(["bash", "-c", ": " + rest], cwd=cwd, capture_output=True, timeout=10)
    return (cwd / "PWNED").exists()


@pytest.mark.parametrize("fmt", ["agent", "json", "table", "sarif"])
def test_no_suggested_command_runs_code_from_a_file_name(box, fmt):
    home, project = _setup(box)
    result = CliRunner().invoke(
        app, ["scan", str(project), "--home", str(home), "--format", fmt, "--fail-on", "none"]
    )
    assert result.exit_code == 0, result.output
    assert "AP008" in result.output  # the hostile files really were reported

    texts = [result.output]
    if fmt == "json":
        texts += list(_json_steps(json.loads(result.output)))
    if fmt == "sarif":
        for run in json.loads(result.output)["runs"]:
            for rule in run["tool"]["driver"]["rules"]:
                texts.append(rule.get("help", {}).get("text", ""))
                texts.append(rule.get("help", {}).get("markdown", ""))
            for res in run["results"]:
                texts.append(res["message"]["text"])

    commands = [c for text in texts for c in _command_lines(text)]
    for command in commands:
        assert PAYLOAD not in command and "`touch" not in command, command
        assert not _runs_payload(command, box), command
    assert fakes.GITHUB not in result.output
