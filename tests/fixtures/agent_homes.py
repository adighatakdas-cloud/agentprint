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


# --- SQLite stores: Goose, Kilo Code, Crush ---------------------------------------------------


def goose(home: Path) -> None:
    config = home / ".config" / "goose"
    write(
        config / "config.yaml",
        "GOOSE_PROVIDER: anthropic\n"
        "extensions:\n"
        "  github:\n    type: stdio\n    name: github\n    cmd: npx\n"
        "    args: [-y, '@modelcontextprotocol/server-github']\n"
        f"    envs:\n      GITHUB_PERSONAL_ACCESS_TOKEN: {fakes.GITHUB}\n"
        "  developer:\n    type: builtin\n    name: developer\n    enabled: true\n",
    )
    write(config / "secrets.yaml", f"ANTHROPIC_API_KEY: {fakes.ANTHROPIC}\n")  # key store
    write(config / "history.txt", "hello\n")
    sessions = home / ".local" / "share" / "goose" / "sessions"
    sqlite(sessions / "sessions.db", "messages", [("m1", f"aws {fakes.AWS}")])
    logs = home / ".local" / "state" / "goose" / "logs"
    write(logs / "llm_request.0.jsonl", jsonl({"request": {"messages": [f"key {fakes.STRIPE}"]}}))
    hook = {"type": "command", "command": "curl -s https://hooks.example.invalid/x"}
    plugin = home / ".agents" / "plugins" / "notify" / "hooks" / "hooks.json"
    write(plugin, json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}}))


def goose_windows(home: Path) -> None:
    block = home / WIN_APPDATA / "Block" / "goose"
    write(block / "config" / "secrets.yaml", f"OPENAI_API_KEY: {fakes.OPENAI}\n")
    sqlite(block / "data" / "sessions" / "sessions.db", "messages", [("m1", f"k {fakes.SLACK}")])


def kilo(home: Path) -> None:
    data = home / ".local" / "share" / "kilo"
    sqlite(data / "kilo.db", "part", [("prt_1", f"anthropic key {fakes.ANTHROPIC}")])
    write(data / "auth.json", json.dumps({"anthropic": {"type": "api", "key": fakes.ANTHROPIC}}))
    local = {
        "type": "local",
        "command": ["npx", "-y", "gh-mcp"],
        "environment": {"GH_TOKEN": fakes.GITHUB},
    }
    config = {"$schema": "https://app.kilo.ai/config.json", "mcp": {"gh": local}}
    write(home / ".config" / "kilo" / "kilo.jsonc", "// my config\n" + json.dumps(config))


def kilo_project(root: Path) -> None:
    write(
        root / "kilo.jsonc",
        json.dumps({"mcp": {"api": {"type": "remote", **remote(fakes.OPENAI)}}}),
    )


def crush(home: Path) -> None:
    write(home / ".config" / "crush" / "crushrc", f"provider set openai --api-key {fakes.OPENAI}\n")
    # Crush's own key store (pasted keys): never secret-scanned.
    data = {"providers": {"anthropic": {"api_key": fakes.ANTHROPIC}}}
    write(home / ".local" / "share" / "crush" / "crush.json", json.dumps(data))


def crush_project(root: Path) -> None:
    data = root / ".crush"
    write(data / ".gitignore", "*\n")
    sqlite(data / "crush.db", "messages", [("m1", f"key {fakes.AWS}")])
    write(data / "logs" / "crush.log", f"token {fakes.SLACK}\n")
    write(data / "crush.json", json.dumps({"providers": {"openai": {"api_key": fakes.OPENAI}}}))
    hook = {"matcher": "^bash$", "command": "curl -s https://hooks.example.invalid/x"}
    config = {"mcp": {"gh": stdio("GITHUB_TOKEN", fakes.GITHUB)}, "hooks": {"PreToolUse": [hook]}}
    write(root / "crush.json", json.dumps(config))


# --- Zed, Aider -------------------------------------------------------------------------------


def zed(home: Path) -> None:
    servers = json.dumps({"api": remote(fakes.STRIPE)})
    settings = '{\n  // Zed settings\n  "context_servers": ' + servers + ",\n}\n"
    write(home / ".config" / "zed" / "settings.json", settings)
    threads = home / "Library" / "Application Support" / "Zed" / "threads" / "threads.db"
    sqlite(threads, "threads", [("t1", f"Deploy with {fakes.GITHUB}")])


def zed_project(root: Path) -> None:
    servers = {"gh": stdio("GITHUB_TOKEN", fakes.GITHUB)}
    write(root / ".zed" / "settings.json", json.dumps({"context_servers": servers}))


def aider(home: Path) -> None:
    write(home / ".aider.conf.yml", f"model: sonnet\nopenai-api-key: {fakes.OPENAI}\n")
    write(home / ".aider" / "oauth-keys.env", f"OPENROUTER_API_KEY={fakes.GENERIC_VALUE}\n")
    write(home / ".env", f"ANTHROPIC_API_KEY={fakes.ANTHROPIC}\n")  # Aider's: it reads ~/.env
    write(home / ".aider.chat.history.md", f"#### deploy with {fakes.GITHUB}\n")


def aider_project(root: Path) -> None:
    write(root / ".aider.chat.history.md", f"#### use {fakes.SLACK}\n")
    write(root / ".aider.input.history", "+ hello\n")
    write(root / ".env", f"OPENAI_API_KEY={fakes.OPENAI}\n")
