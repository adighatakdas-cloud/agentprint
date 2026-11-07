---
title: Get started
description: Run your first sweep, put agentprint inside your AI coding agents, and learn the commands and exit codes.
---

agentprint is an offline command-line tool. It looks for what AI coding agents leave behind: API keys in history files, risky permissions and hooks, and agent files that would ship in your npm or PyPI package.

:::note
agentprint is v0.1 and pre-release. The `uvx` and `npx` commands work after the first release. Until then, [run it from source](#from-source).
:::

## Two commands

```bash
uvx agentprint scan       # 1. check this machine and the current folder
uvx agentprint install    # 2. put agentprint inside every AI coding agent it finds
```

The first command is a sweep (`agentprint scan`): it reads the files your agents keep and reports what it finds. The second arms your agents (`agentprint install`): it adds a tripwire, a check that runs inside each agent. Before an armed agent runs `npm publish`, `uv publish`, `git push` and similar commands, agentprint looks at what would be uploaded. If something would leak, the agent is stopped and told how to fix it. See [Inside your AI agent](/agentprint/inside-your-agent/).

## Fix what it finds

```bash
uvx agentprint fix
```

`agentprint fix` shows you a plan and asks before it changes anything. Then it makes the fixes that are safe to automate: it masks keys in history files, points Claude Code MCP servers at environment variables, adds ignore lines and makes readable agent files private. It sweeps again and ends on BLOCK or CLEAR. What only you can do, such as rotating a key, it lists under **Needs you**. See [Fix what it finds](/agentprint/fix/).

## From source

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
uv sync
uv run agentprint scan
```

Prefix the commands below with `uv run` when you run from source.

## The commands

| Command | What it does |
|---|---|
| `agentprint scan` | A sweep: checks your home folder and the current folder for keys, risky settings and readable agent files. |
| `agentprint fix` | Makes the safe fixes for what `scan` finds, after showing you the plan. `--undo` puts back what it can. |
| `agentprint pack` | A preflight: checks what your npm or PyPI package would publish. |
| `agentprint install` | Arms every AI coding agent it finds: a hook, an MCP server and a short note in each agent's global instructions. |
| `agentprint uninstall` | Disarms your agents: takes out only what `install` added. |
| `agentprint status` | Shows the roster: which AI agents are on this machine and which are armed. |
| `agentprint welcome` | Shows the welcome screen again. |
| `agentprint guard` | What an agent's hook runs. You do not call it yourself. |
| `agentprint mcp` | Starts agentprint's MCP server for an agent. You do not call it yourself. |

More examples:

```bash
agentprint scan --no-home .            # only this project, not your home folder
agentprint pack .                      # what your npm/PyPI package would publish
agentprint pack dist/my-pkg-1.0.tgz    # an already-built package (.tgz, .tar.gz, .whl, .zip)
agentprint scan --format json          # also: table, sarif, agent
agentprint fix --dry-run               # show what fix would change, change nothing
agentprint install --dry-run           # show what install would change, write nothing
agentprint uninstall                   # take agentprint back out of every agent
```

`scan` and `pack` also take a path, `--fail-on` and `--plain`. `scan` takes `--home` to sweep a different home folder. `install` and `uninstall` take `--agent` and `--home`.

## Output formats

`--format` picks how a result is written.

| Format | For |
|---|---|
| `table` | A person at a terminal: findings grouped by severity, the fixes, and a BLOCK or CLEAR verdict. |
| `json` | Scripts. |
| `sarif` | GitHub code scanning. |
| `agent` | An AI coding agent: the verdict first, then fix steps and what to ask the human. |

You do not have to pick. Inside an AI coding agent, `scan`, `pack` and `fix` choose `--format agent` for you. In a terminal they choose `table`.

`--plain` turns off color and animation. It works before or after the command: `agentprint --plain scan` and `agentprint scan --plain` are the same.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Nothing at or above `--fail-on`. |
| `1` | At least one finding at or above `--fail-on`. |
| `2` | Usage or packaging error. |

`--fail-on` takes a severity and defaults to `high`. `pack` on a Python project runs `uv build`, which runs the project's build backend and may download it. Use it on projects you trust, or pass an already-built archive.

## Running `agentprint` with no command

- **First run, in a terminal:** the welcome screen. It shows the roster of agents agentprint found and how to start.
- **Later runs, in a terminal:** a menu with the same actions as the commands above. It closes by itself after 60 seconds without a key press.
- **An AI agent, a pipe or CI:** plain help text. agentprint never waits for a key press there.

## Next

- [What agentprint checks](/agentprint/checks/): eight checks, AP001 to AP008.
- [Use it in CI](/agentprint/ci/).
- [Privacy](/agentprint/privacy/): what agentprint reads, and what it never does.
