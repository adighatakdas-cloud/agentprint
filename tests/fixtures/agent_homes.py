"""Fake home and project folders for the agents graduated from scan-only, one builder each.

Built at test time, never committed as files: every key comes from tests.fakes. Each builder
copies the STRUCTURE of the agent's documented files (names, nesting, key names) and puts a fake
key in each file the docs say can hold one: key stores (never secret-scanned, only checked for
who can read them), history (AP001), settings (AP002) and MCP config (AP005).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests import fakes

MAC_CODE = Path("Library") / "Application Support" / "Code" / "User"
WIN_APPDATA = Path("AppData") / "Roaming"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def jsonl(*rows: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def sqlite(path: Path, table: str, rows: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f'CREATE TABLE "{table}" (id TEXT, data TEXT)')
    con.executemany(f'INSERT INTO "{table}" VALUES (?, ?)', rows)
    con.commit()
    con.close()
    return path


def stdio(env_name: str, key: str, package: str = "demo-mcp") -> dict:
    return {"command": "npx", "args": ["-y", package], "env": {env_name: key}}


def remote(token: str) -> dict:
    return {
        "url": "https://mcp.example.invalid/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }


# --- Cline -----------------------------------------------------------------------------------


def cline(home: Path) -> None:
    data = home / ".cline" / "data"
    # Cline's own key stores: never secret-scanned.
    write(data / "secrets.json", json.dumps({"apiKey": fakes.ANTHROPIC}))
    write(data / "settings" / "providers.json", json.dumps({"openai": {"apiKey": fakes.OPENAI}}))
    servers = {"github": stdio("GITHUB_PERSONAL_ACCESS_TOKEN", fakes.GITHUB)}
    write(data / "settings" / "cline_mcp_settings.json", json.dumps({"mcpServers": servers}))
    write(data / "settings" / "global-settings.json", json.dumps({"mode": "act"}))
    write(data / "globalState.json", json.dumps({"apiProvider": "anthropic"}))
    turn = {"role": "user", "content": f"use {fakes.SLACK}"}
    write(data / "sessions" / "s-0001" / "messages.jsonl", jsonl(turn))
    sqlite(data / "db" / "sessions.db", "messages", [("m1", f"aws key {fakes.AWS}")])
    write(data / "logs" / "cline.log", "started\n")
    task = home / MAC_CODE / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" / "t1"
    history = [{"role": "user", "content": [{"type": "text", "text": f"deploy {fakes.STRIPE}"}]}]
    write(task / "api_conversation_history.json", json.dumps(history))
