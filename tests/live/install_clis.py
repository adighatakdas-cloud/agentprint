"""Install agent CLIs for live tests into one throwaway folder (never globally).

    python -m tests.live.install_clis <folder> [agent ...]

npm packages go to <folder>/<agent> (npm --prefix), Python ones to a venv in <folder>/<agent>.
npm's cache and HOME point inside <folder> too, so nothing lands in the real home. Delete the
folder to remove everything. Versions are pinned here and in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NPM = {
    "claude-code": "@anthropic-ai/claude-code@2.1.318",
    "codex": "@openai/codex@0.166.0",
    "opencode": "opencode-ai@1.19.5",
    "gemini": "@google/gemini-cli@0.67.0",
    "copilot": "@github/copilot@1.0.105",
    "qwen": "@qwen-code/qwen-code@0.26.7",
}
PIP = {"hermes": "hermes-agent==0.19.0"}
BINARIES = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "gemini": "gemini",
    "copilot": "copilot",
    "qwen": "qwen",
    "hermes": "hermes",
}


def binary(root: Path, agent: str) -> Path:
    """Where the agent's CLI ends up after install."""
    sub = "bin" if agent in PIP else "node_modules/.bin"
    return root / agent / sub / BINARIES[agent]


def install(root: Path, agent: str, spec: str | None = None) -> Path:
    env = {
        **os.environ,
        "HOME": str(root / "home"),
        "npm_config_cache": str(root / "npm-cache"),
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "npm_config_update_notifier": "false",
        "PIP_CACHE_DIR": str(root / "pip-cache"),
        "UV_CACHE_DIR": str(root / "uv-cache"),
    }
    (root / "home").mkdir(parents=True, exist_ok=True)
    target = root / agent
    if agent in NPM:
        cmd = ["npm", "install", "--prefix", str(target), "--no-save", spec or NPM[agent]]
        subprocess.run(cmd, env=env, check=True)
    else:
        subprocess.run(["uv", "venv", "-q", str(target)], env=env, check=True)
        python = str(target / "bin" / "python")
        cmd = ["uv", "pip", "install", "-q", "--python", python, spec or PIP[agent]]
        subprocess.run(cmd, env=env, check=True)
    return binary(root, agent)


if __name__ == "__main__":
    folder = Path(sys.argv[1]).resolve()
    for name in sys.argv[2:] or [*NPM, *PIP]:
        print(name, install(folder, name))
