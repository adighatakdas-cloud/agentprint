"""Strict mode: when agentprint cannot check a publish or push, block instead of allowing."""

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from agentprint import __version__, guard, hook, install
from agentprint.cli import app
from agentprint.guard import evaluate
from agentprint.integrations.base import BLOCK_EXIT

runner = CliRunner()
REAL_REPORTED_VERSION = install.reported_version


@pytest.fixture(autouse=True)
def fixed_launch(monkeypatch):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["/usr/bin/agentprint"])
    monkeypatch.setattr(install, "reported_version", lambda argv: install.STRICT_SINCE)


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


# ---- strict mode through the hook launcher ---------------------------------------------------

# Runs the real `agentprint guard` with an agent integration that fails even to answer, so
# run_hook falls back to its bare reply.
_BROKEN_GUARD = """
import sys
from agentprint import hook
class Broken:
    def parse_hook(self, payload, env):
        raise RuntimeError("bug")
    def reply(self, verdict):
        raise RuntimeError("bug")
hook.ALL_INTEGRATIONS["claude-code"] = Broken()
from agentprint.cli import app
sys.argv = ["agentprint", *sys.argv[1:]]
app()
"""


@pytest.mark.parametrize(
    ("flags", "blocked"), [((), False), (("--strict",), True)], ids=["default", "strict"]
)
def test_bare_fallback_blocks_through_the_launcher_only_in_strict(tmp_path, flags, blocked):
    script = tmp_path / "agentprint-hook"
    script.write_text(install.launcher_script([sys.executable, "-c", _BROKEN_GUARD]))
    raw = claude_payload("npm publish", tmp_path)
    result = subprocess.run(
        ["sh", str(script), "--agent", "claude-code", *flags],
        input=raw,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if blocked:  # exit 2 is Claude Code's "block"; stderr is the reason it shows
        assert result.returncode == 2, result.stderr
        assert "internal error (strict mode); command blocked" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "blocked" not in result.stderr


def test_bare_strict_fallback_uses_the_launchers_block_code(monkeypatch):
    class Broken:
        def parse_hook(self, payload, env):
            raise RuntimeError("bug")

        def reply(self, verdict):
            raise RuntimeError("bug")

    monkeypatch.setitem(hook.ALL_INTEGRATIONS, "claude-code", Broken())
    assert hook.run_hook("claude-code", "{}", {}, strict=True).exit_code == BLOCK_EXIT
    assert hook.run_hook("claude-code", "{}", {}).exit_code == 0


# ---- install --strict with an older permanent agentprint on PATH -----------------------------

UVX = ["/opt/uv/uvx", f"agentprint=={__version__}"]


def _permanent(tmp_path, monkeypatch, prints: str, status: int = 0) -> str:
    """A permanent agentprint on PATH that prints `prints` for --version (and notes each run)."""
    exe = tmp_path / "bin" / "agentprint"
    exe.parent.mkdir()
    ran = tmp_path / "ran"
    exe.write_text(f"#!/bin/sh\necho run >> '{ran}'\nprintf '%s\\n' '{prints}'\nexit {status}\n")
    exe.chmod(0o755)
    monkeypatch.setattr(install, "resolve_agentprint", lambda: [str(exe)])
    monkeypatch.setattr(install, "reported_version", REAL_REPORTED_VERSION)
    monkeypatch.setattr(install, "pinned_uvx", lambda: list(UVX))
    return str(exe)


def _strict_install(home, *flags):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    args = ["install", "--home", str(home), "--agent", "claude-code", *flags]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    launcher = (home / ".agentprint" / "bin" / "agentprint-hook").read_text()
    return result.output, launcher


@pytest.mark.parametrize(
    ("prints", "status", "why"),
    [
        ("agentprint 0.1.0", 0, "is version 0.1.0, which ignores strict mode"),
        ("agentprint 0.0.9", 0, "is version 0.0.9, which ignores strict mode"),
        ("Usage: agentprint [OPTIONS]", 0, "did not report its version"),
        ("agentprint 0.1.1", 1, "did not report its version"),  # it failed
    ],
    ids=["0.1.0", "0.0.9", "no version", "failed"],
)
def test_strict_install_passes_over_an_agentprint_that_would_ignore_it(
    tmp_path, monkeypatch, prints, status, why
):
    exe = _permanent(tmp_path, monkeypatch, prints, status)
    output, launcher = _strict_install(tmp_path / "home", "--strict")
    assert exe not in launcher and "agentprint==" + __version__ in launcher
    assert f"the agentprint at {exe} {why}" in output
    assert "uv tool upgrade agentprint" in output
    assert "Agents will start agentprint with: " + " ".join(UVX) in output
    assert "no permanent install found" not in output
    assert "Strict mode: a publish or push agentprint cannot check will be blocked." in output


@pytest.mark.parametrize("prints", ["agentprint 0.1.1", "agentprint 0.2.0", "agentprint 1.0.0"])
def test_strict_install_keeps_an_agentprint_that_knows_strict(tmp_path, monkeypatch, prints):
    exe = _permanent(tmp_path, monkeypatch, prints)
    output, launcher = _strict_install(tmp_path / "home", "--strict")
    assert exe in launcher and "agentprint==" not in launcher
    assert "ignores strict mode" not in output and "did not report" not in output
    assert "will be blocked" in output


def test_default_install_does_not_run_the_version_check(tmp_path, monkeypatch):
    exe = _permanent(tmp_path, monkeypatch, "agentprint 0.1.0")
    output, launcher = _strict_install(tmp_path / "home")
    assert exe in launcher and not (tmp_path / "ran").exists()
    assert "Strict mode" not in output


def test_reported_version_of_a_missing_agentprint_is_none(tmp_path):
    assert REAL_REPORTED_VERSION([str(tmp_path / "gone" / "agentprint")]) is None


def test_uvx_running_this_version_is_trusted_without_running_it(monkeypatch):
    monkeypatch.setattr(install, "reported_version", _boom)
    assert install.ignores_strict(["/opt/uv/uvx", f"agentprint=={__version__}"]) is None
