"""Fake home and project folders, built at test time.

Each agent's files copy the STRUCTURE of the real ones (names, nesting, key names) as verified
on macOS; every value is synthetic and every key comes from tests.fakes.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from tests import fakes


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _jsonl(*rows: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def _sqlite(path: Path, table: str, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f'CREATE TABLE "{table}" (key TEXT, value BLOB)')
    con.executemany(f'INSERT INTO "{table}" VALUES (?, ?)', rows)
    con.commit()
    con.close()


def _claude_and_codex_home(home: Path) -> None:
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


def _cursor_home(home: Path) -> None:
    cursor = home / ".cursor"
    server = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
    server["env"] = {"GITHUB_PERSONAL_ACCESS_TOKEN": fakes.GITHUB}
    _write(cursor / "mcp.json", json.dumps({"mcpServers": {"github": server}}))
    cli = {"version": 1, "editor": {"vimMode": False}, "approvalMode": "unrestricted"}
    cli["permissions"] = {"allow": ["Shell(ls)", "Shell(*)"], "deny": []}
    _write(cursor / "cli-config.json", json.dumps(cli))
    hook = {"command": "curl -s -X POST https://hooks.example.invalid/done"}
    _write(cursor / "hooks.json", json.dumps({"version": 1, "hooks": {"stop": [hook]}}))
    turn = {"role": "user", "message": {"content": [{"type": "text", "text": fakes.OPENAI}]}}
    transcripts = cursor / "projects" / "Users-demo-app" / "agent-transcripts"
    _write(transcripts / "0001" / "0001.jsonl", _jsonl(turn))
    user = home / "Library" / "Application Support" / "Cursor" / "User"
    bubble = json.dumps({"type": 1, "text": f"aws key {fakes.AWS}"}).encode()
    _sqlite(user / "globalStorage" / "state.vscdb", "cursorDiskKV", [("bubbleId:c:b", bubble)])
    _sqlite(user / "globalStorage" / "state.vscdb.backup", "cursorDiskKV", [("k", bubble)])
    _write(user / "settings.json", '{\n  // editor settings\n  "editor.fontSize": 13,\n}\n')


@pytest.fixture
def fake_home(tmp_path) -> Path:
    """Claude Code and Codex files (week 1)."""
    home = tmp_path / "home"
    _claude_and_codex_home(home)
    return home


@pytest.fixture
def agents_home(tmp_path) -> Path:
    """Cursor, Gemini CLI, Windsurf and OpenCode files."""
    home = tmp_path / "agents-home"
    _cursor_home(home)
    return home


@pytest.fixture
def fake_project(tmp_path) -> Path:
    root = tmp_path / "proj"
    allow = [f"Bash(export ANTHROPIC_API_KEY={fakes.ANTHROPIC})"]
    _write(root / ".claude" / "settings.local.json", json.dumps({"permissions": {"allow": allow}}))
    _write(root / "package.json", json.dumps({"name": "demo-pkg", "version": "1.0.0"}))
    _write(root / "index.js", "module.exports = 1;\n")
    return root


@pytest.fixture
def agents_project(tmp_path) -> Path:
    """Project-level files of Cursor, Gemini CLI, Windsurf and OpenCode, plus personal files."""
    root = tmp_path / "agents-proj"
    remote = {"url": "https://mcp.example.invalid/mcp"}
    remote["headers"] = {"Authorization": f"Bearer {fakes.GENERIC_VALUE}"}
    _write(root / ".cursor" / "mcp.json", json.dumps({"mcpServers": {"api": remote}}))
    _write(root / ".cursor" / "rules" / "style.mdc", "Use tabs.\n")
    return root
