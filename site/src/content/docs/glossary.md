---
title: Glossary
description: The words agentprint uses, from print and sweep to SARIF and tarball, each with a one-line meaning.
---

## agentprint's words

**print**: a trace an AI coding agent leaves behind: a key in a history file, a risky permission, an agent file in a package.

**sweep**: a scan of your machine and the current folder for prints. The command is `agentprint scan`.

**preflight**: a check of what a package would publish, before it goes out. The command is `agentprint pack`.

**tripwire**: the check that runs inside an agent before it publishes a package or runs `git push`. It stops the agent if something would leak.

**armed**: an agent that has the tripwire installed. `agentprint install` arms agents.

**unarmed**: an agent that does not have the tripwire. `agentprint uninstall` disarms an agent.

**roster**: the list of AI agents agentprint found on your machine, and which are armed. The command is `agentprint status`.

**BLOCK**: the verdict when a problem at or above `--fail-on` was found. The exit code is `1`.

**CLEAR**: the verdict when nothing at or above `--fail-on` was found. The exit code is `0`.

**masked**: a key shown as its first four characters and its length, like `sk-a…(len 105)`. agentprint never prints a full key.

**brief**: what an AI agent reads from agentprint: the verdict, fix steps it can do itself, and what to ask the human. Its format is `--format agent`.

**census**: a count of how often agent files, and keys inside them, ship in newly published npm and PyPI packages. See [Census](/agentprint/census/).

## Plain-English terms

### hook

A small program an AI agent runs at set moments, such as before it runs a shell command. agentprint's hook runs before publishes and `git push`.

### MCP server

A program an AI agent can call for extra tools. agentprint's MCP server (`agentprint mcp`) gives the agent `check_package`, `check_push`, `scan_secrets` and `explain_finding`.

### SARIF

A standard file format for security findings. GitHub code scanning reads it. Use `--format sarif`.

### exit code

The number a command returns when it ends. `0` means success. agentprint uses `1` for findings and `2` for a usage or packaging error. CI jobs fail on a non-zero exit code.

### uvx

Runs a Python tool without installing it first. `uvx agentprint scan` downloads agentprint and runs it. It comes with [uv](https://docs.astral.sh/uv/).

### npx

Runs an npm package without installing it first. `npx agentprint-cli pack` is the npm wrapper for the same tool.

### .gitignore / .npmignore

Files that list what git or npm should leave out. A line like `.claude/` in `.npmignore` keeps agent files out of your npm package.

### tarball

A packed archive of files, often ending in `.tgz` or `.tar.gz`. npm publishes packages as tarballs.

### wheel

A built Python package, a file ending in `.whl`. PyPI releases usually have a wheel and a source archive (sdist).
