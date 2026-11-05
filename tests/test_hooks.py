"""`agentprint guard`: each agent's hook payload in, its native reply out."""

import io
import json
import re
import tarfile

import pytest
from typer.testing import CliRunner

from agentprint import hook
from agentprint.cli import app
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.base import BLOCK_EXIT
from tests import fakes

runner = CliRunner()
LEAKY = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}).encode()
PUBLISH = "npm publish demo-1.0.0.tgz"


@pytest.fixture
def project(tmp_path):
    with tarfile.open(tmp_path / "demo-1.0.0.tgz", "w:gz") as tf:
        info = tarfile.TarInfo("package/.claude/settings.local.json")
        info.size = len(LEAKY)
        tf.addfile(info, io.BytesIO(LEAKY))
    return tmp_path


# Payloads as each agent sends them (shapes from the docs cited in agentprint/integrations/).
def payload(agent: str, command: str, cwd) -> dict:
    cwd = str(cwd)
    return {
        "claude-code": {
            "session_id": "s",
            "cwd": cwd,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "x"},
        },
        "codex": {
            "session_id": "s",
            "cwd": cwd,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
        "gemini": {
            "session_id": "s",
            "cwd": cwd,
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": command},
        },
        "cursor": {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": cwd,
            "workspace_roots": [cwd],
        },
        "windsurf": {
            "agent_action_name": "pre_run_command",
            "tool_info": {"command_line": command, "cwd": cwd},
        },
        "devin": {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec",
            "tool_input": {"command": command, "cwd": cwd},
        },
        "opencode": {"command": command, "cwd": cwd},
    }[agent]


def run(agent, data) -> hook.HookReply:
    return hook.run_hook(agent, json.dumps(data), {})


def blocked(agent: str, reply) -> str | None:
    """The reason the agent would see if `reply` blocks, else None."""
    out = json.loads(reply.stdout) if reply.stdout.strip() else {}
    if agent in {"claude-code", "codex"}:
        spec = out.get("hookSpecificOutput", {})
        return spec.get("permissionDecisionReason") if spec.get("permissionDecision") else None
    if agent == "gemini":
        return out.get("reason") if out.get("decision") == "deny" else None
    if agent == "cursor":
        return out.get("agent_message") if out.get("permission") == "deny" else None
    if agent == "windsurf":
        return reply.stderr if reply.exit_code == BLOCK_EXIT else None
    if agent in {"devin", "opencode"}:
        return out.get("reason") if out.get("decision") == "block" else None
    raise AssertionError(agent)


def test_every_integration_has_a_payload_test():
    assert set(ALL_INTEGRATIONS) == {
        "claude-code",
        "codex",
        "cursor",
        "gemini",
        "windsurf",
        "devin",
        "opencode",
    }


@pytest.mark.parametrize("agent", sorted(ALL_INTEGRATIONS))
def test_leaky_publish_is_blocked_in_native_format(agent, project):
    reply = run(agent, payload(agent, PUBLISH, project))
    reason = blocked(agent, reply)
    assert reason and reason.startswith("agentprint: BLOCK — 2 problems to fix before publishing")
    assert fakes.ANTHROPIC not in reply.stdout + reply.stderr


@pytest.mark.parametrize("agent", sorted(ALL_INTEGRATIONS))
def test_ordinary_command_is_not_blocked(agent, project):
    reply = run(agent, payload(agent, "ls -la", project))
    assert blocked(agent, reply) is None
    assert reply.exit_code == 0


def test_claude_allow_is_silent_and_never_says_allow(project):
    reply = run("claude-code", payload("claude-code", "ls", project))
    assert reply.stdout == "" and reply.exit_code == 0


def test_codex_output_only_uses_fields_its_schema_accepts(project):
    out = json.loads(run("codex", payload("codex", PUBLISH, project)).stdout)
    assert set(out) <= {"hookSpecificOutput", "systemMessage"}


def test_gemini_always_prints_json(project):
    assert json.loads(run("gemini", payload("gemini", "ls", project)).stdout) == {}


def test_cursor_allows_clean_commands_without_prompting(project):
    # Hook "allow" does not override Cursor's own allowlist/approval (see cursor.py), so a
    # clean result answers allow instead of adding an extra prompt.
    for command in ("ls", "git stash push", "npm publish ok.tgz"):
        out = json.loads(run("cursor", payload("cursor", command, project)).stdout)
        assert out["permission"] == "allow", command
        assert "user_message" not in out or "could not check" in out["user_message"]


CURSOR_MATCHES = [
    "npm publish",
    "cd pkg && npm publish --access public",
    "pnpm -r publish",
    "yarn npm publish",
    "NODE_ENV=prod npm publish",
    "npx changeset publish",
    "npx lerna publish",
    "npx np",
    "npm run release",
    "uv publish",
    "twine upload dist/*",
    "python -m twine upload dist/*",
    "git push",
    "git -C sub push origin main",
    "git add . && git commit -m x && git push",
    "if npm test; then git push; fi",
]
CURSOR_SKIPS = [
    "git stash push",
    'git commit -m "push notifications"',
    "echo npm publish",
    "rg 'npm publish' .",
    "ls -la",
    "npm test",
    "git status",
]


@pytest.mark.parametrize("command", CURSOR_MATCHES)
def test_cursor_matcher_fires_for_risky_commands(command):
    from agentprint.integrations.cursor import MATCHER

    assert re.search(MATCHER, command), command


@pytest.mark.parametrize("command", CURSOR_SKIPS)
def test_cursor_matcher_skips_ordinary_commands(command):
    from agentprint.integrations.cursor import MATCHER

    assert not re.search(MATCHER, command), command


def test_other_tools_are_ignored():
    data = {"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": "/"}
    assert run("claude-code", data).stdout == ""


def test_garbage_input_fails_open_with_warning():
    reply = hook.run_hook("claude-code", "not json", {})
    assert reply.exit_code == 0
    assert "allowed" in json.loads(reply.stdout)["systemMessage"]


def test_unknown_agent_fails_open():
    reply = hook.run_hook("nope", "{}", {})
    assert reply.exit_code == 0 and "unknown agent" in reply.stderr


def test_internal_error_fails_open_with_visible_warning(monkeypatch, project):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(hook, "evaluate", boom)
    reply = run("claude-code", payload("claude-code", PUBLISH, project))
    assert reply.exit_code == 0
    out = json.loads(reply.stdout)
    assert "hookSpecificOutput" not in out and "allowed" in out["systemMessage"]


def test_guard_cli_reads_stdin_and_replies(project):
    data = json.dumps(payload("claude-code", PUBLISH, project))
    result = runner.invoke(app, ["guard", "--agent", "claude-code"], input=data)
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_cli_tolerates_unknown_extra_args(project):
    data = json.dumps(payload("claude-code", "ls", project))
    result = runner.invoke(app, ["guard", "--agent", "claude-code", "--future-flag"], input=data)
    assert result.exit_code == 0
