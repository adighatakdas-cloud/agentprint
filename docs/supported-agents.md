# Supported agents: the details

The README has the summary. This page lists, for each agent, which files `agentprint scan`
reads, what `agentprint install` changes, and how each one was verified.

## What `scan` reads

| Agent | What is read | Verified |
|---|---|---|
| Claude Code | `~/.claude/` history, transcripts, paste cache, file backups; settings; `~/.claude.json` and `.mcp.json` MCP servers | 2026-10-08, macOS |
| Codex | `~/.codex/` history, sessions, attachments; `config.toml` (+ project), `hooks.json` | 2026-10-08, macOS |
| Cursor | `~/.cursor/` agent transcripts, `mcp.json`, `cli-config.json`, `hooks.json`; IDE chat history in `state.vscdb` (SQLite) | 2026-11-05, macOS |
| Gemini CLI | `~/.gemini/settings.json` (+ project), chats and logs in `~/.gemini/tmp/` | 2026-11-05, macOS (folders only; file names from docs) |
| Windsurf | `~/.codeium/windsurf/` `mcp_config.json`, `hooks.json`, memories; command allow list in the editor's `settings.json` | 2026-11-05, macOS (folders only; file names from docs) |
| OpenCode | `opencode.json(c)` (global + project); session database `opencode.db` (SQLite) and storage | 2026-11-05, macOS |
| GitHub Copilot (CLI + VS Code) | `~/.copilot/` session history, logs, `settings.json`, `mcp-config.json`, `hooks/`; VS Code chat sessions and `settings.json`; project `.github/copilot/`, `.github/hooks/`, `.vscode/mcp.json` | 2026-11-05, macOS |
| Hermes Agent | `~/.hermes/` `config.yaml` (approvals, hooks, MCP), `state.db` history (SQLite), sessions, memories, skills, `SOUL.md`; profiles | 2026-11-05, macOS (folder only; file names from docs) |
| Google Antigravity | `~/.gemini/antigravity*/brain/` transcripts and artifacts; `~/.gemini/config/hooks.json` and `mcp_config.json`; CLI `settings.json`; project `.agents/` | 2026-11-05, macOS |

**Scan-only agents** (15): Aider, Cline, Kilo Code, Qwen Code, Crush, Goose, Kiro, Factory
Droid, Continue, Zed, Pi, OpenClaw, Warp, Roo Code, Augment. agentprint knows which of their
files must never ship in a package (`agentprint pack`) and which personal files they write
inside a project. Their instruction and rules files (`AGENTS.md`, `.clinerules/`,
`.goosehints`, ...) are never treated as leaks. The rules are in
[`agentprint/agents/scan_only.py`](../agentprint/agents/scan_only.py).

Credential stores where a key is expected (`~/.codex/auth.json`, `~/.gemini/oauth_creds.json`
and `.env`, OpenCode `auth.json`, `~/.hermes/.env` and `auth.json`, `~/.copilot/providers.json`
and `config.json`, Antigravity `mcp_oauth_tokens.json`) are never read; AP008 only checks who
can read them. SQLite chat databases are opened read-only, and at most 64 MB of text is read
from each. Hermes `config.yaml` is read with a safe YAML loader.

**Not covered yet:** Windsurf Cascade conversations and older Antigravity conversations (binary
`.pb` files); Devin Desktop's new paths (`.devin/`, `~/.config/devin/`); Cursor's IDE auto-run
mode (stored inside `state.vscdb`); OpenCode plugins (JavaScript); Hermes outbound webhooks.

## What AP003 flags in each agent

See the [AP003 page](https://adighatakdas-cloud.github.io/agentprint/checks/ap003/).

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

Every hook runs one small launcher script, `~/.agentprint/bin/agentprint-hook`, which starts
`agentprint guard --agent <name>` and answers in that agent's own format. If the launcher or
agentprint is missing or fails, the hook answers "allow".

**Hermes `config.yaml`:** agentprint only edits it if Hermes has already created it, and keeps
your own lines, comments, indentation and line endings; its entries go first in each section.
If the file is missing, can't be read safely, or the section uses YAML anchors (`&`, `*`,
`<<`), it changes nothing and prints what to add by hand.

**Codex `config.toml`:** agentprint's MCP entry sits in a marked block at the top of the file.
If Codex ever writes its own settings inside that block, uninstall keeps those lines and
removes only agentprint's.

**Cursor and Claude Code together:** Cursor also runs Claude Code's hooks, so with both
installed the check can run twice. That is harmless.

## Live tests

The live tests ([`tests/live/`](../tests/live/), run nightly and on pull requests that touch
the hooks) start the real agent program with a scripted mock model: no account, no real key, a
throwaway home folder. The mock model asks the agent to run `npm publish` or `git push` (fake
versions that only leave a marker file) after `agentprint install`. Each test checks that a
package or repo with a leaked key is **stopped and the model is told why**, that a clean one
**goes ahead**, and that `agentprint uninstall` takes agentprint back out.

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
