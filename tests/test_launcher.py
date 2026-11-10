"""The hook launcher's exit codes and output, per agent (review item 14).

Allow = exit 0 with the agent's native allow output; block = the agent's own block (JSON on
exit 0, or exit 2 for Windsurf); any failure of agentprint itself = allow (exit 0) plus a
warning on stderr. Exit 1 is never used: GitHub Copilot's preToolUse reads any non-zero exit
other than 2 as "deny", and other agents log it as a hook error.
"""

import json
import os
import shlex
import shutil
import subprocess

import pytest

from agentprint import hook, install
from agentprint.guard import COULD_NOT_START, Verdict, strict_block
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.base import BLOCK_EXIT, LAUNCHER_GONE

AGENTS = sorted(ALL_INTEGRATIONS)
# Documented semantics, per agent: (exit code that blocks, how a block is expressed).
BLOCKS = {
    "claude-code": "json",  # https://code.claude.com/docs/en/hooks
    "codex": "json",  # https://learn.chatgpt.com/docs/hooks
    "gemini": "json",  # https://geminicli.com/docs/hooks/reference
    "cursor": "json",  # https://cursor.com/docs/hooks
    "devin": "json",  # https://docs.devin.ai/cli/extensibility/hooks/overview
    "opencode": "json",  # our plugin reads stdout JSON
    "windsurf": "exit2",  # Cascade: only exit 2 blocks (archived docs.windsurf.com hooks page)
    # Copilot: JSON deny on exit 0 (exit 2 also denies; ANY other non-zero exit denies too).
    "copilot": "json",  # https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference
    "hermes": "json",  # https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
    "antigravity": "json",  # exit codes ignored: https://antigravity.google/docs/hooks/
}


def _launch(tmp_path, fake_exit: int, stdout: str = "", stderr: str = "", agent="claude-code"):
    body = f"cat >/dev/null; printf %s {json.dumps(stdout)}; printf %s {json.dumps(stderr)} >&2"
    fake = ["sh", "-c", f"{body}; exit {fake_exit}", "fake"]
    script = tmp_path / "agentprint-hook"
    script.write_text(install.launcher_script(fake))
    return subprocess.run(
        ["sh", str(script), "--agent", agent], input="{}", capture_output=True, text=True
    )


def test_every_agent_has_documented_block_semantics():
    assert set(BLOCKS) == set(ALL_INTEGRATIONS)


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", AGENTS)
def test_allow_passes_through_with_exit_0(tmp_path, agent):
    allow = ALL_INTEGRATIONS[agent].reply(None)
    assert allow.exit_code == 0
    result = _launch(tmp_path, 0, allow.stdout, agent=agent)
    assert result.returncode == 0
    assert result.stdout.strip() == allow.stdout.strip()


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", AGENTS)
def test_block_uses_the_agents_own_mechanism(tmp_path, agent):
    block = ALL_INTEGRATIONS[agent].reply(Verdict(True, "agentprint: BLOCK — 1 problem"))
    result = _launch(tmp_path, block.exit_code, block.stdout, block.stderr, agent=agent)
    if BLOCKS[agent] == "exit2":
        assert block.exit_code == BLOCK_EXIT and result.returncode == 2
        assert "agentprint: BLOCK" in result.stderr
    else:
        assert block.exit_code == 0 and result.returncode == 0
        assert "agentprint: BLOCK" in result.stdout


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("failure", [1, 2, 127])
def test_any_failure_allows_with_exit_0_and_a_warning(tmp_path, agent, failure):
    # 2 is what uv itself exits with on its own errors; it must never turn into a block.
    result = _launch(tmp_path, failure, '{"partial', agent=agent)
    assert result.returncode == 0
    assert "allowed" in result.stderr
    allow = ALL_INTEGRATIONS[agent].reply(None).stdout
    assert result.stdout.strip() == allow.strip()  # Cursor needs valid JSON even then
    if allow:
        json.loads(result.stdout)


@pytest.mark.posix_only
def test_missing_agentprint_allows_with_exit_0(tmp_path):
    script = tmp_path / "agentprint-hook"
    script.write_text(install.launcher_script([str(tmp_path / "gone" / "agentprint")]))
    result = subprocess.run(
        ["sh", str(script), "--agent", "cursor"], input="{}", capture_output=True, text=True
    )
    assert result.returncode == 0 and json.loads(result.stdout) == {"permission": "allow"}


@pytest.mark.parametrize("agent", AGENTS)
def test_guard_itself_never_exits_1(agent):
    for verdict in (None, Verdict(False, warning="could not check")):
        assert ALL_INTEGRATIONS[agent].reply(verdict).exit_code == 0
    assert hook.run_hook(agent, "not json", {}).exit_code == 0


def test_unknown_agent_allows_with_exit_0():
    reply = hook.run_hook("nope", "{}", {})
    assert reply.exit_code == 0 and "unknown agent" in reply.stderr


# ---- strict mode: a launcher that can't start agentprint blocks (Ruling X21) -------------------


def _launch_strict(tmp_path, fake_exit, agent, *, flag=True, env=None):
    fake = ["sh", "-c", f"cat >/dev/null; printf %s '{{\"partial'; exit {fake_exit}", "fake"]
    script = tmp_path / "agentprint-hook"
    script.write_text(install.launcher_script(fake))
    args = ["sh", str(script), "--agent", agent, *(["--strict"] if flag else [])]
    run_env = {"PATH": "/usr/bin:/bin", **(env or {})}
    return subprocess.run(args, input="{}", capture_output=True, text=True, env=run_env)


def _assert_blocked(result, agent):
    assert "blocked (strict mode)" in result.stderr
    if BLOCKS[agent] == "exit2":
        assert result.returncode == 2 and "agentprint: BLOCK (strict mode)" in result.stderr
    else:
        assert result.returncode == 0
        text = json.dumps(json.loads(result.stdout))  # valid JSON the agent reads as "deny"
        assert "agentprint: BLOCK (strict mode)" in text and "could not start" in text
        assert ("deny" in text or "block" in text) and "allow" not in text


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("failure", [1, 2, 127])
def test_strict_flag_blocks_when_agentprint_cannot_start(tmp_path, agent, failure):
    _assert_blocked(_launch_strict(tmp_path, failure, agent), agent)


@pytest.mark.posix_only
@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_agentprint_strict_in_the_environment_blocks_too(tmp_path, value):
    result = _launch_strict(
        tmp_path, 2, "claude-code", flag=False, env={"AGENTPRINT_STRICT": value}
    )
    _assert_blocked(result, "claude-code")


@pytest.mark.posix_only
@pytest.mark.parametrize("value", ["", "0", "no"])
def test_strict_off_values_still_allow(tmp_path, value):
    result = _launch_strict(tmp_path, 2, "cursor", flag=False, env={"AGENTPRINT_STRICT": value})
    assert result.returncode == 0 and json.loads(result.stdout) == {"permission": "allow"}
    assert "allowed" in result.stderr


@pytest.mark.posix_only
def test_strict_missing_agentprint_blocks(tmp_path):
    script = tmp_path / "agentprint-hook"
    script.write_text(install.launcher_script([str(tmp_path / "gone" / "agentprint")]))
    result = subprocess.run(
        ["sh", str(script), "--agent", "cursor", "--strict"],
        input="{}",
        capture_output=True,
        text=True,
    )
    _assert_blocked(result, "cursor")


@pytest.mark.posix_only
def test_strict_still_passes_agentprint_s_own_answers_through(tmp_path):
    """Strict changes only what happens when agentprint fails: its allow and block stand."""
    for verdict in (None, Verdict(True, "agentprint: BLOCK — 1 problem")):
        reply = ALL_INTEGRATIONS["claude-code"].reply(verdict)
        fake = ["sh", "-c", f"cat >/dev/null; printf %s {shlex.quote(reply.stdout)}", "fake"]
        script = tmp_path / "agentprint-hook"
        script.write_text(install.launcher_script(fake))
        result = subprocess.run(
            ["sh", str(script), "--agent", "claude-code", "--strict"],
            input="{}",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 and result.stdout.strip() == reply.stdout.strip()
        assert "strict mode" not in result.stderr


@pytest.mark.posix_only
@pytest.mark.parametrize("agent", AGENTS)
def test_a_strict_hook_blocks_when_the_launcher_itself_is_gone(tmp_path, agent):
    """The hook command answers for a deleted launcher: allow by default, block in strict."""
    allow = ALL_INTEGRATIONS[agent].reply(None).stdout
    gone = str(tmp_path / "gone" / "agentprint-hook")
    strict = install.Launch(hook=gone, mcp=(), strict=True).hook_command(agent, allow)
    result = subprocess.run(strict, shell=True, input="{}", capture_output=True, text=True)
    assert result.stderr.splitlines()[0] == LAUNCHER_GONE
    expected = ALL_INTEGRATIONS[agent].reply(strict_block(COULD_NOT_START))
    if expected.stdout.strip():
        assert result.returncode == 0 and result.stdout.strip() == expected.stdout.strip()
    else:
        assert result.returncode == 2 and "agentprint: BLOCK (strict mode)" in result.stderr
    default = install.Launch(hook=gone, mcp=()).hook_command(agent, allow)
    result = subprocess.run(default, shell=True, input="{}", capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == allow.strip()


OPENCODE_RUN = """
import plugin from "./agentprint.mjs"
const hooks = await plugin.server({ directory: "." })
try {
  await hooks["tool.execute.before"]({ tool: "bash" }, { args: { command: "npm publish" } })
  console.log("allowed")
} catch (err) {
  console.log("blocked: " + err.message.split("\\n")[0])
}
"""


@pytest.mark.posix_only
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(("strict", "env", "expected"), [
    (False, {}, "allowed"),
    (True, {}, "blocked: agentprint: BLOCK (strict mode)"),
    (False, {"AGENTPRINT_STRICT": "1"}, "blocked: agentprint: BLOCK (strict mode)"),
])  # fmt: skip
def test_the_opencode_plugin_blocks_a_missing_launcher_only_in_strict(
    tmp_path, strict, env, expected
):
    from agentprint.integrations import opencode

    launch = install.Launch(hook=str(tmp_path / "gone" / "agentprint-hook"), mcp=(), strict=strict)
    (plugin,) = [e for e in opencode.edits(tmp_path, {}, launch) if e.what.startswith("hook")]
    (tmp_path / "agentprint.mjs").write_text(plugin.content)
    (tmp_path / "run.mjs").write_text(OPENCODE_RUN)
    run_env = {"PATH": os.environ["PATH"], **env}
    done = subprocess.run(
        [shutil.which("node") or "node", "run.mjs"],
        cwd=tmp_path, capture_output=True, text=True, env=run_env, timeout=60,
    )  # fmt: skip
    assert done.stdout.strip() == expected, done.stderr
    assert ("blocked (strict mode)" if "blocked" in expected else "command allowed") in done.stderr
