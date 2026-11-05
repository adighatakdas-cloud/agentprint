"""Guard rails for .github/workflows/release.yml (it is never run from tests)."""

import re
from pathlib import Path

import pytest
import yaml

RELEASE = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
pytestmark = pytest.mark.skipif(not RELEASE.is_file(), reason="not in the sdist")


def _workflow() -> dict:
    data = yaml.safe_load(RELEASE.read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as True
    return data


def _steps(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["steps"]


def test_runs_only_for_version_tags():
    assert _workflow()["on"] == {"push": {"tags": ["v*"]}}


def test_no_permissions_unless_a_job_asks():
    workflow = _workflow()
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}


def test_pypi_uses_trusted_publishing_from_its_own_environment():
    job = _workflow()["jobs"]["pypi"]
    assert job["needs"] == "build"
    assert job["environment"]["name"] == "pypi"
    assert job["permissions"] == {"id-token": "write"}
    [publish] = [s for s in job["steps"] if "pypa/gh-action-pypi-publish" in s.get("uses", "")]
    assert "password" not in publish.get("with", {})  # no stored PyPI token


def test_npm_publishes_after_pypi_with_provenance():
    job = _workflow()["jobs"]["npm"]
    assert "pypi" in job["needs"]  # the wrapper runs uvx agentprint==<version>
    assert job["environment"]["name"] == "npm"
    assert job["permissions"]["id-token"] == "write"
    [publish] = [s for s in job["steps"] if "npm publish" in s.get("run", "")]
    assert "--provenance" in publish["run"]
    assert "--access public" in publish["run"]


def test_build_checks_tag_tests_and_npm_name_before_anything_is_published():
    runs = "\n".join(s.get("run", "") for s in _steps("build"))
    assert "GITHUB_REF_NAME" in runs  # tag must equal the package version
    assert 'pytest -m "integration or not integration"' in runs
    assert "npm view" in runs  # stop early if the npm name belongs to another project
    assert "uv build" in runs


def test_only_secret_is_the_optional_first_publish_npm_token():
    secrets = set(re.findall(r"secrets\.(\w+)", RELEASE.read_text()))
    assert secrets <= {"NPM_TOKEN"}


def test_checkouts_do_not_keep_credentials():
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            if "actions/checkout" in step.get("uses", ""):
                assert step["with"]["persist-credentials"] is False
