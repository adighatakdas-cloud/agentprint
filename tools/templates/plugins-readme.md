# agentprint plugins

<!-- {{generated}} -->

agentprint {{version}} packaged the native way for each AI coding agent. Every hook here runs
`uvx --from agentprint=={{version}} agentprint guard --agent <agent>` in that agent's own hook
format: the same entry `agentprint install` writes. Use the plugin or `agentprint install` for
an agent, not both.

| Folder | For | Install |
|---|---|---|
| this folder | Claude Code | `claude plugin marketplace add {{owner}}/agentprint` |
| `codex/` | Codex | `codex plugin marketplace add {{owner}}/agentprint` |
| `copilot/` | GitHub Copilot CLI | `copilot plugin marketplace add {{owner}}/agentprint` |
| `cursor/` | Cursor | copy to `~/.cursor/plugins/local/agentprint` |
| `antigravity/` | Antigravity | `agy plugin install <this folder>/antigravity` |
| `hermes/` | Hermes Agent | copy to `~/.hermes/plugins/agentprint` |
| `gemini/` | Gemini CLI (context file; manifest at the repo root) | `gemini extensions install {{repo}}` |

Details: https://adighatakdas-cloud.github.io/agentprint/plugins/
