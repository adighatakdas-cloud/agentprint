"""Run one real agent CLI against the mock model in a throwaway HOME, after `agentprint install`.

Each live test describes its agent with an `AgentCLI` (how to run it headless and point it at
the mock), and `run` does the rest: fake HOME, fake npm/git on PATH, a leaky or clean project,
`agentprint install --agent <name>` into that HOME, one scripted shell command from the model,
and what happened (did the fake publish/push run; what the agent sent back to the model).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentprint import install as installer
from tests.live import harness
from tests.live.install_clis import binary
from tests.live.mock_model import MockModel, Script

TIMEOUT = 240


@dataclass(frozen=True)
class AgentCLI:
    name: str  # agentprint integration name
    cli: str  # name in tests/live/install_clis.py
    argv: Callable[[Path, str], list[str]]  # (binary, prompt) -> command line
    env: Callable[[Path, str], dict[str, str]]  # (home, mock url) -> extra environment
    setup: Callable[[Path, str], None] = lambda home, url: None  # before agentprint install
    folders: tuple[str, ...] = field(default_factory=tuple)  # created in HOME so it's "installed"


@dataclass
class Outcome:
    published: bool
    pushed: bool
    tool_results: list[str]
    output: str
    install_output: str
    home: Path


def find_cli(cli: str) -> Path:
    """The agent CLI from AGENTPRINT_LIVE_TOOLS (see install_clis.py), else skip the test."""
    root = os.environ.get("AGENTPRINT_LIVE_TOOLS")
    path = binary(Path(root), cli) if root else None
    if path is None or not path.exists():
        pytest.skip(f"{cli} CLI not installed (python -m tests.live.install_clis <dir> {cli}; "
                    "then set AGENTPRINT_LIVE_TOOLS=<dir>)")  # fmt: skip
    return path


def run(agent: AgentCLI, tmp_path: Path, action: str, leaky: bool) -> Outcome:
    exe = find_cli(agent.cli)
    home = tmp_path / "home"
    for folder in agent.folders:
        (home / folder).mkdir(parents=True, exist_ok=True)
    bin_dir, markers = tmp_path / "shims", tmp_path / "markers"
    harness.make_shims(bin_dir, markers)
    project = tmp_path / "project"
    if action == "publish":
        harness.package(project, leaky)
        command, prompt = harness.PUBLISH, "Publish this package to npm now."
    else:
        harness.repo(project, leaky)
        command, prompt = harness.PUSH, "Push my commits now."
    script = Script(command, str(project), tmp_path / "requests.jsonl")
    with MockModel(script) as url:
        agent.setup(home, url)
        lines: list[str] = []
        installer.install(home, {}, [agent.name], False, lines.append)
        env = harness.agent_env(home, bin_dir, [exe.parent], **agent.env(home, url))
        result = subprocess.run(
            agent.argv(exe, prompt),
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    return Outcome(
        published=(markers / "PUBLISHED").exists(),
        pushed=(markers / "PUSHED").exists(),
        tool_results=script.tool_results(),
        output=result.stdout + result.stderr,
        install_output="\n".join(lines),
        home=home,
    )


def uninstall_leaves_nothing(agent: AgentCLI, home: Path) -> list[str]:
    """Run `agentprint uninstall --agent <name>`; return agent files that still mention
    agentprint. (~/.agentprint itself may stay: uninstall keeps its backups when the agent
    rewrote a file in the meantime, as Claude Code does with ~/.claude.json.)"""
    installer.uninstall(home, {}, [agent.name], False, lambda line: None)
    left = []
    for path in home.rglob("*"):
        if ".agentprint" in path.relative_to(home).parts:
            continue
        if path.is_file() and path.suffix in {".json", ".toml", ".yaml", ".md", ".js", ".mdc"}:
            try:
                if "agentprint" in path.read_text(errors="replace"):
                    left.append(str(path.relative_to(home)))
            except OSError:
                continue
    return left


def assert_blocked(outcome: Outcome, action: str) -> None:
    ran = outcome.published if action == "publish" else outcome.pushed
    assert not ran, f"the {action} ran although agentprint should block it:\n{outcome.output}"
    assert any("agentprint: BLOCK" in text for text in outcome.tool_results), (
        f"the model never saw agentprint's BLOCK reason; tool results: {outcome.tool_results}\n"
        f"agent output:\n{outcome.output[-3000:]}"
    )


def assert_ran(outcome: Outcome, action: str) -> None:
    ran = outcome.published if action == "publish" else outcome.pushed
    assert ran, (
        f"the clean {action} did not run; tool results: {outcome.tool_results}\n"
        f"agent output:\n{outcome.output[-3000:]}"
    )


def which(name: str) -> str | None:
    return shutil.which(name)
