---
title: Plugins and extensions
description: Add agentprint to an AI coding agent as its own plugin or extension, what each one includes, how far each is verified, and how to remove it.
---

`agentprint install` edits each agent's config files for you. If you would rather add agentprint the way you add any other plugin, each agent below has one. Both ways give the agent the same three things: a hook that checks a package publish or `git push` before it runs, the agentprint MCP server, and the short note on when to check.

:::caution[Use the plugin or `agentprint install`, not both]
For each agent, pick one. With both, the same check runs twice and the agent sees two agentprint MCP servers. `agentprint status` says when it finds both for one agent ("armed twice") and how to take one out. To keep the plugin, run `agentprint uninstall --agent <name>`.
:::

## What each plugin includes

| Agent | Hook | MCP server | Note | Formats |
|---|---|---|---|---|
| Claude Code | yes | yes | as a skill | as documented |
| Cursor | yes | yes | always-on rule | as documented |
| Codex | yes | yes | as a skill | docs-verified only |
| GitHub Copilot CLI | yes | yes | as a skill | docs-verified only |
| Gemini CLI | yes | yes | context file | docs-verified only |
| Google Antigravity | yes | yes | always-on rule | docs-verified only |
| Hermes Agent | yes (after you enable it) | no: add it to `config.yaml` yourself | as a skill | docs-verified only |
| VS Code | no | yes | no | docs-verified only (the link) |

**As documented** means every file uses a shape the agent's official docs show. **Docs-verified only** means the plugin is built from the docs, but a part of its format is not shown there, so agentprint uses the closest documented shape:

- **Codex:** the docs show plugin MCP servers only for remote servers, not local (stdio) ones, and say bundled hooks run in the desktop app; whether the CLI runs them is not stated.
- **GitHub Copilot CLI:** the docs name the plugin's hooks file but not its contents, so it uses the Copilot CLI hooks format.
- **Gemini CLI:** the docs imply but don't show the outer `"hooks"` key in the extension's hooks file, and don't say whether the context file may sit in a subfolder.
- **Google Antigravity:** the docs name only the manifest. The hook, MCP and rule files copy the layout of `~/.gemini/config/`, which `agentprint install` writes.
- **Hermes Agent:** the docs don't list the terminal tool's arguments or say what path `register_skill` takes.
- **VS Code:** the docs show `code --add-mcp` with a server name in the JSON; the install link uses the same JSON.

agentprint's tests check every plugin file and run every plugin hook command against a fake package. None of these plugins has been loaded into its real agent yet. Nothing is listed in any marketplace or registry yet: you install from this repository.

## Install

<!-- plugins:start -->
Each plugin runs agentprint 0.1.1: hooks run `uvx --from agentprint==0.1.1 agentprint guard --agent <agent>`, and the MCP server is `uvx --from agentprint==0.1.1 agentprint mcp`.

### Claude Code

```bash
claude plugin marketplace add adighatakdas-cloud/agentprint
claude plugin install agentprint@agentprint
```

### Codex

```bash
codex plugin marketplace add adighatakdas-cloud/agentprint
```

Then open `codex`, type `/plugins` and install **agentprint**. Codex runs a plugin's hook only after you trust it: type `/hooks` and trust the agentprint hook.

### GitHub Copilot CLI

```bash
copilot plugin marketplace add adighatakdas-cloud/agentprint
copilot plugin install agentprint@agentprint
```

Or in one step, straight from the folder: `copilot plugin install adighatakdas-cloud/agentprint:plugins/agentprint/copilot`.

### Gemini CLI

```bash
gemini extensions install https://github.com/adighatakdas-cloud/agentprint --ref v0.1.1
```

### Antigravity

```bash
git clone --depth 1 --branch v0.1.1 https://github.com/adighatakdas-cloud/agentprint.git agentprint-src
agy plugin install ./agentprint-src/plugins/agentprint/antigravity
```

### Cursor

```bash
git clone --depth 1 --branch v0.1.1 https://github.com/adighatakdas-cloud/agentprint.git agentprint-src
mkdir -p ~/.cursor/plugins/local
cp -R agentprint-src/plugins/agentprint/cursor ~/.cursor/plugins/local/agentprint
```

Then run **Developer: Reload Window** in Cursor.

### Hermes Agent

```bash
git clone --depth 1 --branch v0.1.1 https://github.com/adighatakdas-cloud/agentprint.git agentprint-src
mkdir -p ~/.hermes/plugins
cp -R agentprint-src/plugins/agentprint/hermes ~/.hermes/plugins/agentprint
hermes plugins enable agentprint
```

Hermes plugins cannot bring an MCP server. To add it, put this under `mcp_servers:` in `~/.hermes/config.yaml`:

```yaml
  agentprint:
    command: uvx
    args: ["--from", "agentprint==0.1.1", "agentprint", "mcp"]
    enabled: true
```

### VS Code (MCP server only)

```bash
code --add-mcp '{"name":"agentprint","type":"stdio","command":"uvx","args":["--from","agentprint==0.1.1","agentprint","mcp"]}'
```

Or open this install link from a page that allows `vscode:` links (GitHub's README view does not): `vscode:mcp/install?%7B%22name%22%3A%22agentprint%22%2C%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22--from%22%2C%22agentprint%3D%3D0.1.1%22%2C%22agentprint%22%2C%22mcp%22%5D%7D`
<!-- plugins:end -->

Antigravity can also import an installed Gemini CLI extension with `agy plugin import gemini`. Its docs don't say which parts carry over, and the Gemini hook watches Gemini's shell tool (`run_shell_command`), not Antigravity's (`run_command`). To get the hook in Antigravity, install the Antigravity plugin above.

## How the plugin hook behaves

- It is the same hook `agentprint install` writes for that agent, in the agent's own format. Only package publishes and `git push` are checked; every other command runs untouched.
- It needs [uv](https://docs.astral.sh/uv/) (`uvx`) on the agent's `PATH`, or in `~/.local/bin` or `~/.cargo/bin`. The first run downloads that exact agentprint version into uv's cache. For hooks that start faster from a permanent install, use `uv tool install agentprint` and `agentprint install` instead.
- If `uvx` is missing, or agentprint cannot start, the command runs and the agent's hook log gets one line, such as `agentprint: uvx not found, so this command was not checked (install uv to turn it on)`. The hook never blocks because agentprint itself failed.
- Strict mode (`AGENTPRINT_STRICT=1`, see [strict mode](/agentprint/inside-your-agent/#strict-mode-block-when-it-cannot-check)) reaches agentprint where the agent passes its environment on to hooks. Gemini CLI does not. As with `install`, strict mode cannot block when agentprint never starts.
- **Plugin hooks run on macOS and Linux only for now** (they use `/bin/sh`). Formats that can name a Windows command (Claude Code's `shell`, Copilot's `powershell`, Codex's `commandWindows`) don't get one yet. On Windows, use `agentprint install`, which adds the MCP server and the note.
- Each agent asks you first in its own way: Claude Code shows the plugin's details before it installs, Gemini CLI asks for consent when you install the extension, Codex runs the hook only after you trust it in `/hooks`, and Hermes loads the plugin only once you enable it.

## Remove it

| Agent | Remove the plugin |
|---|---|
| Claude Code | `claude plugin uninstall agentprint@agentprint` |
| Codex | uninstall agentprint in Codex's `/plugins` menu |
| GitHub Copilot CLI | `copilot plugin uninstall agentprint` |
| Gemini CLI | `gemini extensions uninstall agentprint` |
| Google Antigravity | `agy plugin uninstall agentprint` |
| Cursor | delete `~/.cursor/plugins/local/agentprint`, then reload the Cursor window |
| Hermes Agent | `hermes plugins disable agentprint`, then delete `~/.hermes/plugins/agentprint` |
| VS Code | remove the agentprint server from the MCP server list |

`agentprint status` finds a plugin by the folder the agent installs it into, so it counts a plugin you have disabled but not removed.

## The MCP registry

The repository has a `server.json` for the [official MCP registry](https://modelcontextprotocol.io/registry/quickstart) (name `io.github.adighatakdas-cloud/agentprint`, from PyPI, started with `uvx agentprint mcp`). It is prepared, not published.
