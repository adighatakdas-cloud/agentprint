# agentprint

> Find the secrets and risky permissions your AI coding agents leave behind — on your machine and in the packages you publish. Offline. No account.

**Status:** planning — not yet usable. See [docs/PLAN.md](docs/PLAN.md).

## What it will do

- Find API keys pasted into Claude Code, Codex, Cursor, Gemini CLI, Copilot, Cline and Aider history files
- Flag over-broad agent permissions (`Bash(*)`, auto-approve modes) and hooks that call the network
- Catch `.claude/` and other agent folders before they ship inside your npm or PyPI package
- Redact what it finds (`agentprint fix`), output SARIF for CI, run as a pre-publish hook

## Run it (planned)

```bash
uvx agentprint scan
```

## License

TBD (MIT planned)
