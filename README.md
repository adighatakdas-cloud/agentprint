# agentprint

> Find the secrets and risky permissions your AI coding agents leave behind — on your machine and in the packages you publish. Offline. No account.

[![CI](https://github.com/adighatakdas-cloud/agentprint/actions/workflows/ci.yml/badge.svg)](https://github.com/adighatakdas-cloud/agentprint/actions)

**Status:** building — v0.1 in progress.

## What it will do

- Find API keys pasted into Claude Code, Codex, Cursor, Gemini CLI, Windsurf and OpenCode history files
- Flag over-broad agent permissions (`Bash(*)`, auto-approve modes) and hooks that call the network
- Catch `.claude/` and other agent folders before they ship inside your npm or PyPI package
- Output for humans, JSON and SARIF; run as a GitHub Action or a pre-publish hook

## Run it (from source)

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
uv sync
uv run agentprint --help
```

## Tech

Python 3.11+, [uv](https://docs.astral.sh/uv/), typer, rich, pytest, ruff.

## License

MIT
