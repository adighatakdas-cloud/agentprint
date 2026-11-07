"""agentprint as a native plugin or extension: every manifest's shape, every plugin hook's
command, and each hook replayed against a fake leaky package.

A plugin hook must be the entry `agentprint install` writes for that agent (same keys, same
matcher, same timeout) with the plugin's pinned uvx command, and that command must fail open with
one stderr line when uvx is missing or fails.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agentprint import __version__, hook, plugins
from agentprint.checks.ap004_network_hook import network_indicator
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.base import BLOCK_EXIT, INSTRUCTIONS, Launch
from agentprint.secrets import find_secrets
from tests.test_hooks import payload
from tests.test_hooks import project as project  # noqa: F401 - the leaky-package fixture
from tools import plugins as gen

ROOT = gen.ROOT
PLUGIN = gen.PLUGIN
GUARD = f"uvx --from agentprint=={__version__} agentprint guard --agent"
MCP_ARGS = ["--from", f"agentprint=={__version__}", "agentprint", "mcp"]
LEAKY_PUBLISH = "npm publish demo-1.0.0.tgz"
pytestmark = pytest.mark.skipif(not PLUGIN.is_dir(), reason="not in the sdist")

# agent -> (the plugin's hook file, how to find the agentprint hook command in it)
HOOK_FILES = {
    "claude-code": PLUGIN / "hooks" / "hooks.json",
    "codex": PLUGIN / "codex" / "hooks" / "hooks.json",
    "copilot": PLUGIN / "copilot" / "hooks.json",
    "gemini": ROOT / "hooks" / "hooks.json",
}


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def _commands(data: object) -> list[str]:
    """Every hook command string in a hook file (`command`, or Copilot's `bash`)."""
    if isinstance(data, dict):
        found = [v for k, v in data.items() if k in ("command", "bash") and isinstance(v, str)]
        return found + [c for v in data.values() for c in _commands(v)]
    if isinstance(data, list):
        return [c for v in data for c in _commands(v)]
    return []


def _hook_command(agent: str) -> str:
    [command] = _commands(_load(HOOK_FILES[agent]))
    return command


def _installed(agent: str) -> dict:
    """The hook file `agentprint install` writes for `agent`, launcher command and all."""
    launch = Launch(hook="/home/u/.agentprint/bin/agentprint-hook", mcp=("agentprint", "mcp"))
    for edit in ALL_INTEGRATIONS[agent].edits(Path("/nonexistent/home"), {}, launch):
        if edit.what.startswith("hook"):
            return json.loads(edit.apply(None))
    raise LookupError(agent)


def _without_commands(data: object) -> object:
    if isinstance(data, dict):
        return {
            k: "<command>" if k in ("command", "bash") else _without_commands(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_without_commands(v) for v in data]
    return data


def _json_files() -> list[Path]:
    return sorted(p for p in gen.files() if p.suffix == ".json")


# ---- every ecosystem ----------------------------------------------------------------------------


def test_every_plugin_file_is_generated_and_current():
    for path, text in gen.files().items():
        assert path.read_text(encoding="utf-8") == text, (
            path
        )  # run: uv run python -m tools.gen_docs


def test_every_json_file_parses_and_every_version_is_this_one():
    def versions(data: object) -> list[str]:
        if isinstance(data, dict):
            own = [v for k, v in data.items() if k == "version" and isinstance(v, str)]
            return own + [x for v in data.values() for x in versions(v)]
        if isinstance(data, list):
            return [x for v in data for x in versions(v)]
        return []

    found = []
    for path in _json_files():
        found += versions(_load(path))
    assert found and set(found) == {__version__}


@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_plugin_hook_is_the_entry_install_writes(agent):
    assert _without_commands(_load(HOOK_FILES[agent])) == _without_commands(_installed(agent))


@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_plugin_hook_runs_this_version_through_uvx(agent):
    command = _hook_command(agent)
    argv = shlex.split(command)
    assert argv[:2] == ["/bin/sh", "-c"] and argv[3:] == [plugins.PLUGIN_MARK]
    assert f"{GUARD} {agent})" in argv[2]
    allow = ALL_INTEGRATIONS[agent].reply(None).stdout.strip()
    assert command == plugins.hook_command(agent, allow)


def test_hook_commands_never_look_like_network_calls():
    # AP004 flags hooks that call curl, pipe into a shell or name a URL; ours must stay clean.
    commands = [_hook_command(agent) for agent in HOOK_FILES]
    assert [c for c in commands if network_indicator(c)] == []


def test_every_mcp_server_runs_this_version_through_uvx():
    servers = []
    for path in _json_files():
        servers += _load(path).get("mcpServers", {}).values()
    assert servers
    for server in servers:
        assert server["command"] == "uvx" and server["args"] == MCP_ARGS


def test_no_plugin_file_holds_a_key():
    for path, text in gen.files().items():
        assert find_secrets(text) == [], path


def test_names_are_agentprint_and_kebab_case():
    for path in _json_files():
        data = _load(path)
        if "name" in data and path.name != "server.json":
            assert data["name"] == "agentprint", path
            assert re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", data["name"])


def test_instruction_files_carry_install_s_instructions():
    notes = [p for p in gen.files() if p.suffix in (".md", ".mdc") and p.name != "README.md"]
    assert notes
    for path in notes:
        assert INSTRUCTIONS in path.read_text(encoding="utf-8"), path


# ---- the hook command, run for real --------------------------------------------------------------


def _fake_uvx(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    uvx = bin_dir / "uvx"
    uvx.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{bin_dir}/args"\n{body}\n')
    uvx.chmod(0o755)


def _real_guard_uvx(bin_dir: Path) -> None:
    """A uvx that runs this checkout's agentprint for `--from agentprint==V agentprint ...`."""
    python = shlex.quote(sys.executable)
    code = shlex.quote("import sys; from agentprint.cli import app; app()")
    _fake_uvx(bin_dir, f'shift 3\nexec {python} -c {code} "$@"')


def _run(command: str, home: Path, path: str, stdin: str) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": path}
    return subprocess.run(
        command, shell=True, input=stdin, capture_output=True, text=True, env=env, timeout=120
    )


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_plugin_hook_blocks_a_leaky_publish_like_the_guard(agent, project, tmp_path):
    bin_dir = tmp_path / "bin"
    _real_guard_uvx(bin_dir)
    raw = json.dumps(payload(agent, LEAKY_PUBLISH, project))
    result = _run(_hook_command(agent), tmp_path, str(bin_dir), raw)
    expected = hook.run_hook(agent, raw, {})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected.stdout.strip()
    assert "agentprint: BLOCK" in result.stdout
    args = (bin_dir / "args").read_text().split()
    assert args == ["--from", f"agentprint=={__version__}", "agentprint", "guard", "--agent", agent]


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_plugin_hook_lets_an_ordinary_command_through(agent, project, tmp_path):
    bin_dir = tmp_path / "bin"
    _real_guard_uvx(bin_dir)
    raw = json.dumps(payload(agent, "ls -la", project))
    result = _run(_hook_command(agent), tmp_path, str(bin_dir), raw)
    assert result.returncode == 0
    assert result.stdout.strip() == hook.run_hook(agent, raw, {}).stdout.strip()
    assert "BLOCK" not in result.stdout


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_missing_uvx_fails_open_with_one_stderr_line(agent, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(_hook_command(agent), tmp_path, str(empty), "{}")
    assert result.returncode == 0
    assert result.stderr.splitlines() == [plugins.NO_UVX]
    assert result.stdout.strip() == ALL_INTEGRATIONS[agent].reply(None).stdout.strip()


@pytest.mark.posix_only
def test_uvx_in_the_installer_folder_is_found_without_path(tmp_path):
    _fake_uvx(tmp_path / ".local" / "bin", "while read -r _; do :; done; exit 0")
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(_hook_command("claude-code"), tmp_path, str(empty), "{}")
    assert result.returncode == 0 and result.stderr == ""
    assert (tmp_path / ".local" / "bin" / "args").is_file()


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", sorted(HOOK_FILES))
def test_failing_uvx_fails_open_and_never_blocks(agent, tmp_path):
    # uv's own errors exit 2, which Claude Code and Codex would read as "block".
    bin_dir = tmp_path / "bin"
    _fake_uvx(bin_dir, "while read -r _; do :; done; echo 'error: no network' >&2; exit 2")
    result = _run(_hook_command(agent), tmp_path, str(bin_dir), "{}")
    assert result.returncode == 0
    assert result.stderr.splitlines()[-1] == plugins.FAILED.replace("$s", "2")
    assert result.stdout.strip() == ALL_INTEGRATIONS[agent].reply(None).stdout.strip()


@pytest.mark.posix_only
def test_guard_block_exit_becomes_the_agents_exit_2(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_uvx(bin_dir, f"while read -r _; do :; done; echo blocked; exit {BLOCK_EXIT}")
    result = _run(_hook_command("claude-code"), tmp_path, str(bin_dir), "{}")
    assert result.returncode == 2 and result.stdout == "blocked\n"


# ---- Claude Code ---------------------------------------------------------------------------------


def test_claude_code_marketplace_and_manifest():
    market = _load(ROOT / ".claude-plugin" / "marketplace.json")
    assert market["name"] == "agentprint" and market["owner"]["name"]
    [entry] = market["plugins"]
    assert entry["name"] == "agentprint" and entry["source"] == "./plugins/agentprint"
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    manifest = _load(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "agentprint" and manifest["version"] == __version__
    assert manifest["license"] == "MIT"
    # Claude Code does not load a plugin's CLAUDE.md (and `claude plugin validate` warns).
    assert not (PLUGIN / "CLAUDE.md").exists()
    # A package.json + lockfile in the plugin root would make Claude Code run npm install.
    assert not (PLUGIN / "package.json").exists()


def test_claude_code_mcp_and_skill():
    server = _load(PLUGIN / ".mcp.json")["mcpServers"]["agentprint"]
    assert server == {"type": "stdio", "command": "uvx", "args": MCP_ARGS}
    skill = (PLUGIN / "skills" / "agentprint" / "SKILL.md").read_text()
    front = yaml.safe_load(skill.split("---\n")[1])
    assert front["name"] == "agentprint" and "git push" in front["description"]


def test_claude_code_plugin_hook_replays_through_run_hook(project):
    """The plugin's own matcher and agent name, replayed through the guard: a leaky npm
    publish is denied with the reason the model reads."""
    [entry] = _load(HOOK_FILES["claude-code"])["hooks"]["PreToolUse"]
    event = payload("claude-code", LEAKY_PUBLISH, project)
    assert re.fullmatch(entry["matcher"], event["tool_name"])
    agent = shlex.split(entry["hooks"][0]["command"])[2].split("--agent ")[1].split(")")[0]
    reply = hook.run_hook(agent, json.dumps(event), {})
    decision = json.loads(reply.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("agentprint: BLOCK")


# ---- Codex ---------------------------------------------------------------------------------------


def test_codex_marketplace_and_manifest():
    market = _load(ROOT / ".agents" / "plugins" / "marketplace.json")
    assert market["name"] == "agentprint"
    [entry] = market["plugins"]
    assert entry["name"] == "agentprint" and entry["category"]
    assert entry["source"]["source"] == "local"
    path = entry["source"]["path"]
    assert path.startswith("./") and (ROOT / path / "plugin.json").is_file()
    assert entry["policy"]["installation"] in {"AVAILABLE", "INSTALLED_BY_DEFAULT", "NOT_AVAILABLE"}
    assert entry["policy"]["authentication"]
    manifest = _load(PLUGIN / "codex" / "plugin.json")
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert {"name", "version", "description"} <= set(manifest)
    assert manifest["version"] == __version__


def test_codex_mcp_is_a_stdio_server():
    mcp = _load(PLUGIN / "codex" / "mcp.json")
    assert mcp["mcpServers"]["agentprint"]["type"] == "stdio"
    assert (PLUGIN / "codex" / "skills" / "agentprint" / "SKILL.md").is_file()


# ---- GitHub Copilot CLI --------------------------------------------------------------------------


def test_copilot_marketplace_and_manifest():
    market = _load(ROOT / ".github" / "plugin" / "marketplace.json")
    assert market["name"] == "agentprint" and market["owner"]["name"]
    [entry] = market["plugins"]
    folder = ROOT / entry["source"]
    manifest = _load(folder / ".github" / "plugin" / "plugin.json")
    assert manifest["name"] == "agentprint" and manifest["version"] == __version__
    assert len(manifest["description"]) <= 1024
    # Copilot reads the first manifest it finds: no other one may sit in front of ours.
    others = [m for m in (".plugin/plugin.json", "plugin.json") if (folder / m).exists()]
    assert others == []


def test_copilot_hook_file_is_the_one_install_writes():
    launch = gen.LAUNCH
    [edit] = [
        e for e in ALL_INTEGRATIONS["copilot"].edits(gen.NOWHERE, {}, launch) if "hook" in e.what
    ]
    assert (PLUGIN / "copilot" / "hooks.json").read_text() == edit.apply(None)


def test_copilot_mcp_server_is_local_with_all_tools():
    server = _load(PLUGIN / "copilot" / ".mcp.json")["mcpServers"]["agentprint"]
    assert server["type"] == "local" and server["tools"] == ["*"]


# ---- Gemini CLI ----------------------------------------------------------------------------------


def test_gemini_extension_sits_at_the_repository_root():
    manifest = _load(ROOT / "gemini-extension.json")
    assert manifest["name"] == "agentprint" and manifest["version"] == __version__
    assert (ROOT / manifest["contextFileName"]).read_text().endswith(INSTRUCTIONS)
    assert set(manifest["mcpServers"]["agentprint"]) == {"command", "args"}  # no `type`
    [entry] = _load(ROOT / "hooks" / "hooks.json")["hooks"]["BeforeTool"]
    assert entry["matcher"] == "run_shell_command"
    assert entry["hooks"][0]["timeout"] == 60000  # milliseconds
