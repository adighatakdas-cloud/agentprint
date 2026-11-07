"""The Windows hook launcher (agentprint-hook.ps1) and the hook commands that start it, run by
real PowerShell: Windows PowerShell 5.1 (powershell.exe, on every Windows) and PowerShell 7
(pwsh) when it is installed. Windows only; tests/test_windows_hooks.py checks the same text on
every OS.

Allow = exit 0 with the agent's own allow output; block = the agent's block (JSON on exit 0, or
the guard's exit 3 turned into 2); any failure of agentprint = exit 0, the allow output and one
line on stderr. Never exit 1: Copilot CLI denies on it and Claude Code without Git Bash counts
it as a hook failure. agentprint is replaced by a Python script that records what it was given
(arguments, the exact bytes on its stdin, PYTHONUTF8) and answers as told.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentprint import install
from agentprint.guard import Verdict
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.base import BLOCK_EXIT, WINDOWS_LAUNCHER, Launch

pytestmark = pytest.mark.windows_only

AGENTS = sorted(name for name, i in ALL_INTEGRATIONS.items() if i.windows_hooks)
SHELLS = ["powershell.exe", *(["pwsh"] if shutil.which("pwsh") else [])]
# Each agent's hook command as the agent runs it: Codex through cmd.exe /C, the others in
# PowerShell (either version).
RUNNERS = [(a, s) for a in AGENTS for s in (["cmd.exe"] if a == "codex" else SHELLS)]
FOLDER = "O'Brien Zoë dir"  # a space, an apostrophe and a non-ASCII letter in every path
PAYLOAD = json.dumps(
    {"tool_name": "Bash", "tool_input": {"command": "cd C:\\Users\\Zoë\\日本 && npm publish"}},
    ensure_ascii=False,
).encode("utf-8")
FAKE = """\
import json, os, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
data = sys.stdin.buffer.read()
seen = {"argv": sys.argv[2:], "stdin": data.hex(), "utf8": os.environ.get("PYTHONUTF8")}
with open(config["record"], "w", encoding="utf-8") as record:
    json.dump(seen, record)
sys.stdout.buffer.write(config["stdout"].encode("utf-8"))
sys.stdout.buffer.flush()
sys.stderr.write(config["stderr"])
sys.stderr.flush()
sys.exit(config["exit"])
"""


@dataclass
class Fake:
    launcher: Path
    config: Path
    record: Path

    def answer(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        data = {"record": str(self.record), "stdout": stdout, "stderr": stderr, "exit": exit_code}
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def seen(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))


def _launcher(folder: Path, agentprint: list[str]) -> Path:
    launcher = folder / WINDOWS_LAUNCHER
    launcher.write_bytes(install.windows_launcher_script(agentprint).encode("ascii"))
    return launcher


@pytest.fixture
def fake(tmp_path) -> Fake:
    folder = tmp_path / FOLDER
    folder.mkdir()
    script = folder / "fake agentprint.py"
    script.write_text(FAKE, encoding="utf-8")
    config = folder / "config.json"
    launcher = _launcher(folder, [sys.executable, str(script), str(config)])
    made = Fake(launcher, config, folder / "record.json")
    made.answer()
    return made


def _env() -> dict[str, str]:
    """The environment an agent gives its hooks: CI's PYTHONUTF8=1 taken out, so the tests see
    that the launcher sets it."""
    return {k: v for k, v in os.environ.items() if k.upper() != "PYTHONUTF8"}


def _run(args: list[str] | str, payload: bytes):
    return subprocess.run(args, input=payload, capture_output=True, timeout=120, env=_env())


def run_file(shell: str, launcher: Path, *args: str, payload: bytes = PAYLOAD):
    argv = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    return _run([*argv, str(launcher), *args], payload)


def hook_command(agent: str, launch: Launch) -> str:
    """The Windows hook command `agentprint install` writes for `agent`."""
    [edit] = [
        e
        for e in ALL_INTEGRATIONS[agent].edits(Path("C:/nonexistent/home"), {}, launch)
        if e.what.startswith("hook")
    ]
    data = json.loads(edit.apply(None))
    if agent == "copilot":
        return data["hooks"]["preToolUse"][0]["powershell"]
    if agent == "cursor":
        return data["hooks"]["beforeShellExecution"][0]["command"]
    handler = data["hooks"]["PreToolUse"][0]["hooks"][0]
    return handler["commandWindows" if agent == "codex" else "command"]


def run_as_agent(runner: str, command: str, payload: bytes = PAYLOAD):
    if runner == "cmd.exe":  # codex-rs: %COMSPEC% /C "<commandWindows>", the payload on stdin
        return _run(f'cmd.exe /C "{command}"', payload)
    return _run([runner, "-NoProfile", "-NonInteractive", "-Command", command], payload)


def out(result) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def ours(result) -> list[str]:
    """agentprint's own lines on stderr (PowerShell may add its own)."""
    text = result.stderr.decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if "agentprint" in line]


def allow(agent: str) -> str:
    return ALL_INTEGRATIONS[agent].reply(None).stdout.strip()


# ---- the launcher on its own ----------------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("agent", AGENTS)
def test_allow_passes_through_with_exit_0(fake, shell, agent):
    fake.answer(0, ALL_INTEGRATIONS[agent].reply(None).stdout + "\n")
    result = run_file(shell, fake.launcher, "--agent", agent)
    assert result.returncode == 0, result.stderr
    assert out(result) == allow(agent)
    assert fake.seen()["argv"] == ["guard", "--agent", agent]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("agent", AGENTS)
def test_block_uses_the_agents_own_mechanism(fake, shell, agent):
    block = ALL_INTEGRATIONS[agent].reply(Verdict(True, "agentprint: BLOCK — 1 problem"))
    assert block.exit_code == 0  # all four block with JSON on exit 0
    fake.answer(block.exit_code, block.stdout + "\n", block.stderr)
    result = run_file(shell, fake.launcher, "--agent", agent)
    assert result.returncode == 0, result.stderr
    assert "agentprint: BLOCK" in out(result)
    assert json.loads(out(result)) == json.loads(block.stdout)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_guards_block_exit_becomes_2(fake, shell):
    fake.answer(BLOCK_EXIT, "blocked\n", "agentprint: internal error (strict mode)\n")
    result = run_file(shell, fake.launcher, "--agent", "claude-code")
    assert result.returncode == 2
    assert out(result) == "blocked"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("failure", [1, 2, 127])
def test_any_failure_allows_with_exit_0_and_one_line(fake, shell, agent, failure):
    # 2 is what uv itself exits with on its own errors; it must never turn into a block.
    fake.answer(failure, '{"partial')
    result = run_file(shell, fake.launcher, "--agent", agent)
    assert result.returncode == 0
    assert out(result) == allow(agent)  # Cursor needs valid JSON even then
    assert ours(result) == [
        f"agentprint: hook check failed (exit {failure}); the command was allowed"
    ]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("agent", AGENTS)
def test_missing_agentprint_allows_with_exit_0(tmp_path, shell, agent):
    folder = tmp_path / FOLDER
    folder.mkdir()
    launcher = _launcher(folder, [str(folder / "gone" / "agentprint.exe")])
    result = run_file(shell, launcher, "--agent", agent)
    assert result.returncode == 0
    assert out(result) == allow(agent)
    assert ours(result) == [
        "agentprint: hook check failed (agentprint did not start); the command was allowed"
    ]


@pytest.mark.parametrize("shell", SHELLS)
def test_hook_input_reaches_agentprint_byte_for_byte(fake, shell):
    result = run_file(shell, fake.launcher, "--agent", "cursor")
    assert result.returncode == 0, result.stderr
    seen = fake.seen()
    assert bytes.fromhex(seen["stdin"]) == PAYLOAD
    assert seen["utf8"] == "1"


@pytest.mark.parametrize("shell", SHELLS)
def test_strict_reaches_agentprint(fake, shell):
    run_file(shell, fake.launcher, "--agent", "codex", "--strict")
    assert fake.seen()["argv"] == ["guard", "--agent", "codex", "--strict"]


@pytest.mark.parametrize("shell", SHELLS)
def test_no_agent_name_allows_without_running_agentprint(fake, shell):
    result = run_file(shell, fake.launcher)
    assert result.returncode == 0
    assert not fake.record.exists()
    assert ours(result) == [
        "agentprint: hook check failed (no agent name); the command was allowed"
    ]


# ---- the hook command each agent runs -------------------------------------------------------


@pytest.mark.parametrize(("agent", "runner"), RUNNERS)
@pytest.mark.parametrize("strict", [False, True])
def test_hook_command_runs_the_launcher_with_the_input(fake, agent, runner, strict):
    launch = Launch(hook=str(fake.launcher), mcp=("agentprint", "mcp"), strict=strict, windows=True)
    fake.answer(0, ALL_INTEGRATIONS[agent].reply(None).stdout + "\n")
    result = run_as_agent(runner, hook_command(agent, launch))
    assert result.returncode == 0, result.stderr
    assert out(result) == allow(agent)
    seen = fake.seen()
    assert seen["argv"] == ["guard", "--agent", agent, *(["--strict"] if strict else [])]
    assert bytes.fromhex(seen["stdin"]) == PAYLOAD  # through every PowerShell in between


@pytest.mark.parametrize(("agent", "runner"), RUNNERS)
def test_hook_command_keeps_the_block_exit(fake, agent, runner):
    launch = Launch(hook=str(fake.launcher), mcp=("agentprint", "mcp"), windows=True)
    fake.answer(BLOCK_EXIT, "blocked\n")
    result = run_as_agent(runner, hook_command(agent, launch))
    assert result.returncode == 2
    assert out(result) == "blocked"


@pytest.mark.parametrize(("agent", "runner"), RUNNERS)
def test_hook_command_allows_when_the_launcher_is_gone(tmp_path, agent, runner):
    gone = tmp_path / FOLDER / "bin" / WINDOWS_LAUNCHER
    launch = Launch(hook=str(gone), mcp=("agentprint", "mcp"), windows=True)
    result = run_as_agent(runner, hook_command(agent, launch))
    assert result.returncode == 0, result.stderr
    assert out(result) == allow(agent)


@pytest.mark.parametrize(("agent", "runner"), RUNNERS)
def test_hook_command_allows_when_the_launcher_cannot_run(tmp_path, agent, runner):
    folder = tmp_path / FOLDER
    folder.mkdir()
    broken = folder / WINDOWS_LAUNCHER
    broken.write_text("# agentprint-hook\nif ( {\n", encoding="ascii")  # does not parse
    launch = Launch(hook=str(broken), mcp=("agentprint", "mcp"), windows=True)
    result = run_as_agent(runner, hook_command(agent, launch))
    assert result.returncode == 0, result.stderr
    assert out(result) == allow(agent)
