"""The composite GitHub Action (action.yml) and the workflows that ship with it."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests import fakes
from tests.action_fixtures import EXPECTED, write_fixtures

ROOT = Path(__file__).resolve().parent.parent
ACTION_FILE = ROOT / "action.yml"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
pytestmark = pytest.mark.skipif(not ACTION_FILE.is_file(), reason="action.yml is not in the sdist")
PINNED = re.compile(r"^\s*-?\s*uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s*v\d[\w.]*\s*$")


def _action() -> dict:
    return yaml.safe_load(ACTION_FILE.read_text())


def _step(step_id: str) -> dict:
    [step] = [s for s in _action()["runs"]["steps"] if s.get("id") == step_id]
    return step


def test_inputs_outputs_and_defaults():
    action = _action()
    inputs = action["inputs"]
    assert set(inputs) == {"command", "path", "fail-on", "upload-sarif"}
    assert inputs["command"]["default"] == "pack"
    assert inputs["path"]["default"] == "."
    assert inputs["fail-on"]["default"] == "high"
    assert inputs["upload-sarif"]["default"] == "false"
    assert "security-events: write" in inputs["upload-sarif"]["description"]
    assert set(action["outputs"]) == {"exit-code", "sarif-file"}
    assert action["runs"]["using"] == "composite"


def test_scripts_never_expand_expressions_inline():
    """Inputs reach scripts through env vars only, so they can't inject shell code."""
    for step in _action()["runs"]["steps"]:
        if "run" in step:
            assert "${{" not in step["run"], step.get("name")


def test_upload_is_opt_in_and_the_last_step_returns_agentprint_exit_code():
    steps = _action()["runs"]["steps"]
    [upload] = [s for s in steps if "upload-sarif" in s.get("uses", "")]
    assert "inputs.upload-sarif == 'true'" in upload["if"]
    last = steps[-1]
    assert last["env"]["AP_EXIT"] == "${{ steps.run.outputs.exit-code }}"
    assert "exit" in last["run"]


@pytest.mark.parametrize("path", [ACTION_FILE, *WORKFLOWS], ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_a_commit(path):
    for line in path.read_text().splitlines():
        if re.match(r"^\s*-?\s*uses:", line) and "uses: ./" not in line:
            assert PINNED.match(line), f"{path.name}: pin to a full SHA with a # vX comment: {line}"


def test_selftest_workflow_uses_the_local_action_on_every_fixture():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "action-selftest.yml").read_text())
    steps = [s for job in workflow["jobs"].values() for s in job["steps"]]
    paths = {s["with"]["path"] for s in steps if s.get("uses") == "./"}
    for fixture in EXPECTED:
        assert any(p.endswith(f"/{fixture}") for p in paths), fixture


def test_fixtures_hold_no_key_shaped_literal():
    source = (ROOT / "tests" / "action_fixtures.py").read_text()
    assert fakes.ANTHROPIC not in source


# --- run the action's own shell steps locally -------------------------------------------------


@pytest.fixture(scope="module")
def action_env(tmp_path_factory):
    """Environment the action's scripts see, with agentprint installed into a throwaway venv."""
    tmp = tmp_path_factory.mktemp("action")
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(ROOT),
        "RUNNER_TEMP": str(tmp),
        "UV_PROJECT_ENVIRONMENT": str(tmp / "venv"),  # never touch the dev venv
    }
    env.pop("VIRTUAL_ENV", None)
    install = [s for s in _action()["runs"]["steps"] if s.get("name") == "Install agentprint"][0]
    subprocess.run(["bash", "-eo", "pipefail", "-c", install["run"]], env=env, check=True)
    fixtures = tmp / "fx"
    write_fixtures(fixtures)
    return env, fixtures


def _run_action(
    env: dict, cwd: Path | None = None, **inputs: str
) -> tuple[dict[str, str], subprocess.CompletedProcess]:
    output = Path(env["RUNNER_TEMP"]) / "github_output"
    output.write_text("")
    step = _step("run")
    step_env = {**env, "GITHUB_OUTPUT": str(output)}
    for key, value in step["env"].items():
        name = re.fullmatch(r"\$\{\{ inputs\.([\w-]+) \}\}", str(value))
        step_env[key] = inputs.get(name.group(1), "") if name else str(value)
    proc = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        env=step_env,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return outputs, proc


DEFAULTS = {"fail-on": "high", "upload-sarif": "false"}
RESULTS = {"clean-npm": 0, "leaky-npm": 2, "clean-repo": 0, "leaky-repo": 1}  # SARIF results


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_action_exit_codes_and_sarif(action_env, fixture):
    command, expected = EXPECTED[fixture]
    if command == "pack" and shutil.which("npm") is None:
        pytest.skip("npm not installed")
    env, fixtures = action_env
    outputs, proc = _run_action(env, command=command, path=str(fixtures / fixture), **DEFAULTS)
    assert proc.returncode == 0, proc.stderr  # the step itself never fails; the last step does
    assert outputs["exit-code"] == str(expected), proc.stdout + proc.stderr
    sarif = Path(outputs["sarif-file"]).read_text()
    assert len(json.loads(sarif)["runs"][0]["results"]) == RESULTS[fixture]
    assert fakes.ANTHROPIC not in sarif + proc.stdout + proc.stderr


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        ({"command": "scan", "path": "leaky-repo", "fail-on": "none"}, "0"),
        ({"command": "scan", "path": "missing"}, "2"),
        ({"command": "deploy", "path": "clean-repo"}, "2"),
        ({"command": "scan", "path": "clean-repo", "fail-on": "bogus"}, "2"),
    ],
)
def test_action_fail_on_and_errors(action_env, inputs, expected):
    env, fixtures = action_env
    inputs = {**DEFAULTS, **inputs, "path": str(fixtures / inputs["path"])}
    outputs, proc = _run_action(env, **inputs)
    assert proc.returncode == 0, proc.stderr
    assert outputs["exit-code"] == expected, proc.stdout + proc.stderr
    if expected == "2":
        assert "sarif-file" not in outputs


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_action_passes_a_dash_path_as_a_path(action_env):
    env, fixtures = action_env
    (fixtures / "--help").mkdir(exist_ok=True)
    outputs, proc = _run_action(env, cwd=fixtures, command="scan", path="--help", **DEFAULTS)
    assert outputs["exit-code"] == "0", proc.stdout + proc.stderr
    assert "Usage" not in proc.stdout
