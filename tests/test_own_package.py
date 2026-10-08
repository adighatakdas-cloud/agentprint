import shutil
from pathlib import Path

import pytest

from agentprint.pack import read_python_project

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_TOP_LEVEL = {
    "agentprint",
    "tests",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "PKG-INFO",
    ".gitignore",
}


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_own_sdist_ships_only_allowlisted_files():
    listings = read_python_project(ROOT, lambda name: False)
    [sdist] = [listing for listing in listings if listing.label.endswith(".tar.gz")]
    top_level = {name.split("/", 2)[1] for name in sdist.files}
    assert top_level <= ALLOWED_TOP_LEVEL, top_level - ALLOWED_TOP_LEVEL
