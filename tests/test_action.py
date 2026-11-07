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
    assert set(inputs) == {
        "command",
        "path",
        "fail-on",
        "upload-sarif",
        "badge-dir",
        "attest-badge",
        "publish-badge",
    }
    assert inputs["command"]["default"] == "pack"
    assert inputs["path"]["default"] == "."
    assert inputs["fail-on"]["default"] == "high"
    assert inputs["upload-sarif"]["default"] == "false"
    assert "security-events: write" in inputs["upload-sarif"]["description"]
    assert set(action["outputs"]) == {"exit-code", "sarif-file", "badge-file", "verdict-file"}
    assert action["runs"]["using"] == "composite"


PREDICATE = "https://adighatakdas-cloud.github.io/agentprint/badge/v1"
PUSH_TO_DEFAULT = "github.event_name == 'push'"
DEFAULT_REF = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
PUBLISH_SCRIPT = ROOT / "scripts" / "publish-badge.sh"


def _steps_named(prefix: str) -> list[dict]:
    return [s for s in _action()["runs"]["steps"] if s.get("name", "").startswith(prefix)]


def test_badge_inputs_default_off_and_document_permissions():
    inputs = _action()["inputs"]
    assert inputs["badge-dir"]["default"] == ""
    assert inputs["attest-badge"]["default"] == "false"
    assert inputs["publish-badge"]["default"] == "false"
    assert "id-token: write" in inputs["attest-badge"]["description"]
    assert "attestations: write" in inputs["attest-badge"]["description"]
    assert "contents: write" in inputs["publish-badge"]["description"]
    assert "dry-run" in inputs["publish-badge"]["description"]


def test_badge_dir_is_passed_to_the_run_that_sets_the_exit_code():
    step = _step("run")
    assert step["env"]["AP_BADGE_DIR"] == "${{ inputs.badge-dir }}"
    assert '--badge "$AP_BADGE_DIR"' in step["run"]
    assert 'mkdir -p "$AP_BADGE_DIR"' in step["run"]
    assert "badge-file=" in step["run"] and "verdict-file=" in step["run"]


def test_attest_step_is_pinned_signs_the_badge_and_skips_private_repos():
    [attest] = [s for s in _action()["runs"]["steps"] if "actions/attest@" in s.get("uses", "")]
    assert re.fullmatch(r"actions/attest@[0-9a-f]{40}", attest["uses"])
    assert attest["with"]["predicate-type"] == PREDICATE
    assert attest["with"]["subject-path"] == "${{ steps.run.outputs.badge-file }}"
    assert attest["with"]["predicate-path"] == "${{ steps.run.outputs.verdict-file }}"
    assert "inputs.attest-badge == 'true'" in attest["if"]
    assert "!github.event.repository.private" in attest["if"]
    [notice] = _steps_named("Skip attestation")
    assert "github.event.repository.private" in notice["if"]
    assert "inputs.attest-badge == 'true'" in notice["if"]
    assert "::notice" in notice["run"] and "private" in notice["run"]


def test_publish_runs_only_on_a_push_to_the_default_branch():
    [publish] = _steps_named("Publish badge")
    cond = publish["if"]
    assert "inputs.publish-badge == 'true'" in cond
    assert PUSH_TO_DEFAULT in cond and DEFAULT_REF in cond
    assert publish["env"]["AP_PUBLISH_MODE"] == "true"
    assert publish["env"]["AP_TOKEN"] == "${{ github.token }}"
    [skipped] = _steps_named("Skip publishing")
    assert "::notice" in skipped["run"]
    assert "inputs.publish-badge == 'true'" in skipped["if"]
    assert skipped["if"].count("!(") == 1  # the exact negation of the publish condition
    [dry] = _steps_named("Dry-run publish")
    assert "inputs.publish-badge == 'dry-run'" in dry["if"]
    assert dry["env"]["AP_PUBLISH_MODE"] == "dry-run"
    assert "AP_TOKEN" not in dry["env"]  # a dry run never sees the token


def test_no_token_in_urls_or_logs():
    text = ACTION_FILE.read_text() + PUBLISH_SCRIPT.read_text()
    assert not re.search(r"https?://[^\s/]*@", text)
    assert "x-access-token:@" not in text and "x-access-token:${" not in text.replace(
        "x-access-token:%s", ""
    )
    script = PUBLISH_SCRIPT.read_text()
    assert "::add-mask::$token" in script
    assert "http.extraheader" in script
    assert script.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in script


def _publish(tmp_path, badge_dir, mode, remote=None, **extra):
    env = {
        **os.environ,
        "AP_BADGE_DIR": str(badge_dir),
        "AP_PUBLISH_MODE": mode,
        "AP_REPOSITORY": "owner/repo",
        "AP_TOKEN": "FAKETOKEN123",
        **extra,
    }
    if remote:
        env["AP_REMOTE"] = str(remote)
    return subprocess.run(
        ["bash", str(PUBLISH_SCRIPT)], env=env, cwd=tmp_path, capture_output=True, text=True
    )


@pytest.fixture
def badge_dir(tmp_path):
    d = tmp_path / "badge"
    d.mkdir()
    (d / "badge.json").write_text('{"message": "CLEAR"}\n')
    (d / "verdict.json").write_text('{"verdict": "CLEAR"}\n')
    return d


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_publish_dry_run_pushes_nothing(tmp_path, badge_dir):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    proc = _publish(tmp_path, badge_dir, "dry-run", remote)
    assert proc.returncode == 0, proc.stderr
    assert "dry-run" in proc.stdout and "agentprint-badge" in proc.stdout
    assert "badge.json" in proc.stdout and "verdict.json" in proc.stdout
    refs = subprocess.run(["git", "-C", str(remote), "branch"], capture_output=True, text=True)
    assert refs.stdout.strip() == ""
    assert "FAKETOKEN123" not in (proc.stdout + proc.stderr).replace("::add-mask::FAKETOKEN123", "")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("existing", [False, True])
def test_publish_keeps_one_commit_whether_or_not_the_branch_exists(tmp_path, badge_dir, existing):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    if existing:
        assert _publish(tmp_path, badge_dir, "true", remote).returncode == 0
        (badge_dir / "badge.json").write_text('{"message": "BLOCK"}\n')
    proc = _publish(tmp_path, badge_dir, "true", remote)
    assert proc.returncode == 0, proc.stderr
    git = ["git", "-C", str(remote)]
    count = subprocess.run(
        [*git, "rev-list", "--count", "agentprint-badge"], capture_output=True, text=True
    )
    assert count.stdout.strip() == "1"
    shown = subprocess.run(
        [*git, "show", "agentprint-badge:badge.json"], capture_output=True, text=True
    )
    assert shown.stdout == (badge_dir / "badge.json").read_text()
    files = subprocess.run(
        [*git, "ls-tree", "--name-only", "agentprint-badge"], capture_output=True, text=True
    )
    assert files.stdout.split() == ["badge.json", "verdict.json"]
    assert "::add-mask::FAKETOKEN123" in proc.stdout
    assert "FAKETOKEN123" not in proc.stdout.replace("::add-mask::FAKETOKEN123", "")


def test_publish_without_badge_files_is_an_error(tmp_path):
    proc = _publish(tmp_path, tmp_path, "dry-run")
    assert proc.returncode == 2 and "::error" in proc.stdout


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
