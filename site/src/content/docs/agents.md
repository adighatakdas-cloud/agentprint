---
title: Agents
description: Which AI coding agents agentprint supports, which files each one reads, what install changes, and how each was verified.
---

agentprint keeps one module per agent. This page lists what a sweep (`agentprint scan`) reads for each agent, what `agentprint install` changes, and how each one was verified.

## Summary

| Agent | Scan | Install | Tested |
|---|---|---|---|
| Claude Code | yes | hook, MCP, note | live in CI |
| Codex | yes | hook, MCP, note | live in CI |
| OpenCode | yes | plugin, MCP, note | live in CI |
| Gemini CLI | yes | hook, MCP, note | live in CI |
| GitHub Copilot | yes (CLI and VS Code) | hook, MCP, note | live in CI (Copilot CLI) |
| Hermes Agent | yes | hook (after you approve it), MCP | live in CI |
| Cursor | yes | hook, MCP, note | docs-verified |
| Windsurf | yes | hook, MCP, note | docs-verified |
| Google Antigravity | yes | hook, MCP, note | docs-verified |
| Devin Desktop / CLI | not yet (its new folders) | hook, MCP, note | docs-verified |

**Scanned in full, no install yet** (14 more): Cline, Claude Desktop, Kilo Code, Zed, Goose, Aider, Continue, Qwen Code, Crush, Kiro, Factory Droid, Augment, Amp, Junie. A sweep reads their home folders and project files, and `fix` works on them, but `agentprint install` can't arm them yet. `agentprint status` lists the ones it finds as "scanned". See [the table below](#agents-scanned-in-full-no-install-yet).

**Scan-only** (4 more): Pi, OpenClaw, Warp, Roo Code. agentprint knows which of their files must never ship in a package and which personal files they write inside a project.

**Live in CI** means a nightly job runs the real agent program against a scripted mock model (no account, no real key, a throwaway home folder). It checks that a leaky `npm publish` or `git push` is stopped with the reason reaching the model, and a clean one goes ahead. **Docs-verified** means built from the agent's documentation and real folders, not driven end to end (desktop apps with no headless mode, or an account needed).

## What `scan` reads

| Agent | What is read | Verified |
|---|---|---|
| Claude Code | `~/.claude/` history, transcripts, paste cache, file backups; settings; `~/.claude.json` and `.mcp.json` MCP servers | 2026-10-08, macOS |
| Codex | `~/.codex/` history, sessions, attachments; `config.toml` (+ project), `hooks.json` | 2026-10-08, macOS |
| Cursor | `~/.cursor/` agent transcripts, `mcp.json`, `cli-config.json`, `hooks.json`; IDE chat history in `state.vscdb` (SQLite) | 2026-11-05, macOS |
| Gemini CLI | `~/.gemini/settings.json` (+ project), chats and logs in `~/.gemini/tmp/` | 2026-11-05, macOS (folders only; file names from docs) |
| Windsurf | `~/.codeium/windsurf/` `mcp_config.json`, `hooks.json`, memories; command allow list in the editor's `settings.json` | 2026-11-05, macOS (folders only; file names from docs) |
| OpenCode | `opencode.json(c)` (global + project); session database `opencode.db` (SQLite) and storage | 2026-11-05, macOS |
| GitHub Copilot (CLI + VS Code) | `~/.copilot/` session history, logs, `settings.json`, `mcp-config.json`, `hooks/`; VS Code chat sessions, `settings.json` and `mcp.json` (the user's, and each profile's under `profiles/`; the per-OS user folder and the profile layout are docs-unverified); project `.github/copilot/`, `.github/hooks/`, `.vscode/mcp.json` | 2026-11-05, macOS |
| Hermes Agent | `~/.hermes/` `config.yaml` (approvals, hooks, MCP), `state.db` history (SQLite), sessions, memories, skills, `SOUL.md`; profiles | 2026-11-05, macOS (folder only; file names from docs) |
| Google Antigravity | `~/.gemini/antigravity*/brain/` transcripts and artifacts; `~/.gemini/config/hooks.json` and `mcp_config.json`; CLI `settings.json`; project `.agents/` | 2026-11-05, macOS |

### Agents scanned in full, no install yet

None of these is installed on the verification Mac, so every path comes from the agent's own docs or source, on the date shown. Paths the docs don't confirm are still looked at, and are listed in the last column as docs-unverified. Paths are the same under `%USERPROFILE%` on Windows unless the table names a Windows folder (`AppData`). Every MCP config is read by one shared reader, so [AP005](/agentprint/checks/ap005/) reports an inline key in any of them.

<!-- agents:start -->
| Agent | What `scan` reads | Key stores (only who can read them is checked) | What `fix` does | Paths from | Docs-unverified (probed) |
|---|---|---|---|---|---|
| Cline | `~/.cline/data/` sessions, `db/sessions.db` (SQLite), logs, legacy tasks; `settings/cline_mcp_settings.json` (MCP), `global-settings.json`, `globalState.json`; the extension's tasks in VS Code `globalStorage/saoudrizwan.claude-dev/` | `secrets.json` (plain text), `settings/providers.json` | masks keys in sessions, logs and task files; MCP keys: needs you (no env reference) | docs and source, 2026-11-05 | the extension folder on each OS (VS Code's usual layout); `~/.cline` on Windows |
| Claude Desktop | `claude_desktop_config.json` (MCP servers) in `~/Library/Application Support/Claude/`, `%APPDATA%\Claude\` or `~/.config/Claude/`; app logs, including `mcp*.log` | none | masks keys in logs; MCP keys: needs you (no env reference) | MCP docs (modelcontextprotocol.io), 2026-11-07 | none |
| Kilo Code | session database `~/.local/share/kilo/kilo.db` (SQLite); `~/.config/kilo/kilo.json(c)` and project `kilo.json(c)`, `.kilo/kilo.json(c)` (MCP); the old extension's MCP file | `~/.local/share/kilo/auth.json` | MCP `environment` keys become `"{env:NAME}"` (headers: needs you); keys in the database: needs you (agentprint never edits a database) | docs, 2026-11-05 | `auth.json` (inferred from `kilo.db`); the config folder on Windows; the old extension folder on each OS |
| Zed | `threads/threads.db` (SQLite; titles only, thread bodies are compressed) in `~/Library/Application Support/Zed`, `~/.local/share/zed` or `%LOCALAPPDATA%\Zed`; `settings.json` in `~/.config/zed` or `%APPDATA%\Zed`, and project `.zed/settings.json` (MCP `context_servers`) | none | keys in the database and MCP keys: needs you | docs and source, 2026-11-05 and 2026-11-07 | `threads.db` on Linux and Windows, `settings.json` on Windows (Zed's source only) |
| Goose | sessions (`sessions.db`, SQLite, and older JSONL), logs including raw LLM requests, `history.txt`; `config.yaml` (MCP extensions); hooks in `~/.agents/plugins/*/hooks/`; on Windows the same under `%APPDATA%\Block\goose\` | `secrets.yaml` | masks keys in logs, old sessions and history; MCP keys: needs you (use `env_keys`) | docs, 2026-11-05 | none |
| Aider | transcripts `.aider.chat.history.md`, `.aider.input.history`, `.aider.llm.history` (in a project or the home folder); `.aider.conf.yml` (home and project) | `~/.aider/oauth-keys.env`; `.env` next to Aider's files (home and project) | masks keys in transcripts; keys in `.aider.conf.yml`: needs you | docs and source, 2026-11-05 | none |
| Continue | `~/.continue/` sessions, `logs/prompt.log` (full prompts), `dev_data/`; `config.yaml`, `config.json` and `mcpServers/` (MCP, raw `apiKey`); project `.continue/mcpServers/` | `~/.continue/.env`, `auth.json`; project `.continue/.env` | masks keys in sessions and logs; MCP keys: needs you (use `${{ secrets.NAME }}`) | docs and source, 2026-11-05 | none |
| Qwen Code | `~/.qwen/` chats (JSONL), shell history, edit backups, auto memory; `settings.json` (MCP, hooks, `env` block) and project `.qwen/settings.json` | `oauth_creds.json`, `mcp-oauth-tokens.json`, `.env`; project `.qwen/.env` | masks keys in chats and history; MCP `env` keys become `"${NAME}"` | docs and source, 2026-11-05 | none |
| Crush | project `.crush/crush.db` (SQLite) and `.crush/logs/`; `~/.config/crush/crushrc` and `crush.json`, project `crushrc` and `crush.json` (MCP, hooks) | `~/.local/share/crush/crush.json` (pasted keys; `%LOCALAPPDATA%\crush\` on Windows); project `.crush/crush.json` | masks keys in logs; MCP `env` keys in `crush.json` become `"$NAME"` (headers: needs you); keys in the database and in `crushrc`: needs you | README, config docs and source, 2026-11-05 | none |
| Kiro | `~/.kiro/settings/mcp.json` (MCP), `cli.json`, CLI hooks; project `.kiro/settings/mcp.json` and `.kiro/hooks/` | none | MCP keys: needs you (approve an env variable in Kiro) | docs, 2026-11-05 | none |
| Factory Droid | `~/.factory/settings.json`, `settings.local.json` (model keys), `mcp.json` (MCP), `hooks.json`, and the same under a project's `.factory/`; sessions (JSONL) | none | masks keys in sessions; MCP `env` keys in `mcp.json` become `"${NAME}"` (headers: needs you); keys in settings: needs you | docs, 2026-11-05 | `~/.factory/sessions/` (from Factory's plugin repo, not the docs) |
| Augment (Auggie) | `~/.augment/settings.json` and project `.augment/settings*.json` (MCP, hooks) | `~/.augment/session.json` | MCP `env` keys become `"${NAME}"` (headers: needs you) | docs, 2026-11-05 and 2026-11-07 | `session.json` (not named on the current CLI reference page) |
| Amp | `~/.config/amp/settings.json(c)` and project `.amp/settings.json(c)` (`amp.mcpServers`) | `~/.local/share/amp/secrets.json` | MCP `env` keys become `"${NAME}"` (headers: needs you) | docs, 2026-11-05 | `~/.local/share/amp/secrets.json` (third-party report) |
| Junie | `~/.junie/sessions/` (transcripts, events); `~/.junie/mcp/mcp.json` (MCP), `config.json` (hooks), and the same under a project's `.junie/` | none | masks keys in sessions; MCP keys: needs you | docs, 2026-11-05 and 2026-11-07 | `sessions/` and `config.json` (in the 2026-11-05 docs; not found again 2026-11-07) |
<!-- agents:end -->

Their instruction and rules files (`AGENTS.md`, `.clinerules/`, `.goosehints`, `.kiro/steering/`, and others) are never treated as leaks. A project's `.env` counts as Aider's only next to Aider's own files.

**Scan-only agents** (4): Pi, OpenClaw, Warp, Roo Code. The rules are in [`agentprint/agents/scan_only.py`](https://github.com/adighatakdas-cloud/agentprint/blob/main/agentprint/agents/scan_only.py).

Credential stores where a key is expected (`~/.codex/auth.json`, `~/.gemini/oauth_creds.json` and `.env`, OpenCode `auth.json`, `~/.hermes/.env` and `auth.json`, `~/.copilot/providers.json` and `config.json`, Antigravity `mcp_oauth_tokens.json`) are never read. [AP008](/agentprint/checks/ap008/) only checks who can read them. SQLite chat databases are opened read-only, and at most 64 MB of text is read from each. Hermes `config.yaml` is read with a safe YAML loader.

**Not covered yet:** Windsurf Cascade conversations and older Antigravity conversations (binary `.pb` files); Devin Desktop's new paths (`.devin/`, `~/.config/devin/`); Cursor's IDE auto-run mode (stored inside `state.vscdb`); OpenCode plugins (JavaScript); Hermes outbound webhooks. For the agents scanned with no install yet: Zed thread bodies (compressed); Cline hook scripts and Kilo, Amp and Goose plugins (code, not config); the Kiro CLI session database, Kiro IDE chats, Augment sessions and Factory and Junie logins (no path in the docs); permission settings of these agents ([AP003](/agentprint/checks/ap003/)); and the variables that move their folders (`QWEN_HOME`, `GOOSE_PATH_ROOT`, `CONTINUE_GLOBAL_DIR`, `KILO_DB`, `KIRO_HOME`, `JUNIE_HOME` and others).

## What AP003 flags in each agent

See the [AP003 page](/agentprint/checks/ap003/).

## What `install` changes

| Agent | Hook (blocks a leaky publish/push) | MCP server | Instructions note |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | `~/.claude.json` | `~/.claude/CLAUDE.md` |
| Codex | `~/.codex/hooks.json` (trust it once in `/hooks`) | `~/.codex/config.toml` | `~/.codex/AGENTS.md` |
| Cursor | `~/.cursor/hooks.json` | `~/.cursor/mcp.json` | `~/.cursor/rules/agentprint.mdc` |
| Gemini CLI | `~/.gemini/settings.json` | `~/.gemini/settings.json` | `~/.gemini/GEMINI.md` |
| Windsurf (Cascade) | `~/.codeium/windsurf/hooks.json` | `~/.codeium/windsurf/mcp_config.json` | `memories/global_rules.md` |
| Devin Desktop / CLI | `~/.config/devin/config.json` | `~/.config/devin/mcp_config.json` | `~/.devin/rules/agentprint.md` |
| OpenCode | plugin `~/.config/opencode/plugins/agentprint.js` | `opencode.json` | `AGENTS.md` (only if it won't hide your `~/.claude/CLAUDE.md`) |
| GitHub Copilot | `~/.copilot/hooks/agentprint.json` (checked against the Copilot CLI docs; VS Code uses it only where it runs the same hooks as the CLI) | `~/.copilot/mcp-config.json` | `~/.copilot/copilot-instructions.md` |
| Hermes Agent | `~/.hermes/config.yaml` (**off until you approve it** in `hermes chat`) | `~/.hermes/config.yaml` | none: its only global file is the agent's persona, `SOUL.md` |
| Antigravity | `~/.gemini/config/hooks.json` | `~/.gemini/config/mcp_config.json` | `~/.gemini/config/rules/agentprint.md` |

Every hook runs one small launcher script, `~/.agentprint/bin/agentprint-hook`, which starts `agentprint guard --agent <name>` and answers in that agent's own format. If the launcher or agentprint is missing or fails, the hook answers "allow".

**Hermes `config.yaml`:** agentprint only edits it if Hermes has already created it, and keeps your own lines, comments, indentation and line endings. Its entries go first in each section. If the file is missing, can't be read safely, or the section uses YAML anchors (`&`, `*`, `<<`), agentprint changes nothing and prints what to add by hand.

**Codex `config.toml`:** agentprint's MCP entry sits in a marked block at the top of the file. If Codex ever writes its own settings inside that block, uninstall keeps those lines and removes only agentprint's.

**Cursor and Claude Code together:** Cursor also runs Claude Code's hooks, so with both installed the check can run twice. That is harmless.

## Live tests

The live tests ([`tests/live/`](https://github.com/adighatakdas-cloud/agentprint/tree/main/tests/live), run nightly and on pull requests that touch the hooks) start the real agent program with a scripted mock model: no account, no real key, a throwaway home folder. The mock model asks the agent to run `npm publish` or `git push` (fake versions that only leave a marker file) after `agentprint install`. Each test checks that a package or repo with a leaked key is **stopped and the model is told why**, that a clean one **goes ahead**, and that `agentprint uninstall` takes agentprint back out.

| Agent | Version tested | Notes |
|---|---|---|
| Claude Code | 2.1.318 | |
| Codex | 0.166.0 | run with `--dangerously-bypass-hook-trust`, the documented way to run a hook unattended; you trust it once in `/hooks` |
| OpenCode | 1.19.5 | through agentprint's OpenCode plugin |
| Gemini CLI | 0.67.0 | |
| GitHub Copilot CLI | 1.0.105 | with a model key of your own (BYOK), no GitHub login |
| Hermes Agent | 0.19.0 | run with `--accept-hooks`, the documented way to approve a hook unattended; you approve it once in `hermes chat` |
| Cursor, Windsurf, Antigravity | – | docs-verified only: desktop apps with no headless mode to drive in a test |
| Devin Desktop / Devin CLI | – | docs-verified only: needs a Devin account to run |
