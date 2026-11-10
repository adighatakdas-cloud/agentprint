"""Guard rails for the quality-signal workflows and config (never run from tests)."""

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PRIVATE_SKIP = "!github.event.repository.private"
pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="not in the sdist")


def _load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as True
    return data


def _uses(workflow: dict) -> list[str]:
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]


@pytest.mark.parametrize("name", ["codeql.yml", "scorecard.yml", "ci.yml", "docs.yml"])
def test_every_action_is_pinned_to_a_full_sha_with_a_version_comment(name):
    text = (WORKFLOWS / name).read_text()
    for line in text.splitlines():
        if re.match(r"\s*-?\s*uses:", line):
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


@pytest.mark.parametrize("name", ["codeql.yml", "scorecard.yml"])
def test_checkouts_do_not_persist_credentials(name):
    for job in _load(name)["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


@pytest.mark.parametrize("name", ["codeql.yml", "scorecard.yml"])
def test_public_only_workflows_skip_while_the_repo_is_private(name):
    for job in _load(name)["jobs"].values():
        assert PRIVATE_SKIP in job["if"]


def test_scorecard_permissions_are_exactly_what_its_docs_ask_for():
    workflow = _load("scorecard.yml")
    assert workflow["permissions"] == {}
    [job] = workflow["jobs"].values()
    assert job["permissions"] == {
        "security-events": "write",
        "id-token": "write",
        "contents": "read",
        "actions": "read",
    }
    [step] = [s for s in job["steps"] if s.get("uses", "").startswith("ossf/scorecard-action@")]
    assert step["with"]["publish_results"] is True
    assert step["with"]["results_format"] == "sarif"
    assert any(u.startswith("github/codeql-action/upload-sarif@") for u in _uses(workflow))


def test_scorecard_runs_on_main_pushes_and_weekly_only():
    on = _load("scorecard.yml")["on"]
    assert on["push"] == {"branches": ["main"]}
    assert len(on["schedule"]) == 1
    assert "pull_request" not in on


def test_codeql_scans_python_and_actions_on_push_pr_and_weekly():
    workflow = _load("codeql.yml")
    on = workflow["on"]
    assert on["push"] == {"branches": ["main"]}
    assert "pull_request" in on
    assert len(on["schedule"]) == 1
    [job] = workflow["jobs"].values()
    assert [e["language"] for e in job["strategy"]["matrix"]["include"]] == ["python", "actions"]
    assert job["permissions"] == {
        "security-events": "write",
        "contents": "read",
        "actions": "read",
        "packages": "read",
    }
    assert workflow["permissions"] == {}


def test_other_os_job_runs_macos_and_windows_as_hard_gates():
    job = _load("ci.yml")["jobs"]["test-other-os"]
    assert "if" not in job  # the repo is public; no private-repo guard
    assert job["strategy"]["fail-fast"] is False
    legs = job["strategy"]["matrix"]["include"]
    windows = [leg for leg in legs if leg["os"] == "windows-latest"]
    assert sorted(leg["python"] for leg in windows) == ["3.11", "3.13"]
    assert all(leg["experimental"] is False for leg in legs)  # a red Windows leg fails the run
    assert job["continue-on-error"] == "${{ matrix.experimental }}"
    assert job["env"]["PYTHONUTF8"] == "1"
    assert "permissions" not in job  # inherits the top-level contents: read


def test_no_run_step_interpolates_an_expression():
    for job in _load("ci.yml")["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step


def test_ci_runs_pyright_and_a_coverage_floor():
    text = (WORKFLOWS / "ci.yml").read_text()
    assert "uv run pyright" in text
    assert re.search(r"--cov-fail-under=\d+", text)
    assert "--cov=agentprint" in text


def test_docs_workflow_writes_the_coverage_badge_without_any_secret():
    workflow = _load("docs.yml")
    text = (WORKFLOWS / "docs.yml").read_text()
    assert "tools.coverage_badge" in text
    assert "site/public/coverage.json" in text
    assert "secrets." not in text
    assert "paths" in workflow["on"]["pull_request"]  # PR builds keep the path filter
    assert "paths" not in workflow["on"]["push"]  # main pushes always refresh the badge
    steps = workflow["jobs"]["build"]["steps"]
    names = [s.get("run", "") for s in steps]
    badge = next(i for i, r in enumerate(names) if "tools.coverage_badge" in r)
    build = next(i for i, r in enumerate(names) if r == "pnpm build")
    assert badge < build
    [tests] = [s for s in steps if s.get("name") == "Run the tests with coverage"]
    assert tests["continue-on-error"] is True  # a flaky test must not block the site
    [write] = [s for s in steps if "tools.coverage_badge" in s.get("run", "")]
    assert "[ -f coverage.json ]" in write["run"]  # a missing report skips the badge


@pytest.mark.parametrize("name", ["ci.yml", "codeql.yml", "scorecard.yml"])
def test_public_only_workflows_can_be_started_by_hand_after_the_flip(name):
    workflow = _load(name)
    assert "workflow_dispatch" in workflow["on"]
    if name != "ci.yml":
        for job in workflow["jobs"].values():
            assert PRIVATE_SKIP in job["if"]


def test_ci_has_no_expression_inside_run():
    for job in _load("ci.yml")["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")


def test_tools_read_and_write_text_as_utf8():
    for name in ("gen_brand.py", "gen_docs.py", "coverage_badge.py"):
        text = (ROOT / "tools" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if re.search(r"\.(?:read_text|write_text)\(", line):
                assert 'encoding="utf-8"' in line, (name, line)


def test_coverage_badge_writes_the_shields_endpoint_json(tmp_path):
    coverage_badge = pytest.importorskip("tools.coverage_badge")  # not in the sdist
    badge, write_badge = coverage_badge.badge, coverage_badge.write_badge

    assert badge(87.4) == {
        "schemaVersion": 1,
        "label": "coverage",
        "message": "87%",
        "color": "FFB020",
        "labelColor": "050B14",
    }
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 87.4}}))
    out = tmp_path / "out" / "coverage.json"
    write_badge(report, out)
    assert json.loads(out.read_text()) == badge(87.4)


def test_pyproject_pins_dev_tools_and_configures_pyright_and_coverage():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev = config["dependency-groups"]["dev"]
    for tool in ("pytest-cov", "pyright"):
        assert any(re.fullmatch(rf"{tool}==\d+(\.\d+)+", d) for d in dev), tool
    pyright = config["tool"]["pyright"]
    assert pyright["include"] == ["agentprint", "tools"]
    assert pyright["pythonVersion"] == "3.11"
    assert pyright["typeCheckingMode"] == "standard"
    assert config["tool"]["coverage"]["run"]["source"] == ["agentprint"]


def test_readme_has_the_coverage_and_types_badges_but_no_scorecard_yet():
    readme = (ROOT / "README.md").read_text()
    assert (
        "https://img.shields.io/endpoint?url=https://adighatakdas-cloud.github.io/agentprint/coverage.json"
        in readme
    )
    assert "scorecard" not in readme.lower()  # added on launch day, after the first score
    assert "label=types" in readme or "types-pyright" in readme
    assert len(readme.splitlines()) <= 140
