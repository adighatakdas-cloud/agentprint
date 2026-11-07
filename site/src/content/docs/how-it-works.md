---
title: How it works
description: The parts of agentprint, how a sweep and a publish check move through them, and the safety decisions behind them.
---

Every AI coding agent keeps files in its own places (`~/.claude/`, `~/.codex/`, and others) in its own formats. agentprint separates **where files are** (one module per agent) from **what is risky** (one module per check). A third layer, the integrations, plugs agentprint into each agent: it reads the agent's hook messages and answers in the agent's own format.

All of it produces one thing, a list of findings. They are already masked. A person sees them grouped by severity, ending on BLOCK or CLEAR. An AI agent gets a brief: a verdict, fix steps, and what to ask the human. Who is running agentprint decides which.

## The parts

- **Agents** (`agentprint/agents/`): one module per agent. It says where the agent keeps files in your home folder and in a project, how to read its config (permissions, hooks, MCP servers), and which of its files must never ship in a package. Fifteen lighter agents live in `scan_only.py`. Adding an agent never touches a check.
- **Checks** (`agentprint/checks/`): one module per [check](/agentprint/checks/). A check gets the found files and returns findings. Checks never parse an agent's file format. When several checks see the same key, the most precise one claims it, so you get one finding.
- **Secrets** (`secrets.py`): key patterns and the masking function.
- **Pack** (`pack.py`): asks the package manager what a package would ship (`npm pack --dry-run --ignore-scripts`, or `uv build`), or reads a built archive in memory. AP006 then matches every file name against all agents' package rules.
- **Integrations** (`agentprint/integrations/`): one module per agent. Each gives the three edits `install` makes (hook, MCP server, note), how to read the agent's hook message, and how to answer in its format.
- **Commands and guard** (`commands.py`, `guard.py`): read the shell command an agent is about to run, spot publishes and pushes, work out what would upload, and check it. Everything else passes straight through.
- **Output** (`advice.py`, `report.py`, `sarif.py`, `ui/`): fix steps and what to ask the human; JSON, agent and SARIF output; and a person's screens.

## Two paths through the code

**`agentprint scan`:** the CLI asks every agent module for its files in your home and in the project. Every check runs over that list. Duplicate claims are dropped, findings are sorted most severe first, and the result is printed for a person or an AI agent.

**An agent about to run `npm publish`:** the agent runs its hook. The hook runs `~/.agentprint/bin/agentprint-hook`, which runs `agentprint guard --agent <name>`. agentprint reads the agent's JSON, extracts the command and folder, sees "npm publish in my-app/", and lists the package with `npm pack --dry-run`. AP006 finds `.claude/settings.local.json` with a key in it. The verdict is BLOCK, and agentprint replies in the agent's own format. The agent's model reads the fix steps.

## Safety decisions

- **Keys are masked at match time.** A full key exists only inside the check that found it, while that check reads the line. The finding stores the first 4 characters and the length (`sk-a…(len 105)`). No output format can print a full key, because no finding holds one.
- **Keys are never used.** Detection is by pattern only. agentprint never sends a key anywhere, not even to check whether it still works. `scan` makes no network calls.
- **Hooks fail open.** A security tool that breaks your agent gets uninstalled. So any error lets the command run, with a warning. When agentprint cannot tell what a publish would upload, it says "could not check", never "OK".
- **Scanned names are untrusted.** File names inside a package are chosen by whoever built it. Before any of that text is shown, agentprint removes terminal escape sequences, control characters and invisible Unicode, and caps the length. In output for an AI agent, scanned text is a quoted JSON string under a note that says it is data, not instructions.
- **Commands are never shortened.** A fix step that holds a shell command is shown whole and unchanged, or replaced by a plain-words instruction. Cutting a quoted command could turn part of a hostile file name into something that runs.
- **Installs are reversible.** Every edit marks what it added, so `uninstall` removes exactly that and keeps your own changes. Files are backed up first and written atomically. A file agentprint cannot edit safely is left alone, and the lines to add by hand are printed instead.
- **Reading is bounded.** SQLite databases are opened read-only. Archives are read in memory with caps on entries and size. YAML is read with a safe loader.

## How it is tested

Unit tests build fake homes and projects at test time, with synthetic keys assembled at runtime so no key-shaped text is committed. Integration tests run real `npm pack` and `uv build`. Live tests start the real agent programs (see [Agents](/agentprint/agents/#live-tests)).

## For contributors

The full design, with a diagram and a reading order for the code, is in [docs/ARCHITECTURE.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/ARCHITECTURE.md). Tech: Python 3.11+, [uv](https://docs.astral.sh/uv/), typer, rich, the MCP Python SDK, pytest, ruff. A thin npm wrapper runs the same Python tool.
