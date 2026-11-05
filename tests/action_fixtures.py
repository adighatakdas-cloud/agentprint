"""Synthetic projects for the GitHub Action self-test. Keys come from tests/fakes.py at runtime.

Usage (from the repository root): python -m tests.action_fixtures DEST
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests import fakes

# fixture -> (command, exit code expected with the default --fail-on high)
EXPECTED = {
    "clean-npm": ("pack", 0),
    "leaky-npm": ("pack", 1),
    "clean-repo": ("scan", 0),
    "leaky-repo": ("scan", 1),
}

LEAKY_SETTINGS = json.dumps(
    {"permissions": {"allow": [f"Bash(export ANTHROPIC_API_KEY={fakes.ANTHROPIC})"]}}
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _npm_project(root: Path, name: str) -> None:
    _write(root / "package.json", json.dumps({"name": name, "version": "1.0.0"}))
    _write(root / "index.js", "module.exports = 1;\n")


def write_fixtures(dest: Path) -> None:
    _npm_project(dest / "clean-npm", "clean-demo")
    _npm_project(dest / "leaky-npm", "leaky-demo")
    _write(dest / "leaky-npm" / ".claude" / "settings.local.json", LEAKY_SETTINGS)
    _write(dest / "clean-repo" / "README.md", "nothing to see\n")
    _write(dest / "leaky-repo" / ".claude" / "settings.local.json", LEAKY_SETTINGS)


if __name__ == "__main__":
    write_fixtures(Path(sys.argv[1]))
