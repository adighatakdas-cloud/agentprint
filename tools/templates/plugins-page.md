Each plugin runs agentprint {{version}}: hooks run `uvx --from agentprint=={{version}} agentprint guard --agent <agent>`, and the MCP server is `{{mcp}}`.

### Claude Code

```bash
claude plugin marketplace add {{owner}}/agentprint
claude plugin install {{name}}@{{name}}
```

### Codex

```bash
codex plugin marketplace add {{owner}}/agentprint
```

Then open `codex`, type `/plugins` and install **agentprint**. Codex runs a plugin's hook only after you trust it: type `/hooks` and trust the agentprint hook.

### GitHub Copilot CLI

```bash
copilot plugin marketplace add {{owner}}/agentprint
copilot plugin install {{name}}@{{name}}
```

Or in one step, straight from the folder: `copilot plugin install {{owner}}/agentprint:plugins/agentprint/copilot`.

### Gemini CLI

```bash
gemini extensions install {{repo}} --ref v{{version}}
```

### Antigravity

```bash
git clone --depth 1 --branch v{{version}} {{repo}}.git agentprint-src
agy plugin install ./agentprint-src/plugins/agentprint/antigravity
```

### Cursor

```bash
git clone --depth 1 --branch v{{version}} {{repo}}.git agentprint-src
mkdir -p ~/.cursor/plugins/local
cp -R agentprint-src/plugins/agentprint/cursor ~/.cursor/plugins/local/agentprint
```

Then run **Developer: Reload Window** in Cursor.

### Hermes Agent

```bash
git clone --depth 1 --branch v{{version}} {{repo}}.git agentprint-src
mkdir -p ~/.hermes/plugins
cp -R agentprint-src/plugins/agentprint/hermes ~/.hermes/plugins/agentprint
hermes plugins enable {{name}}
```

Hermes plugins cannot bring an MCP server. To add it, put this under `mcp_servers:` in `~/.hermes/config.yaml`:

```yaml
  {{name}}:
    command: {{mcp_command}}
    args: {{mcp_args}}
    enabled: true
```

### VS Code (MCP server only)

```bash
code --add-mcp '{{vscode}}'
```

Or open this install link from a page that allows `vscode:` links (GitHub's README view does not): `{{vscode_link}}`
