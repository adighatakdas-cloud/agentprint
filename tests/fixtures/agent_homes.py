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


# --- MCP-config agents ------------------------------------------------------------------------


def claude_desktop(home: Path) -> None:
    app = home / "Library" / "Application Support" / "Claude"
    brave = stdio("BRAVE_API_KEY", fakes.GENERIC_VALUE, "@brave/brave-search-mcp-server")
    write(app / "claude_desktop_config.json", json.dumps({"mcpServers": {"brave-search": brave}}))
    write(home / "Library" / "Logs" / "Claude" / "mcp-server-github.log", f"token {fakes.GITHUB}\n")


def claude_desktop_windows(home: Path) -> None:
    servers = {"stripe": stdio("STRIPE_SECRET_KEY", fakes.STRIPE)}
    config = home / WIN_APPDATA / "Claude" / "claude_desktop_config.json"
    write(config, json.dumps({"mcpServers": servers}))


def vscode(home: Path) -> None:
    """VS Code's user and profile mcp.json (read by the Copilot module)."""
    servers = {"github": stdio("GITHUB_TOKEN", fakes.GITHUB)}
    write(home / MAC_CODE / "mcp.json", json.dumps({"servers": servers, "inputs": []}))
    profile = {"servers": {"aws": stdio("AWS_ACCESS_KEY_ID", fakes.AWS)}}
    write(home / MAC_CODE / "profiles" / "-5f3a1b" / "mcp.json", json.dumps(profile))


def kiro(home: Path) -> None:
    base = home / ".kiro"
    servers = {
        "github": stdio("GITHUB_PERSONAL_ACCESS_TOKEN", fakes.GITHUB),
        "api": remote(fakes.OPENAI),
    }
    write(base / "settings" / "mcp.json", json.dumps({"mcpServers": servers}))
    write(base / "settings" / "cli.json", json.dumps({"chat.defaultModel": "auto"}))
    hook = {
        "name": "notify",
        "trigger": "Stop",
        "action": {"type": "command", "command": "curl -s https://hooks.example.invalid/x"},
    }
    write(base / "hooks" / "notify.json", json.dumps({"version": "v1", "hooks": [hook]}))
    write(base / "steering" / "product.md", "We build demos.\n")  # instructions: not read


def kiro_project(root: Path) -> None:
    servers = {"slack": stdio("SLACK_BOT_TOKEN", fakes.SLACK)}
    write(root / ".kiro" / "settings" / "mcp.json", json.dumps({"mcpServers": servers}))
    write(root / ".kiro" / "specs" / "login" / "design.md", "# design\n")  # plans: not read


def factory(home: Path) -> None:
    base = home / ".factory"
    model = {"model": "gpt-x", "baseUrl": "https://api.example.invalid", "apiKey": fakes.OPENAI}
    write(base / "settings.json", json.dumps({"customModels": [model]}))
    write(base / "mcp.json", json.dumps({"mcpServers": {"api": remote(fakes.STRIPE)}}))
    hook = {"type": "command", "command": "wget -qO- https://hooks.example.invalid/x"}
    write(
        base / "hooks.json", json.dumps({"PreToolUse": [{"matcher": "Execute", "hooks": [hook]}]})
    )
    turn = {"role": "user", "content": f"key {fakes.AWS}"}
    write(base / "sessions" / "-Users-demo-app" / "s1.jsonl", jsonl(turn))


def factory_project(root: Path) -> None:
    write(
        root / ".factory" / "mcp.json",
        json.dumps({"mcpServers": {"gh": stdio("GH_TOKEN", fakes.GITHUB)}}),
    )
    write(root / ".factory" / "settings.local.json", json.dumps({"model": "x"}))  # personal


def augment(home: Path) -> None:
    base = home / ".augment"
    hook = {"type": "command", "command": "curl -s https://hooks.example.invalid/x"}
    settings = {
        "mcpServers": {"context7": stdio("CONTEXT7_API_KEY", fakes.GENERIC_VALUE)},
        "hooks": {"PreToolUse": [{"matcher": "bash", "hooks": [hook]}]},
    }
    write(base / "settings.json", json.dumps(settings))
    # Augment's own login: never secret-scanned.
    write(base / "session.json", json.dumps({"accessToken": fakes.GENERIC_VALUE}))


def amp(home: Path) -> None:
    config = home / ".config" / "amp"
    servers = {"linear": stdio("LINEAR_API_KEY", fakes.STRIPE), "api": remote(fakes.OPENAI)}
    write(config / "settings.json", json.dumps({"amp.mcpServers": servers}))
    write(
        home / ".local" / "share" / "amp" / "secrets.json", json.dumps({"apiKey": fakes.ANTHROPIC})
    )


def junie(home: Path) -> None:
    base = home / ".junie"
    write(base / "mcp" / "mcp.json", json.dumps({"mcpServers": {"api": remote(fakes.GITHUB)}}))
    hook = {"type": "command", "command": "nc logs.example.invalid 9000"}
    write(base / "config.json", json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}}))
    session = base / "sessions" / "s-0001"
    write(session / "transcript.md", f"user: my key is {fakes.ANTHROPIC}\n")
    write(session / "events.jsonl", jsonl({"type": "user", "text": "hello"}))


# --- Continue, Qwen Code ----------------------------------------------------------------------


def continue_dev(home: Path) -> None:
    base = home / ".continue"
    write(
        base / "config.yaml",
        "name: Local\nversion: 1.0.0\nschema: v1\n"
        f"models:\n  - name: gpt\n    provider: openai\n    apiKey: {fakes.OPENAI}\n"
        "mcpServers:\n  - name: github\n    command: npx\n"
        f"    env:\n      GITHUB_PERSONAL_ACCESS_TOKEN: {fakes.GITHUB}\n",
    )
    search = {"mcpServers": {"search": remote(fakes.SLACK)}}
    write(base / "mcpServers" / "search.json", json.dumps(search))
    session = {"sessionId": "s1", "history": [{"message": {"content": f"key {fakes.AWS}"}}]}
    write(base / "sessions" / "s1.json", json.dumps(session))
    write(base / "logs" / "prompt.log", f"==========\nuse {fakes.STRIPE}\n")
    # Continue's own key stores: never secret-scanned.
    write(base / ".env", f"ANTHROPIC_API_KEY={fakes.ANTHROPIC}\n")
    write(base / "auth.json", json.dumps({"accessToken": fakes.GENERIC_VALUE}))


def qwen(home: Path) -> None:
    base = home / ".qwen"
    hook = {"type": "command", "command": "curl -s https://hooks.example.invalid/x"}
    settings = {
        "mcpServers": {"github": stdio("GITHUB_TOKEN", fakes.GITHUB)},
        "env": {"OPENAI_API_KEY": fakes.OPENAI},
        "hooks": {"PreToolUse": [{"matcher": "run_shell_command", "hooks": [hook]}]},
    }
    write(base / "settings.json", json.dumps(settings, indent=2))
    turn = {"type": "user", "message": {"parts": [{"text": f"token {fakes.SLACK}"}]}}
    write(base / "projects" / "-Users-demo-app" / "chats" / "s1.jsonl", jsonl(turn))
    write(base / "projects" / "-Users-demo-app" / "memory" / "notes.md", "Prefers tabs.\n")
    write(base / "tmp" / "0f3a9c" / "shell_history", f"export AWS_ACCESS_KEY_ID={fakes.AWS}\n")
    # Qwen Code's own key stores: never secret-scanned.
    write(base / "oauth_creds.json", json.dumps({"access_token": fakes.GENERIC_VALUE}))
    write(base / ".env", f"OPENAI_API_KEY={fakes.OPENAI}\n")
