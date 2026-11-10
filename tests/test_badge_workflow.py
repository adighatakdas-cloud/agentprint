"""The reusable workflow that makes agentprint itself sign the badge (never run from tests)."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

import agentprint

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "badge.yml"
ACTION_FILE = ROOT / "action.yml"
pytestmark = pytest.mark.skipif(not WORKFLOW.is_file(), reason="not in the sdist")
PUSH_TO_DEFAULT = "github.event_name == 'push'"
DEFAULT_REF = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"


def _load() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as True
    return data


def _steps() -> list[dict]:
    return _load()["jobs"]["badge"]["steps"]


def _named(prefix: str) -> dict:
    [step] = [s for s in _steps() if s.get("name", "").startswith(prefix)]
    return step


def _action_step(name: str) -> dict:
    [step] = [
        s for s in yaml.safe_load(ACTION_FILE.read_text())["runs"]["steps"] if s.get("name") == name
    ]
    return step


def test_is_a_reusable_workflow_with_three_inputs_and_safe_defaults():
    call = _load()["on"]["workflow_call"]
    assert set(call["inputs"]) == {"command", "path", "fail-on"}
    assert call["inputs"]["command"]["default"] == "scan"
    assert call["inputs"]["path"]["default"] == "."
    assert call["inputs"]["fail-on"]["default"] == "high"
    assert set(_load()["on"]) == {"workflow_call"}  # nothing else can trigger it


def test_permissions_are_empty_at_the_top_and_minimal_on_the_job():
    workflow = _load()
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["badge"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }


@pytest.mark.posix_only  # runs the workflow step with bash
def test_fail_on_none_is_refused_before_anything_runs():
    first = _steps()[0]
    assert first["run"].count("none)") == 1
    assert "exit 2" in first["run"]
    assert "uses" not in first
    bash = subprocess.run(
        ["bash", "-c", first["run"]],
        env={"AP_COMMAND": "scan", "AP_PATH": ".", "AP_FAIL_ON": "none", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert bash.returncode == 2 and "refused" in bash.stdout


@pytest.mark.posix_only  # runs the workflow step with bash
@pytest.mark.parametrize(
    ("command", "path", "fail_on", "code"),
    [
        ("pack", ".", "high", 2),
        ("scan", "sub/dir", "low", 0),
        ("scan", ".", "high", 0),
        ("other", ".", "high", 2),
        ("scan", "/etc", "high", 2),
        ("scan", "../x", "high", 2),
        ("scan", "a/../b", "high", 2),
        ("scan", ".", "everything", 2),
    ],
)
def test_input_check(command, path, fail_on, code):
    env = {"AP_COMMAND": command, "AP_PATH": path, "AP_FAIL_ON": fail_on, "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", "-c", _steps()[0]["run"]], env=env, capture_output=True, text=True
    )
    assert result.returncode == code, result.stdout


def test_installs_the_released_agentprint_pinned_by_one_constant():
    workflow = _load()
    version = workflow["env"]["AGENTPRINT_VERSION"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    text = WORKFLOW.read_text()
    assert re.search(r"^\s*# bumped by the release checklist", text, re.M)
    assert text.count("AGENTPRINT_VERSION:") == 1
    run = _named("Run agentprint")["run"]
    assert '--from "agentprint==$AGENTPRINT_VERSION" agentprint' in run
    assert "--badge" in run
    # the constant is the version in the source tree, minus any .dev/pre-release suffix
    released = re.match(r"\d+\.\d+\.\d+", agentprint.__version__)
    assert released and version == released.group(0)


@pytest.mark.posix_only  # runs the workflow step with bash
def test_pack_is_refused_with_a_pointer_to_the_action():
    env = {"AP_COMMAND": "pack", "AP_PATH": ".", "AP_FAIL_ON": "high", "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", "-c", _steps()[0]["run"]], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2 and "action" in result.stdout and "pack" in result.stdout


def test_agentprint_runs_isolated_from_the_repository_and_never_runs_its_code():
    step = _named("Run agentprint")
    assert step["env"]["UV_NO_CONFIG"] == "1"
    run = step["run"]
    assert 'cd "$RUNNER_TEMP"' in run and run.index("cd ") < run.index("uvx")
    assert "uvx --no-config --from" in run
    assert "agentprint scan --no-home" in run and "pack" not in run
    assert '"$GITHUB_WORKSPACE/$AP_PATH"' in run


def test_a_repositorys_own_config_or_baseline_cannot_soften_the_signed_badge():
    run = _named("Run agentprint")["run"]
    scan = run[run.index("agentprint scan") :]
    assert "--no-config" in scan.split("--badge")[0]  # agentprint's flag, not only uv's


def test_the_publish_script_is_cloned_outside_the_workspace():
    fetch = _named("Fetch the publish script")
    assert "uses" not in fetch
    assert '"$RUNNER_TEMP/agentprint-tool"' in fetch["run"]
    assert '--branch "v$AGENTPRINT_VERSION"' in fetch["run"]
    assert (
        '"$RUNNER_TEMP/agentprint-tool/scripts/publish-badge.sh"' in _named("Publish badge")["run"]
    )
    assert ".agentprint-tool" not in WORKFLOW.read_text()


def test_checks_out_the_callers_repository_without_credentials():
    [checkout] = [s for s in _steps() if s.get("uses", "").startswith("actions/checkout@")][:1]
    assert checkout["with"] == {"persist-credentials": False}  # no `repository:` = the caller's


def test_verdict_gains_path_and_command_only_here():
    run = _named("Run agentprint")["run"]
    assert "path: $path" in run and "command: $command" in run
    assert '"path"' not in (ROOT / "agentprint" / "badge.py").read_text()
    assert "jq" not in ACTION_FILE.read_text()  # the action's --badge output is unchanged


def test_attest_step_matches_the_actions_pin_predicate_and_gate():
    [mine] = [s for s in _steps() if s.get("name") == "Attest the badge"]
    theirs = _action_step("Attest the badge")
    assert mine["uses"] == theirs["uses"]
    assert mine["with"]["predicate-type"] == theirs["with"]["predicate-type"]
    assert PUSH_TO_DEFAULT in mine["if"] and DEFAULT_REF in mine["if"]
    assert "!github.event.repository.private" in mine["if"]
    assert "pull_request" not in mine["if"]


def test_publish_gate_matches_the_actions_and_uses_the_same_script():
    publish = _named("Publish badge")
    theirs = _action_step("Publish badge to the agentprint-badge branch")
    assert PUSH_TO_DEFAULT in publish["if"] and DEFAULT_REF in publish["if"]
    assert set(publish["env"]) == set(theirs["env"])
    assert publish["env"]["AP_PUBLISH_MODE"] == "true"
    assert publish["env"]["AP_TOKEN"] == "${{ github.token }}"
    assert "scripts/publish-badge.sh" in publish["run"]
    fetch = _named("Fetch the publish script")
    assert fetch["if"] == publish["if"]
    assert "adighatakdas-cloud/agentprint.git" in fetch["run"]


def test_the_tool_checkout_comes_after_the_scan():
    names = [s.get("name", "") for s in _steps()]
    assert names.index("Run agentprint") < names.index("Fetch the publish script")


def test_no_expressions_in_run_and_every_use_is_pinned():
    for step in _steps():
        if "run" in step:
            assert "${{" not in step["run"], step.get("name")
    for line in WORKFLOW.read_text().splitlines():
        if re.match(r"\s*-?\s*uses:", line):
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


def test_last_step_returns_agentprints_exit_code_and_no_token_in_urls():
    last = _steps()[-1]
    assert last["env"]["AP_EXIT"] == "${{ steps.run.outputs.exit-code }}"
    assert not re.search(r"https?://[^\s/]*@", WORKFLOW.read_text())
    assert "unfakeable" not in WORKFLOW.read_text().lower()
