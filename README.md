# agentprint

> Find the secrets and risky permissions your AI coding agents leave behind — on your machine and in the packages you publish. Offline. No account.

[![CI](https://github.com/adighatakdas-cloud/agentprint/actions/workflows/ci.yml/badge.svg)](https://github.com/adighatakdas-cloud/agentprint/actions)

**Status:** building — v0.1 in progress.

## Use it with your AI agent

**The security check your AI coding agent runs for you.**

```bash
uvx agentprint install   # coming soon: works once agentprint is published to PyPI
```

Run it once. After that you never type an agentprint command again: your agent (Claude Code, Codex, Cursor, Gemini CLI, Windsurf / Devin Desktop, OpenCode) checks every package publish and every `git push` by itself. If it would leak an API key or a private agent file, the agent is stopped, told exactly what to fix, and told what to ask you to do (like replacing a leaked key).

What `install` changes, for each agent it finds on your computer:

- **A hook** (where the agent supports them): before the agent runs `npm publish`, `uv publish`, `twine upload`, `git push` and friends, agentprint checks it. Everything else runs untouched. If agentprint itself breaks, the command goes ahead.
- **An MCP server entry**, so the agent can ask agentprint to check a package or repo on its own.
- **A short note** in the agent's global instructions file, telling it to run the check and never print your keys.

It shows every change as a diff before writing it, and saves a backup of each file in `~/.agentprint/backups`. Running it twice changes nothing. Preview first with `uvx agentprint install --dry-run`.

**To undo:** `uvx agentprint uninstall` puts every file back exactly as it was. Codex users: Codex runs a new hook only after you trust it — type `/hooks` in Codex once after installing.

From source today: `uv run agentprint install --dry-run`.

## What it will do

- Find API keys pasted into Claude Code, Codex, Cursor, Gemini CLI, Windsurf and OpenCode history files
- Flag over-broad agent permissions (`Bash(*)`, auto-approve modes) and hooks that call the network
- Catch `.claude/` and other agent folders before they ship inside your npm or PyPI package
- Output for humans, JSON and SARIF; run as a GitHub Action or a pre-publish hook

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
uv run agentprint pack . --format agent  # short report for an AI agent: verdict, fix steps, ask_human
```

Exit codes: `0` nothing at or above `--fail-on` (default `high`), `1` findings, `2` usage/packaging error.
Every secret is shown redacted (first 4 characters + length). `scan` is fully offline.
`pack` on a Python project runs `uv build`, which executes the project's build backend and may download it from PyPI — use it on projects you trust, or pass an already-built archive.

## Checks

| ID | What it finds | Severity |
|---|---|---|
| AP001 | API key in an agent's history, transcript, paste cache or edit backups | high |
| AP002 | API key in an agent settings / allowlist / MCP config file | critical |
| AP006 | Agent file (e.g. `.claude/settings.local.json`) would ship in your npm/PyPI package | high/medium |

## Supported agents

| Agent | Verified |
|---|---|
| Claude Code | 2026-10-08, macOS |
| Codex | 2026-10-08, macOS |

Cursor, Gemini CLI, Windsurf and OpenCode are next.

## Tech

Python 3.11+, [uv](https://docs.astral.sh/uv/), typer, rich, pytest, ruff.

## License

MIT
