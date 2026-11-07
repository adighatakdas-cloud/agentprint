<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/adighatakdas-cloud/agentprint/main/docs/brand/logo-dark.svg">
    <img alt="agentprint" src="https://raw.githubusercontent.com/adighatakdas-cloud/agentprint/main/docs/brand/logo-light.svg" width="440">
  </picture>
</p>

<h3 align="center">The security check your AI coding agent runs for you.</h3>

<p align="center">
  <a href="https://github.com/adighatakdas-cloud/agentprint/actions/workflows/ci.yml"><img alt="tests" src="https://img.shields.io/github/actions/workflow/status/adighatakdas-cloud/agentprint/ci.yml?branch=main&label=tests&style=flat-square&labelColor=050B14&color=FFB020"></a>
  <a href="https://pypi.org/project/agentprint/"><img alt="PyPI" src="https://img.shields.io/pypi/v/agentprint?style=flat-square&labelColor=050B14&color=FFB020"></a>
  <a href="https://www.npmjs.com/package/agentprint-cli"><img alt="npm" src="https://img.shields.io/npm/v/agentprint-cli?style=flat-square&labelColor=050B14&color=FFB020"></a>
  <a href="https://github.com/adighatakdas-cloud/agentprint/blob/main/LICENSE"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-FFB020?style=flat-square&labelColor=050B14"></a>
</p>

```bash
uvx agentprint
npx agentprint-cli
```

<p align="center">
  <img alt="agentprint's welcome screen and menu, a scan that finds keys and risky settings left by AI agents and ends on BLOCK, and the BLOCK verdict with fix steps an AI agent gets for a package that would leak a key" src="https://raw.githubusercontent.com/adighatakdas-cloud/agentprint/main/docs/demo.gif">
</p>

<p align="center">
  <sub>Works inside: Claude Code · Codex · Cursor · Gemini CLI · Windsurf · Devin · OpenCode · Copilot · Hermes · Antigravity</sub>
</p>

<p align="center">
  <a href="https://adighatakdas-cloud.github.io/agentprint/">Docs</a> ·
  <a href="https://adighatakdas-cloud.github.io/agentprint/get-started/">Get started</a> ·
  <a href="https://adighatakdas-cloud.github.io/agentprint/checks/">Checks</a> ·
  <a href="https://adighatakdas-cloud.github.io/agentprint/ci/">Use in CI</a>
</p>

## Why

- **Agent files leak into packages.** In April 2026 Lakera
  [reported](https://bdtechtalks.com/2026/04/27/claude-code-api-token-leak/) that of about 46,500
  npm packages it scanned, 428 shipped `.claude/settings.local.json`, and 33 files in 30
  packages held credentials.
- **We measured it too.** Our [pilot scan](https://adighatakdas-cloud.github.io/agentprint/census/pilot-2026-11/)
  (5 November 2026, counts only) of the newest 1,976 npm packages and 1,997 PyPI releases found
  agent files in 2.1% of npm packages and 9.2% of PyPI releases. 1 npm and 6 PyPI packages
  shipped files that should never ship. No key-shaped strings turned up inside those agent files.
- **History files keep what you paste.** Agents save prompts as plain text in your home folder.
  A June 2026 Hermes Agent advisory
  ([CVE-2026-53870](https://github.com/advisories/GHSA-99f9-j8r3-p853)) covers such files being
  created readable by every user on the machine.

## Quickstart

> **Pre-release:** the `uvx` and `npx` commands work after the first release. Until then, see [Get started](https://adighatakdas-cloud.github.io/agentprint/get-started/) to run from source.

```bash
uvx agentprint scan      # sweep this machine and folder (keys masked)
uvx agentprint fix       # make the safe fixes; shows the plan and asks first
uvx agentprint install   # arm each agent it finds with the tripwire
uvx agentprint pack      # preflight: what your package would publish
```

[Get started](https://adighatakdas-cloud.github.io/agentprint/get-started/) has the details,
including running from a clone of this repo.

## What it checks

<!-- checks:start -->
| ID | What it finds | Severity |
|---|---|---|
| [AP001](https://adighatakdas-cloud.github.io/agentprint/checks/ap001/) | An API key sits in an agent's history, transcript, paste cache, edit backups or chat database. | high |
| [AP002](https://adighatakdas-cloud.github.io/agentprint/checks/ap002/) | An API key sits in an agent settings or allowlist file, or inside an agent file in a package or push. | critical |
| [AP003](https://adighatakdas-cloud.github.io/agentprint/checks/ap003/) | An over-broad permission lets the agent run any command or skip every prompt, such as Bash(\*) or bypassPermissions. | medium |
| [AP004](https://adighatakdas-cloud.github.io/agentprint/checks/ap004/) | An agent hook calls the network, with curl, wget, nc, a pipe into a shell, or a URL. | medium |
| [AP005](https://adighatakdas-cloud.github.io/agentprint/checks/ap005/) | An API key is written inline in an MCP server entry (env, headers, args, command or URL). | critical |
| [AP006](https://adighatakdas-cloud.github.io/agentprint/checks/ap006/) | An agent file, such as .claude/settings.local.json, would ship in your npm or PyPI package. | high, medium or low, by file |
| [AP007](https://adighatakdas-cloud.github.io/agentprint/checks/ap007/) | A personal agent file, such as CLAUDE.local.md, is in a git repo but not gitignored, or is already committed. | low |
| [AP008](https://adighatakdas-cloud.github.io/agentprint/checks/ap008/) | Agent history, settings or a key store can be read by other users on this machine (macOS and Linux). | medium; high for key stores |
| [AP009](https://adighatakdas-cloud.github.io/agentprint/checks/ap009/) | Source maps, original source or AI instruction files in your package hand an AI your code. | high to low by licence; no licence, UNLICENSED, SEE LICENSE IN, proprietary or only LicenseRef- choices count as closed; low for a private registry |
<!-- checks:end -->

Keys are found by pattern (Anthropic, OpenAI, GitHub, AWS, Stripe, Slack and Google formats, plus
a generic "secret-looking name = random-looking value" rule) and are never used or sent anywhere.

## Inside your AI agent

- A hook runs before the agent publishes a package or runs `git push`, and checks what would go out.
- An MCP server gives the agent tools to check on its own.
- If something would leak, the agent gets a brief: the fix steps, plus what to ask you (rotate a key).
- It only ever blocks (BLOCK or CLEAR); your agent's own approval rules still apply.
- `agentprint uninstall` takes out exactly what `install` added.

Details: [Inside your agent](https://adighatakdas-cloud.github.io/agentprint/inside-your-agent/).

## More

[Agents](https://adighatakdas-cloud.github.io/agentprint/agents/) ·
[Use in CI](https://adighatakdas-cloud.github.io/agentprint/ci/) ·
[Privacy](https://adighatakdas-cloud.github.io/agentprint/privacy/) ·
[Comparison](https://adighatakdas-cloud.github.io/agentprint/comparison/) ·
[Census](https://adighatakdas-cloud.github.io/agentprint/census/) ·
[How it works](https://adighatakdas-cloud.github.io/agentprint/how-it-works/)

[Contributing](https://github.com/adighatakdas-cloud/agentprint/blob/main/CONTRIBUTING.md) ·
[Security](https://github.com/adighatakdas-cloud/agentprint/blob/main/SECURITY.md) ·
[License (MIT)](https://github.com/adighatakdas-cloud/agentprint/blob/main/LICENSE)

Built with Python 3.11+, uv, typer, rich, the MCP Python SDK, pytest and ruff; docs with Astro Starlight.
