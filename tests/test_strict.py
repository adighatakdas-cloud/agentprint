"""Strict mode: when agentprint cannot check a publish or push, block instead of allowing."""

import json

import pytest
from typer.testing import CliRunner

from agentprint import guard, hook, install
from agentprint.cli import app
from agentprint.guard import evaluate

runner = CliRunner()


@pytest.fixture(autouse=True)
def fixed_launch(monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["/usr/bin/agentprint"])


def _boom(*_args, **_kwargs):
    raise RuntimeError("bug")


def claude_payload(command, cwd):
    return json.dumps({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}})


def deny_reason(reply):
    out = json.loads(reply.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


# ---- evaluate() ---------------------------------------------------------------------------


def test_cannot_check_default_allows_strict_blocks(tmp_path):
    default = evaluate("npm publish", tmp_path)  # no package.json: CannotCheck
    assert not default.block and "could not check" in default.warning
    strict = evaluate("npm publish", tmp_path, strict=True)
    assert strict.block and "could not check" in strict.reason
    assert "AGENTPRINT_STRICT" in strict.reason


def test_unreadable_archive_strict_blocks(tmp_path):
    (tmp_path / "broken.tgz").write_bytes(b"not a tarball")  # PackError
    assert not evaluate("npm publish broken.tgz", tmp_path).block
    assert evaluate("npm publish broken.tgz", tmp_path, strict=True).block


def test_unsure_detection_strict_blocks(tmp_path):
    command = 'npm publish "$(cat tarball-name)"'
    default = evaluate(command, tmp_path)
    assert default is not None and not default.block
    assert evaluate(command, tmp_path, strict=True).block


def test_internal_error_in_decide_strict_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "check_push", _boom)
    assert not evaluate("git push", tmp_path).block
    verdict = evaluate("git push", tmp_path, strict=True)
    assert verdict.block and "RuntimeError" in verdict.reason and "bug" not in verdict.reason


def test_internal_error_in_detect_strict_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "detect", _boom)
    for command in ("npm publish", "git push origin main"):
        assert not evaluate(command, tmp_path).block
        assert evaluate(command, tmp_path, strict=True).block


def test_parser_crash_on_ordinary_command_allows_even_in_strict(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "detect", _boom)
    verdict = evaluate("ls", tmp_path, strict=True)
    assert not verdict.block and "internal error" in verdict.warning


def test_strict_leaves_clean_and_irrelevant_commands_alone(tmp_path, monkeypatch):
    assert evaluate("ls", tmp_path, strict=True) is None
    monkeypatch.setattr(guard, "check_push", lambda cwd: [])
    verdict = evaluate("git push", tmp_path, strict=True)
    assert not verdict.block and verdict.warning is None


# ---- the hook entry point (Claude Code) ----------------------------------------------------


def test_hook_cannot_check_default_vs_strict_flag(tmp_path):
    raw = claude_payload("npm publish", tmp_path)
    allowed = hook.run_hook("claude-code", raw, {})
    assert "permissionDecision" not in allowed.stdout
    assert "could not check" in json.loads(allowed.stdout)["systemMessage"]
    assert "could not check" in deny_reason(hook.run_hook("claude-code", raw, {}, strict=True))


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_hook_strict_from_env(tmp_path, value):
    raw = claude_payload("npm publish", tmp_path)
    reply = hook.run_hook("claude-code", raw, {"AGENTPRINT_STRICT": value})
    assert "could not check" in deny_reason(reply)


@pytest.mark.parametrize("value", ["", "0", "no"])
def test_hook_env_off_values_stay_default(tmp_path, value):
    raw = claude_payload("npm publish", tmp_path)
    reply = hook.run_hook("claude-code", raw, {"AGENTPRINT_STRICT": value})
    assert "permissionDecision" not in reply.stdout


def test_hook_internal_error_strict_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "evaluate", _boom)
    raw = claude_payload("npm publish", tmp_path)
    assert "permissionDecision" not in hook.run_hook("claude-code", raw, {}).stdout
    assert "internal error" in deny_reason(hook.run_hook("claude-code", raw, {}, strict=True))


def test_hook_unreadable_input_strict_blocks():
    assert "permissionDecision" not in hook.run_hook("claude-code", "not json", {}).stdout
    assert "read the hook input" in deny_reason(
        hook.run_hook("claude-code", "not json", {}, strict=True)
    )


# ---- the CLI ------------------------------------------------------------------------------


def test_guard_cli_strict_flag_and_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = claude_payload("npm publish", tmp_path)
    plain = runner.invoke(app, ["guard"], input=raw)
    assert plain.exit_code == 0 and "permissionDecision" not in plain.output
    flag = runner.invoke(app, ["guard", "--strict"], input=raw)
    assert "could not check" in deny_reason(hook.HookReply(stdout=flag.stdout))
    env = runner.invoke(app, ["guard"], input=raw, env={"AGENTPRINT_STRICT": "1"})
    assert "permissionDecision" in env.output


def test_pack_exits_2_when_it_cannot_check(tmp_path):
    result = runner.invoke(app, ["pack", str(tmp_path / "missing-1.0.0.tgz")])
    assert result.exit_code == 2


# ---- install --strict ---------------------------------------------------------------------


def test_install_strict_writes_strict_hooks_default_does_not(tmp_path):
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    runner.invoke(app, ["install", "--home", str(tmp_path), "--agent", "claude-code"])
    assert "--strict" not in settings.read_text()
    result = runner.invoke(
        app, ["install", "--home", str(tmp_path), "--agent", "claude-code", "--strict"]
    )
    assert result.exit_code == 0, result.output
    assert "--strict" in settings.read_text()
    runner.invoke(app, ["install", "--home", str(tmp_path), "--agent", "claude-code"])
    assert "--strict" not in settings.read_text()  # a plain install goes back to default
    runner.invoke(app, ["uninstall", "--home", str(tmp_path), "--agent", "claude-code"])
    assert not settings.exists() or "agentprint-hook" not in settings.read_text()


def test_install_strict_opencode_plugin_passes_flag(tmp_path):
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    runner.invoke(app, ["install", "--home", str(tmp_path), "--agent", "opencode", "--strict"])
    plugin = (tmp_path / ".config" / "opencode" / "plugins" / "agentprint.js").read_text()
    assert '"--strict"' in plugin
