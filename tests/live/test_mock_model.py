"""The mock model and the fake tools themselves (fast; runs in the normal suite)."""

import json
import subprocess
import urllib.request

import pytest

from tests.live.harness import agent_env, make_shims, package
from tests.live.mock_model import MockModel, Script

BASH = {"name": "Bash", "input_schema": {"type": "object",
        "properties": {"command": {"type": "string"}}, "required": ["command"]}}  # fmt: skip
SHELL_ARRAY = {"type": "function", "name": "shell", "parameters": {"type": "object",
               "properties": {"command": {"type": "array"}, "workdir": {"type": "string"}},
               "required": ["command", "workdir"]}}  # fmt: skip
TERMINAL = {"type": "function", "function": {"name": "terminal", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}  # fmt: skip


def post(url: str, body: dict) -> str:
    request = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode()


@pytest.fixture
def script(tmp_path):
    return Script("npm publish", "/work", tmp_path / "requests.jsonl")


def test_anthropic_tool_call_then_final_text(script):
    with MockModel(script) as url:
        first = json.loads(post(f"{url}/v1/messages", {"messages": [
            {"role": "user", "content": "go"}], "tools": [BASH]}))  # fmt: skip
        [block] = first["content"]
        assert (block["type"], block["name"], block["input"]) == (
            "tool_use", "Bash", {"command": "npm publish"})  # fmt: skip
        result = {"type": "tool_result", "tool_use_id": block["id"],
                  "content": "agentprint: BLOCK — 2 problems"}  # fmt: skip
        second = json.loads(post(f"{url}/v1/messages", {"messages": [
            {"role": "user", "content": [result]}], "tools": [BASH]}))  # fmt: skip
        assert second["content"][0]["type"] == "text"
    assert script.tool_results() == ["agentprint: BLOCK — 2 problems"]


def test_anthropic_stream_is_server_sent_events(script):
    with MockModel(script) as url:
        text = post(
            f"{url}/v1/messages",
            {"stream": True, "tools": [BASH], "messages": [{"role": "user", "content": "go"}]},
        )
    assert "event: message_start" in text and '"input_json_delta"' in text


def test_responses_api_fills_the_schema(script):
    with MockModel(script) as url:
        body = {"input": [{"type": "message", "role": "user", "content": "go"}],
                "tools": [SHELL_ARRAY], "stream": True}  # fmt: skip
        text = post(f"{url}/v1/responses", body)
        done = [json.loads(line[6:]) for line in text.splitlines()
                if line.startswith("data: ") and "output_item.done" in line]  # fmt: skip
        args = json.loads(done[0]["item"]["arguments"])
        assert args == {"command": ["bash", "-lc", "npm publish"], "workdir": "/work"}
        out = {"type": "function_call_output", "call_id": "c", "output": "blocked"}
        post(f"{url}/v1/responses", {"input": [out], "tools": [SHELL_ARRAY]})
    assert script.tool_results() == ["blocked"]


def test_chat_completions_stream_ends_with_done(script):
    with MockModel(script) as url:
        text = post(
            f"{url}/v1/chat/completions",
            {"stream": True, "tools": [TERMINAL], "messages": [{"role": "user", "content": "go"}]},
        )
        assert text.rstrip().endswith("data: [DONE]") and '"terminal"' in text
        post(f"{url}/v1/chat/completions", {"messages": [
            {"role": "tool", "tool_call_id": "x", "content": "ok"}]})  # fmt: skip
    assert script.tool_results() == ["ok"]


def test_side_requests_without_tools_get_text(script):
    with MockModel(script) as url:
        reply = json.loads(post(f"{url}/v1/messages", {"messages": [
            {"role": "user", "content": "title?"}]}))  # fmt: skip
    assert reply["content"][0]["type"] == "text"


def test_shims_mark_publish_and_push_and_list_the_package(tmp_path):
    bin_dir, markers = tmp_path / "bin", tmp_path / "markers"
    make_shims(bin_dir, markers)
    pkg = package(tmp_path / "pkg", leaky=True)
    listing = subprocess.run([str(bin_dir / "npm"), "pack", "--dry-run", "--json"],
                             cwd=pkg, capture_output=True, text=True, check=True)  # fmt: skip
    files = {f["path"] for f in json.loads(listing.stdout)[0]["files"]}
    assert ".claude/settings.local.json" in files
    subprocess.run([str(bin_dir / "npm"), "publish"], cwd=pkg, check=True, capture_output=True)
    subprocess.run([str(bin_dir / "git"), "push"], cwd=pkg, check=True, capture_output=True)
    assert (markers / "PUBLISHED").read_text().strip() == str(pkg)
    assert (markers / "PUSHED").exists()


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_login_shells_still_find_the_fake_npm(tmp_path, shell):
    import shutil

    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    make_shims(bin_dir, tmp_path / "markers")
    env = agent_env(home, bin_dir, [])
    found = subprocess.run([shell, "-lc", "command -v npm"], env=env, capture_output=True,
                           text=True, check=True).stdout.strip()  # fmt: skip
    assert found == str(bin_dir / "npm")
    assert env["npm_config_dry_run"] == "true"  # and a real npm could only dry-run anyway
