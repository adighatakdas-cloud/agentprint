"""Run one real agent CLI against the mock model in a throwaway HOME, after `agentprint install`.

Each live test describes its agent with an `AgentCLI` (how to run it headless and point it at
the mock), and `run` does the rest: fake HOME, fake npm/git on PATH, a leaky or clean project,
`agentprint install --agent <name>` into that HOME, one scripted shell command from the model,
and what happened (did the fake publish/push run; what the agent sent back to the model).

Actions: "publish" and "push" try the guarded command. "scan", "bare" and "probe" run in a clean
project, with the agent started as from a person's own terminal (harness.user_terminal):
`agentprint scan`, bare `agentprint`, or a probe of what the agent's shell looks like.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentprint import install as installer
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.base import Launch
from tests.live import harness
from tests.live.install_clis import binary
from tests.live.mock_model import MockModel, Script

TIMEOUT = 240
# The clean-project actions: (command the model runs, prompt).
CLEAN_ACTIONS = {
    "scan": (harness.SCAN, "Scan this project with agentprint."),
    "bare": (harness.BARE, "Run agentprint."),
    "probe": (harness.PROBE, "Show what your shell looks like."),
}


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


def shared_cache(name: str) -> str | None:
    """A cache folder kept next to the installed CLIs (not in the real home), shared by tests."""
    root = os.environ.get("AGENTPRINT_LIVE_TOOLS")
    if not root:
        return None
    folder = Path(root) / "cache" / name
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)


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
    elif action == "push":
        harness.repo(project, leaky)
        command, prompt = harness.PUSH, "Push my commits now."
    else:
        harness.repo(project, leaky)
        command, prompt = CLEAN_ACTIONS[action]
    script = Script(command, str(project), tmp_path / "requests.jsonl")
    with MockModel(script) as url:
        agent.setup(home, url)
        lines: list[str] = []
        installer.install(home, {}, [agent.name], False, lines.append)
        env = harness.agent_env(home, bin_dir, [exe.parent], **agent.env(home, url))
        if action in CLEAN_ACTIONS:
            env = harness.user_terminal(env)
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
    # Exactly the files agentprint edits for this agent (agents' own caches and logs may well
    # mention the agentprint MCP tools they saw; those aren't agentprint's to clean).
    launch = Launch(hook=str(home / ".agentprint" / "bin" / "agentprint-hook"), mcp=("x",))
    paths = {edit.path for edit in ALL_INTEGRATIONS[agent.name].edits(home, {}, launch)}
    return sorted(
        str(path.relative_to(home))
        for path in paths
        if path.is_file() and "agentprint" in path.read_text(errors="replace")
    )


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


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def texts(outcome: Outcome) -> list[str]:
    """Each tool result as the text the command printed. Some agents send it back wrapped in
    JSON (Gemini's functionResponse, Hermes); those are unwrapped to their strings."""
    found = []
    for text in outcome.tool_results:
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        found.append("\n".join(_strings(value)) if isinstance(value, dict | list) else text)
    return found


def _lines(outcome: Outcome) -> list[str]:
    """Every printed line, without an `Output:` label some agents put before the output."""
    return [
        re.sub(r"^Output:\s*", "", line.strip())
        for text in texts(outcome)
        for line in text.splitlines()
    ]


def _seen(outcome: Outcome) -> str:
    return f"tool results: {outcome.tool_results}\nagent output:\n{outcome.output[-3000:]}"


# An escape code as printed, or as JSON or Python would spell it inside a wrapped result.
ESCAPES = ("\x1b[", "\\u001b[", "\\x1b[", "\\033[")


def assert_plain_text(outcome: Outcome) -> None:
    assert outcome.tool_results, f"the model never got the command's output; {_seen(outcome)}"
    raw = outcome.tool_results + texts(outcome)
    assert not any(code in text for text in raw for code in ESCAPES), (
        f"the model got escape codes; {_seen(outcome)}"
    )


def assert_scan_output(outcome: Outcome, agent_format: bool) -> None:
    """Plain text; the agent format (line 1 `agentprint: ...`) exactly when the agent sets
    a marker agentprint knows, else the person's layout (plain, since it is not a terminal)."""
    assert_plain_text(outcome)
    lines = _lines(outcome)
    verdict = any(line.startswith("agentprint: ") for line in lines)
    if agent_format:
        assert verdict, f"no `agentprint: ` verdict line for the model; {_seen(outcome)}"
    else:
        assert not verdict and any(line.startswith("agentprint scan") for line in lines), (
            f"expected the person's layout (no agent marker in this agent); {_seen(outcome)}"
        )


def assert_help_not_menu(outcome: Outcome) -> None:
    """Bare `agentprint` gave the model the help text and never opened the menu."""
    assert_plain_text(outcome)
    text = "\n".join(texts(outcome))
    assert "Usage" in text, f"no help text for the model; {_seen(outcome)}"
    assert "Sweep this machine" not in text and "menu closed" not in text, (
        f"the menu opened inside an AI agent; {_seen(outcome)}"
    )


_PROBE = re.compile(
    re.escape(harness.PROBE_TAG)
    + r" markers=([A-Z_,]*) color=([A-Z_,]*) stdin_tty=([01]) stdout_tty=([01])"
)


def shell_probe(outcome: Outcome) -> dict[str, object]:
    """What harness.PROBE printed inside the agent's shell (names only, never values)."""
    for text in texts(outcome):
        match = _PROBE.search(text)
        if match:
            markers, color, stdin_tty, stdout_tty = match.groups()
            return {
                "markers": [m for m in markers.split(",") if m],
                "color": [c for c in color.split(",") if c],
                "stdin_tty": stdin_tty == "1",
                "stdout_tty": stdout_tty == "1",
            }
    raise AssertionError(f"the probe printed nothing the model saw; {_seen(outcome)}")


def which(name: str) -> str | None:
    return shutil.which(name)
