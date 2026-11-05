"""The npm wrapper must stay in step with the Python package it runs."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from agentprint import __version__

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_JSON = ROOT / "npm" / "package.json"
pytestmark = pytest.mark.skipif(not PACKAGE_JSON.is_file(), reason="npm/ is not in the sdist")

PRE = {"a": "alpha", "b": "beta", "rc": "rc"}


def npm_version(pep440: str) -> str:
    """The npm (semver) spelling of a Python version, as the wrapper's pythonVersion() reverses."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+)|\.dev(\d+))?", pep440)
    assert m, f"{pep440} has no npm equivalent; use X.Y.Z, X.Y.Z(a|b|rc)N or X.Y.Z.devN"
    base, pre, pre_n, dev_n = m.groups()
    if pre:
        return f"{base}-{PRE[pre]}.{pre_n}"
    if dev_n is not None:
        return f"{base}-dev.{dev_n}"
    return base


@pytest.mark.parametrize(
    ("pep440", "semver"),
    [
        ("1.2.3", "1.2.3"),
        ("1.2.3a1", "1.2.3-alpha.1"),
        ("1.2.3b2", "1.2.3-beta.2"),
        ("1.2.3rc3", "1.2.3-rc.3"),
        ("0.1.0.dev0", "0.1.0-dev.0"),
    ],
)
def test_npm_version_spelling(pep440, semver):
    assert npm_version(pep440) == semver


def test_npm_wrapper_version_matches_python_package():
    package = json.loads(PACKAGE_JSON.read_text())
    assert package["version"] == npm_version(__version__), (
        "npm/package.json version drifted from agentprint/__init__.py"
    )


def test_npm_and_pypi_name_the_same_author():
    package = json.loads(PACKAGE_JSON.read_text())
    [author] = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["authors"]
    assert package["author"] == {"name": author["name"], "email": author["email"]}


def test_npm_package_points_at_this_repository():
    """npm provenance only verifies when repository.url matches the repo that built it."""
    package = json.loads(PACKAGE_JSON.read_text())
    assert package["repository"] == {
        "type": "git",
        "url": "git+https://github.com/adighatakdas-cloud/agentprint.git",
        "directory": "npm",
    }
