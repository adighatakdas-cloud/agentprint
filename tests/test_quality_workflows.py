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


def test_other_os_tests_wait_for_a_public_repo():
    job = _load("ci.yml")["jobs"]["test-other-os"]
    assert PRIVATE_SKIP in job["if"]
    assert job["strategy"]["matrix"]["os"] == ["macos-latest", "windows-latest"]
    assert "permissions" not in job  # inherits the top-level contents: read


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


def test_readme_has_the_coverage_scorecard_and_types_badges():
    readme = (ROOT / "README.md").read_text()
    assert (
        "https://img.shields.io/endpoint?url=https://adighatakdas-cloud.github.io/agentprint/coverage.json"
        in readme
    )
    assert (
        "https://api.scorecard.dev/projects/github.com/adighatakdas-cloud/agentprint/badge"
        in readme
    )
    assert "label=types" in readme or "types-pyright" in readme
    assert len(readme.splitlines()) <= 140
