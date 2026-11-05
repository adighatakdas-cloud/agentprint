import json
from pathlib import Path

import pytest

from tests import fakes

# tools/ (research scripts) is not shipped in the source package; skip its tests there.
if not (Path(__file__).resolve().parent.parent / "tools").is_dir():
    collect_ignore_glob = ["test_ecoscan_*.py"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _jsonl(*rows: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


@pytest.fixture
def fake_home(tmp_path) -> Path:
    home = tmp_path / "home"
    claude, codex = home / ".claude", home / ".codex"
    _write(claude / "history.jsonl", _jsonl({"display": f"my key is {fakes.ANTHROPIC}"}))
    _write(
        claude / "settings.json",
        json.dumps(
            {"permissions": {"allow": [f"Bash(curl -H 'Authorization: Bearer {fakes.OPENAI}' x)"]}}
        ),
    )
    _write(claude / "paste-cache" / "abc.txt", f"AWS_ACCESS_KEY_ID={fakes.AWS}\n")
    msg = {"type": "user", "message": {"role": "user", "content": f"push with {fakes.GITHUB}"}}
    _write(claude / "projects" / "-tmp-demo" / "s1.jsonl", _jsonl(msg, msg))
    _write(codex / "config.toml", f'[mcp_servers.demo]\nenv = {{ API_TOKEN = "{fakes.STRIPE}" }}\n')
    _write(
        codex / "auth.json", json.dumps({"OPENAI_API_KEY": fakes.OPENAI})
    )  # expected; not scanned
    row = {"type": "response_item", "payload": {"content": [{"text": f"use {fakes.SLACK}"}]}}
    _write(codex / "sessions" / "2026" / "10" / "01" / "rollout-x.jsonl", _jsonl(row))
    _write(codex / "history.jsonl", _jsonl({"text": f"maps key {fakes.GOOGLE}"}))
    return home


@pytest.fixture
def fake_project(tmp_path) -> Path:
    root = tmp_path / "proj"
    allow = [f"Bash(export ANTHROPIC_API_KEY={fakes.ANTHROPIC})"]
    _write(root / ".claude" / "settings.local.json", json.dumps({"permissions": {"allow": allow}}))
    _write(root / "package.json", json.dumps({"name": "demo-pkg", "version": "1.0.0"}))
    _write(root / "index.js", "module.exports = 1;\n")
    return root
