# agentprint

> Find the secrets and risky permissions your AI coding agents leave behind — on your machine and in the packages you publish. Offline. No account.

[![CI](https://github.com/adighatakdas-cloud/agentprint/actions/workflows/ci.yml/badge.svg)](https://github.com/adighatakdas-cloud/agentprint/actions)

**Status:** building — v0.1 in progress.

## Use it with your AI agent

**The security check your AI coding agent runs for you.**

```bash
uvx agentprint install   # coming soon: works once agentprint is published to PyPI
```

Run it once. After that your agent checks the package publishes and `git push` commands it recognises before they run. If one would leak an API key or a private agent file, the agent is stopped, told exactly what to fix, and told what to ask you to do (like replacing a leaked key).

| Agent | Hook (blocks a leaky publish/push) | MCP server | Instructions note |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | `~/.claude.json` | `~/.claude/CLAUDE.md` |
| Codex | `~/.codex/hooks.json` (trust it once in `/hooks`) | `~/.codex/config.toml` | `~/.codex/AGENTS.md` |
| Cursor | `~/.cursor/hooks.json` | `~/.cursor/mcp.json` | `~/.cursor/rules/agentprint.mdc` |
| Gemini CLI | `~/.gemini/settings.json` | `~/.gemini/settings.json` | `~/.gemini/GEMINI.md` |
| Windsurf (Cascade) | `~/.codeium/windsurf/hooks.json` | `~/.codeium/windsurf/mcp_config.json` | `memories/global_rules.md` |
| Devin Desktop / CLI | `~/.config/devin/config.json` | `~/.config/devin/mcp_config.json` | `~/.devin/rules/agentprint.md` |
| OpenCode | plugin `~/.config/opencode/plugins/agentprint.js` | `opencode.json` | `AGENTS.md` (only if it won't hide your `~/.claude/CLAUDE.md`) |
| GitHub Copilot | `~/.copilot/hooks/agentprint.json` (checked against the Copilot CLI docs; VS Code uses it only where it runs the same hooks as the CLI, and its shell tool name there is unverified) | `~/.copilot/mcp-config.json` | `~/.copilot/copilot-instructions.md` |
| Hermes Agent | `~/.hermes/config.yaml` (**off until you approve it** in `hermes chat`) | `~/.hermes/config.yaml` | none: its only global file is the agent's persona, `SOUL.md` |
| Antigravity | `~/.gemini/config/hooks.json` | `~/.gemini/config/mcp_config.json` | `~/.gemini/config/rules/agentprint.md` |

What `install` changes, for each agent it finds on your computer:

- **A hook** (where the agent supports them): before the agent runs `npm publish`, `pnpm -r publish`, `npx changeset publish`, `uv publish`, `twine upload`, `git push` and similar, agentprint checks what would be uploaded. Everything else runs untouched. If agentprint itself breaks, the command goes ahead.
- **An MCP server entry**, so the agent can ask agentprint to check a package or repo on its own.
- **A short note** in the agent's global instructions file, telling it to run the check and never print your keys.

Before writing, it prints the lines it adds to each file (never your other settings), and it saves a backup of each file in `~/.agentprint/backups`. Running it twice changes nothing. Preview first with `uvx agentprint install --dry-run`.

**To undo:** `uvx agentprint uninstall` takes out only what agentprint added and leaves your own changes alone. A file you haven't touched since comes back byte for byte. `uninstall --agent codex` undoes just one agent. Codex users: Codex runs a new hook only after you trust it — type `/hooks` in Codex once after installing.

From source today: `uv run agentprint install --dry-run`.

**What it can't promise:**

- **The hooks need macOS or Linux.** On Windows, only the MCP server and the instructions note work for now.
- **Spotting a publish is best effort.** agentprint reads the command line the agent is about to run; it isn't a shell. It follows `cd`, `&&`, `bash -c`, `npm run release` (by reading `package.json`), workspaces and release tools like changesets and lerna. A publish hidden in a Makefile, a shell script file or a heredoc can slip past the hook; the instructions note still tells the agent to check first.
- **"Could not check" is not "OK".** When agentprint can't tell what would be uploaded (a folder held in a variable, a `pnpm --filter` selection, nothing built in `dist/` yet, git not installed), it lets the command run and says "agentprint could not check this…" so you and the agent know.
- **Your agents' own approvals still apply.** agentprint only adds a "deny" when it finds a problem; it never approves commands for you. If you use Claude Code too, Cursor also runs Claude Code's hooks, so the check can run twice.
- **Hermes asks before it runs any new hook.** agentprint never approves its own hook: start `hermes chat` once and accept it. Until then (and in gateway or cron runs that never ask) the Hermes hook is off; the MCP server still works. agentprint only edits `config.yaml` if Hermes has already created it, and leaves your own lines, comments, indentation and line endings as they were; its entries go first in each section. If the file is missing, can't be read safely, or the section uses YAML anchors (`&`, `*`, `<<`), it changes nothing and prints what to add by hand.
- **Codex's `config.toml`:** agentprint's MCP entry sits in a marked block at the top of the file. If Codex ever writes its own settings inside that block, uninstall keeps those lines and removes only agentprint's.

### Tested inside real agents

The live tests (`tests/live/`, run nightly and on pull requests that touch the hooks) start the real agent program with a pretend model on your own computer: no account, no real key, a throwaway home folder. The pretend model asks the agent to run `npm publish` or `git push` (fake versions that only leave a marker file), after `agentprint install`. Each test checks that a package or repo with a leaked key is **stopped and the model is told why**, that a clean one **goes ahead**, and that `agentprint uninstall` takes agentprint back out.

| Agent | Version tested | What the live test proves |
|---|---|---|
| Claude Code | 2.1.318 | hook blocks leaky publish/push, reason reaches the model; clean ones run |
| Codex | 0.166.0 | same (run with `--dangerously-bypass-hook-trust`, the documented way to run a hook unattended; you trust it once in `/hooks`) |
| OpenCode | 1.19.5 | same, through agentprint's OpenCode plugin |
| Gemini CLI | 0.67.0 | same |
| GitHub Copilot CLI | 1.0.105 | same, with your own model key (BYOK), no GitHub login |
| Hermes Agent | 0.19.0 | same (run with `--accept-hooks`, the documented way to approve a hook unattended; you approve it once in `hermes chat`) |
| Cursor, Windsurf, Antigravity | – | docs-verified only: desktop apps with no headless mode to drive in a test |
| Devin Desktop / Devin CLI | – | docs-verified only: needs a Devin account to run |

## What it will do

- Find API keys pasted into Claude Code, Codex, Cursor, Gemini CLI, Windsurf, OpenCode, GitHub Copilot, Hermes Agent and Google Antigravity history files
- Flag over-broad agent permissions (`Bash(*)`, auto-approve modes), hooks that call the network, and agent files other users on the machine can read
- Catch `.claude/` and other agent folders (24 agents) before they ship inside your npm or PyPI package
- Output for humans, JSON and SARIF; run as a GitHub Action, a pre-publish script or a pre-commit hook

## Run it (from source)

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
uv sync
uv run agentprint scan          # your machine + current folder
```

More:

```bash
uv run agentprint scan --no-home .     # just this repo
uv run agentprint pack .               # what would ship in your npm/PyPI package
uv run agentprint pack my-pkg-1.0.tgz  # an already-built package (.tgz, .tar.gz, .whl, .zip)
uv run agentprint scan --format json   # machine-readable output
uv run agentprint pack . --format sarif > agentprint.sarif   # for GitHub code scanning
uv run agentprint pack . --format agent  # short report for an AI agent: verdict, fix steps, ask_human
```

Exit codes: `0` nothing at or above `--fail-on` (default `high`), `1` findings, `2` usage/packaging error.
Every secret is shown redacted (first 4 characters + length). `scan` is fully offline.
`pack` on a Python project runs `uv build`, which executes the project's build backend and may download it from PyPI — use it on projects you trust, or pass an already-built archive.

## Install and use in CI

> Available after the first release. Until then, run it from source as above.

```bash
uvx agentprint scan     # with uv (Python)
npx agentprint-cli pack # with npm; runs the same tool through uvx or pipx
```

**Stop a leaky `npm publish`.** Add to your `package.json`:

```json
"scripts": {
  "prepublishOnly": "npx agentprint-cli pack"
}
```

**GitHub Action.** Add `.github/workflows/agentprint.yml`:

```yaml
name: agentprint
on: [push, pull_request]
permissions:
  contents: read
  security-events: write  # only needed for upload-sarif
jobs:
  agentprint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: adighatakdas-cloud/agentprint@v0.1.0  # better: pin the release's commit SHA
        with:
          command: pack        # or scan
          upload-sarif: true   # show results in the repo's Security tab
```

| Input | Default | Meaning |
|---|---|---|
| `command` | `pack` | `pack`: what your npm/PyPI package would publish. `scan`: agent settings files in the repo. |
| `path` | `.` | Project directory, or a built `.tgz`/`.whl` for `pack`. |
| `fail-on` | `high` | Fail on findings at or above `low`, `medium`, `high`, `critical`; `none` only reports. |
| `upload-sarif` | `false` | Upload results to code scanning. Needs `security-events: write`; private repos also need GitHub Code Security. |

The step fails with agentprint's own exit code (`1` findings, `2` error), after the SARIF upload.
Outputs: `exit-code` and `sarif-file`.

**pre-commit.** Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/adighatakdas-cloud/agentprint
    rev: v0.1.0
    hooks:
      - id: agentprint-pack   # agent files or keys in your package
      - id: agentprint-scan   # keys in this repo's agent settings
```

## Checks

| ID | What it finds | Severity |
|---|---|---|
| AP001 | API key in an agent's history, transcript, paste cache, edit backups or chat database | high |
| AP002 | API key in an agent settings / allowlist file | critical |
| AP003 | Over-broad permission: `Bash(*)`, `bypassPermissions`, Codex `never` + `danger-full-access`, Cursor `Shell(*)` / `approvalMode: unrestricted`, Gemini bare `run_shell_command` / trusted MCP server, Windsurf allow-listed shell, OpenCode `"allow"`, Copilot `allow-all` / VS Code `chat.tools.global.autoApprove`, Hermes `approvals.mode: off` / `hooks_auto_accept`, Antigravity `always-proceed` / `command(*)` | medium |
| AP004 | Agent hook (or Codex `notify`) runs a network command: curl, wget, nc, `\| sh`, Invoke-WebRequest, a URL | medium |
| AP005 | API key written inline in an MCP server entry (env, headers, args, URL); replaces AP002 for that key | critical |
| AP006 | Agent file (e.g. `.claude/settings.local.json`, `.aider.chat.history.md`, `.hermes/`) would ship in your npm/PyPI package | high/medium/low |
| AP007 | Personal agent file (`.claude/settings.local.json`, `CLAUDE.local.md`, `.gemini/.env`, `.github/copilot/settings.local.json`, Aider transcripts) in a git repo but not gitignored, or already committed | low |
| AP008 | Agent history, settings or credential store readable by other users on this machine (file mode and every folder above it); fix is an exact `chmod 600`. POSIX only | medium; high for key stores or files holding a key |

Every finding carries a fix written for the AI agent that reads it: which file, which setting to change, which line to add to `.gitignore`, and what to ask the human (rotating a key).

## Supported agents

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

**Scan-only** (package rules for `agentprint pack`, plus the files they write inside a project): Aider, Cline, Kilo Code, Qwen Code, Crush, Goose, Kiro, Factory Droid, Continue, Zed, Pi, OpenClaw, Warp, Roo Code, Augment. Their instruction and rules files (`AGENTS.md`, `.clinerules/`, `.goosehints`, …) are never treated as leaks.

Credential stores where a key is expected (`~/.codex/auth.json`, `~/.gemini/oauth_creds.json` and `.env`, OpenCode `auth.json`, `~/.hermes/.env` and `auth.json`, `~/.copilot/providers.json` and `config.json`, Antigravity `mcp_oauth_tokens.json`) are never read; AP008 only checks who can read them.
SQLite chat databases are opened read-only, and at most 64 MB of text is read from each. Hermes `config.yaml` is read with a safe YAML loader.

Not covered yet: Windsurf Cascade conversations and older Antigravity conversations (binary `.pb` files) and Windsurf's new Devin Desktop paths; Cursor's IDE auto-run mode (stored inside `state.vscdb`); OpenCode plugins (JavaScript); Hermes outbound webhooks.

## Help scan npm and PyPI

We measure how often agent files (and keys inside them) ship in newly published packages,
and tell maintainers privately first. Volunteers each scan a small shard: see
[docs/VOLUNTEER_SCAN.md](docs/VOLUNTEER_SCAN.md).

## Tech

Python 3.11+, [uv](https://docs.astral.sh/uv/), typer, rich, pytest, ruff.

## Security

Found a security problem in agentprint? See [SECURITY.md](SECURITY.md). agentprint never sends
your data anywhere.

## License

MIT
