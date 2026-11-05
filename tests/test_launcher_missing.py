"""A missing hook launcher never blocks an agent, and re-install puts it back (review #2, #3)."""

import json
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from agentprint import install
from agentprint.integrations import ALL_INTEGRATIONS

AGENTPRINT = "/opt/agentprint/bin/agentprint"
# Where each agent's hook command lives after install (paths relative to the fake home).
HOOK_FILES = {
    "claude-code": ".claude/settings.json",
    "codex": ".codex/hooks.json",
    "gemini": ".gemini/settings.json",
    "cursor": ".cursor/hooks.json",
    "windsurf": ".codeium/windsurf/hooks.json",
    "devin": ".config/devin/config.json",
    "copilot": ".copilot/hooks/agentprint.json",
    "hermes": ".hermes/config.yaml",
    "antigravity": ".gemini/config/hooks.json",
}
NO_SHELL = {"hermes"}  # runs the command with shlex.split, shell=False


@pytest.fixture(autouse=True)
def fixed_launch(monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: [AGENTPRINT])


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    for folder in (".claude", ".codex", ".cursor", ".codeium/windsurf", ".config/devin",
                   ".config/opencode", ".copilot", ".gemini/antigravity"):  # fmt: skip
        (home / folder).mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text("{}\n")
    (home / ".hermes").mkdir()
    (home / ".hermes" / "config.yaml").write_text("model: x\n")
    return home


def _install(home) -> str:
    out: list[str] = []
    install.install(home, {}, None, False, out.append)
    return "\n".join(out)


def _hook_command(home: Path, agent: str) -> str:
    path = home / HOOK_FILES[agent]
    data = (
        yaml.safe_load(path.read_text()) if path.suffix == ".yaml" else json.loads(path.read_text())
    )
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and "agentprint-hook" in node:
            found.append(node)

    walk(data)
    [command] = found
    return command


def test_every_hooked_agent_is_in_the_table():
    hooked = set(ALL_INTEGRATIONS) - {"opencode"}  # OpenCode's plugin catches a failed spawn
    assert set(HOOK_FILES) == hooked


@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_hook_allows_when_the_launcher_is_gone(home, agent):
    _install(home)
    shutil.rmtree(home / ".agentprint")
    command = _hook_command(home, agent)
    argv = shlex.split(command) if agent in NO_SHELL else ["/bin/sh", "-c", command]
    result = subprocess.run(argv, input="{}", capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ALL_INTEGRATIONS[agent].reply(None).stdout.strip()


def test_reinstall_puts_a_deleted_launcher_back(home):
    _install(home)
    launcher = home / ".agentprint" / "bin" / "agentprint-hook"
    shutil.rmtree(home / ".agentprint")
    out = _install(home)
    assert launcher.is_file() and "Nothing changed" not in out


def test_reinstall_replaces_a_stale_launcher(home):
    _install(home)
    launcher = home / ".agentprint" / "bin" / "agentprint-hook"
    launcher.write_text(launcher.read_text().replace("guard", "guard-old"))
    _install(home)
    assert "guard-old" not in launcher.read_text()


def test_agentprints_own_files_are_private(home):
    _install(home)
    owned = [
        home / ".copilot" / "hooks" / "agentprint.json",
        home / ".cursor" / "rules" / "agentprint.mdc",
        home / ".gemini" / "config" / "rules" / "agentprint.md",
        home / ".config" / "opencode" / "plugins" / "agentprint.js",
    ]
    for path in owned:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    launcher = home / ".agentprint" / "bin" / "agentprint-hook"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o700
