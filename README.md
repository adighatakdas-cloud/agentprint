# agentprint

**The security check your AI coding agent runs for you.**

[![CI](https://github.com/adighatakdas-cloud/agentprint/actions/workflows/ci.yml/badge.svg)](https://github.com/adighatakdas-cloud/agentprint/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/adighatakdas-cloud/agentprint/blob/main/LICENSE)

![agentprint's welcome screen and menu, a scan that finds keys and risky settings left by AI agents and ends on BLOCK, and the BLOCK verdict with fix steps an AI agent gets for a package that would leak a key](https://raw.githubusercontent.com/adighatakdas-cloud/agentprint/main/docs/demo.gif)

AI coding agents (Claude Code, Codex, Cursor, Gemini CLI and others) write files as they work:
chat history, settings, permission lists. Keys you paste end up in those files, and those files
end up in the packages you publish. agentprint is an offline command-line tool that finds:

- **API keys** left in agents' history and settings files;
- **risky permissions and hooks**, such as `Bash(*)`, auto-approve modes, or hooks that call the network;
- **agent files other users can read** on a shared machine;
- **agent files about to ship** in your npm or PyPI package, such as `.claude/settings.local.json`.

`agentprint install` puts the check *inside* each agent. Before the agent publishes a package or
runs `git push`, agentprint looks at what would be uploaded. If something would leak, the agent
is stopped and told how to fix it, and it asks you only for what it can't do itself: replacing
a leaked key.

**Status:** v0.1, pre-release. The `uvx` and `npx` commands work after the first release; until
then, [run it from source](#from-source).

## Why

- **Agent files leak into packages.** In April 2026 Lakera
  [reported](https://bdtechtalks.com/2026/04/27/claude-code-api-token-leak/) that of about 46,500
  npm packages it scanned, 428 shipped `.claude/settings.local.json`, and 33 files in 30
  packages held credentials. Conventional secret scanning tends to miss these files.
- **We measured it too.** Our [pilot scan](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/pilot-2026-11.md)
  of the newest 1,976 npm packages and 1,997 PyPI releases (5 November 2026, counts only) found
  agent files in 2.1% of npm packages and 9.2% of PyPI releases. 1 npm and 6 PyPI packages
  shipped files that should never ship. No key-shaped strings turned up inside those agent files.
- **History files keep what you paste.** Agents save prompts and transcripts as plain text in
  your home folder, where any program you run can read them. A June 2026 Hermes Agent advisory
  ([CVE-2026-53870](https://github.com/advisories/GHSA-99f9-j8r3-p853)) covers such files being
  created readable by every user on the machine.

## Quickstart

> Works after the first release. Until then, use [from source](#from-source).

```bash
uvx agentprint scan       # 1. check this machine and the current folder
uvx agentprint install    # 2. put agentprint inside every AI coding agent it finds
```

3. That's it. Your agents now check before `npm publish`, `uv publish`, `git push` and similar.
   When something would leak, they get the fix steps and tell you which keys to replace.

### From source

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
uv sync
uv run agentprint scan
```

More (prefix with `uv run` from source):

```bash
agentprint scan --no-home .            # only this project, not your home folder
agentprint pack .                      # what your npm/PyPI package would publish
agentprint pack dist/my-pkg-1.0.tgz    # an already-built package (.tgz, .tar.gz, .whl, .zip)
agentprint scan --format json          # also: sarif, agent (short report for an AI agent)
agentprint install --dry-run           # show what install would change, write nothing
agentprint uninstall                   # take agentprint back out of every agent
```

Exit codes: `0` nothing at or above `--fail-on` (default `high`), `1` findings, `2` usage or
packaging error. `pack` on a Python project runs `uv build`, which runs the project's build
backend and may download it: use it on projects you trust, or pass an already-built archive.

## What it checks

| ID | What it finds | Severity |
|---|---|---|
| AP001 | API key in an agent's history, transcript, paste cache, edit backups or chat database | high |
| AP002 | API key in an agent settings or allowlist file | critical |
| AP003 | Over-broad permission: `Bash(*)`, `bypassPermissions`, auto-approve modes ([per agent](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/supported-agents.md#what-ap003-flags-in-each-agent)) | medium |
| AP004 | Agent hook that calls the network: `curl`, `wget`, `nc`, `\| sh`, a URL | medium |
| AP005 | API key written inline in an MCP server entry (env, headers, args, URL) | critical |
| AP006 | Agent file (e.g. `.claude/settings.local.json`, `.aider.chat.history.md`) would ship in your npm/PyPI package | high, medium or low |
| AP007 | Personal agent file (e.g. `.claude/settings.local.json`, `CLAUDE.local.md`) in a git repo but not gitignored, or already committed | low |
| AP008 | Agent history, settings or key store readable by other users on this machine (macOS and Linux) | medium; high for key stores |

Keys are found by pattern: Anthropic, OpenAI, GitHub, AWS, Stripe, Slack and Google formats,
plus a generic "secret-looking name = random-looking value" rule. Every finding carries a fix
written for the AI agent that reads it: which file, which setting, which line to add to
`.gitignore`, and what to ask the human.

## Works inside your AI agent

`agentprint install` finds the agents on your computer and adds three things to each:

- **A hook** that runs before the agent's shell commands. Only package publishes (`npm`, `pnpm`,
  `yarn` and `bun publish`, `uv publish`, `twine upload`, changesets, lerna, ...) and `git push`
  are checked; everything else runs untouched.
- **An MCP server** (`agentprint mcp`) with the tools `check_package`, `check_push`,
  `scan_secrets` and `explain_finding`, so the agent can check on its own.
- **A short note** in the agent's global instructions: check before publishing or pushing, and
  never print a key.

When a publish would leak, this is what the agent reads (shortened):

```text
agentprint: BLOCK — 2 problems to fix before publishing
[1] critical AP002 Secret in agent file inside package (claude-code)
    where: "my-app-1.0.0.tgz!.claude/settings.local.json" line 4
    fix:
      - Delete the key from the source file ".claude/settings.local.json" in the project (line 4) ...
    ask_human:
      - Rotate the OpenAI API key that starts "sk-p…(len 56)": revoke it and create a new one at ...
[2] high AP006 Agent file would ship in package (claude-code)
    fix:
      - Add the line ".claude/" to .npmignore in the package folder ...
Do every fix step, tell the human each ask_human item, then re-run the same agentprint check.
```

Before it writes anything, `install` prints the lines it adds to each file and backs the file up
in `~/.agentprint/backups`. Running it twice changes nothing. **To undo:** `agentprint uninstall`
takes out only what agentprint added (`--agent codex` for one agent); a file you haven't touched
since comes back byte for byte.

Good to know:

- **Codex** runs a new hook only after you trust it: type `/hooks` in Codex once after installing.
- **Hermes Agent** asks before it runs any new hook, and agentprint never approves its own. Start
  `hermes chat` once and accept it. Until then, and in gateway or cron runs that never ask, the
  hook is off; the MCP server still works.
- **Hooks need macOS or Linux.** On Windows, only the MCP server and the note are installed for now.
- **agentprint only ever blocks, never approves.** Your agents' own approval rules still apply,
  and if agentprint itself fails, the command runs, with a note that it could not check.
- **Spotting a publish is best effort.** agentprint reads the command line; it isn't a shell. It
  follows `cd`, `&&`, `bash -c`, package scripts, workspaces and release tools, but a publish
  hidden in a Makefile, a script file or a heredoc can slip past (the note still tells the agent
  to check). When unsure what would be uploaded, it says "could not check", never "OK".

## Supported agents

| Agent | Scan | Install | Tested |
|---|---|---|---|
| Claude Code | yes | hook, MCP, note | live in CI |
| Codex | yes | hook, MCP, note | live in CI |
| OpenCode | yes | plugin, MCP, note | live in CI |
| Gemini CLI | yes | hook, MCP, note | live in CI |
| GitHub Copilot | yes (CLI and VS Code) | hook, MCP, note | live in CI (Copilot CLI) |
| Hermes Agent | yes | hook (after you approve it), MCP | live in CI |
| Cursor | yes | hook, MCP, note | docs-verified |
| Windsurf | yes | hook, MCP, note | docs-verified |
| Google Antigravity | yes | hook, MCP, note | docs-verified |
| Devin Desktop / CLI | not yet (its new folders) | hook, MCP, note | docs-verified |

**Scan-only** (15 more): Aider, Cline, Kilo Code, Qwen Code, Crush, Goose, Kiro, Factory Droid,
Continue, Zed, Pi, OpenClaw, Warp, Roo Code, Augment. agentprint knows which of their files must
never ship in a package and which personal files they write inside a project.

**Live in CI:** a nightly job runs the real agent program against a scripted mock model (no
account, no real key, a throwaway home folder) and checks that a leaky `npm publish` or `git push`
is stopped with the reason reaching the model, and a clean one goes ahead. **Docs-verified:**
built from the agent's documentation and real folders, not driven end to end (desktop apps with
no headless mode, or an account needed). Versions and file paths:
[docs/supported-agents.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/supported-agents.md).

## Use it in CI

> Works after the first release.

**Stop a leaky `npm publish`.** In `package.json` (the npm name `agentprint` belongs to an
unrelated project, so the wrapper is `agentprint-cli`; it runs the same tool through uvx or pipx):

```json
"scripts": { "prepublishOnly": "npx agentprint-cli pack" }
```

**GitHub Action.** In `.github/workflows/agentprint.yml`:

```yaml
on: [push, pull_request]
permissions: { contents: read, security-events: write }  # security-events: for upload-sarif
jobs:
  agentprint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: adighatakdas-cloud/agentprint@v0.1.0  # better: pin the release's commit SHA
        with: { command: pack, upload-sarif: true }  # command: pack or scan
```

Inputs: `command`, `path` (default `.`), `fail-on` (default `high`; `none` only reports),
`upload-sarif` (default `false`). Outputs: `exit-code`, `sarif-file`.

**pre-commit.** In `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/adighatakdas-cloud/agentprint
    rev: v0.1.0
    hooks:
      - id: agentprint-pack   # agent files or keys in your package
      - id: agentprint-scan   # keys in this repo's agent settings
```

## Privacy

- **Offline.** `scan` makes no network calls. No telemetry, no account.
- **Keys are redacted the moment they are found**, to their first 4 characters and length
  (`sk-a…(len 108)`), in every output format.
- **Found keys are never used**: not sent anywhere, not tested, not even to check whether they
  still work.
- **Credential stores are never read.** Files where a key belongs (`~/.codex/auth.json`,
  `~/.hermes/.env`, ...) are only checked for who can read them.
- **Downloads happen only when you start them:** `uvx`/`npx` fetching agentprint, and `pack`
  running your own `npm pack --dry-run` or `uv build`.

## Alternatives

Checked against each project's README on 5 November 2026. These tools do different jobs and
work well side by side.

| Tool | What it focuses on | Account or network | License |
|---|---|---|---|
| [AgentShield](https://github.com/affaan-m/agentshield) | Claude Code setups (`~/.claude/`): secrets, permissions, hooks, MCP servers, prompt-injection vectors; `--fix`, GitHub Action, SARIF | none for the scan; an optional deep-analysis mode calls Claude with your API key | MIT |
| [snyk/agent-scan](https://github.com/snyk/agent-scan) | MCP servers and skills across many agents: prompt injection, tool poisoning, malware in skills | needs a Snyk account and `SNYK_TOKEN`; starts or connects to MCP servers (with consent) to read their tools | Apache-2.0 |
| [agentsweep](https://github.com/Ishannaik/agent-sweep) | secrets in agent history files across 32 agents, with in-place redaction | none, offline | MIT |
| [gitleaks](https://github.com/gitleaks/gitleaks), [trufflehog](https://github.com/trufflesecurity/trufflehog) | secrets anywhere in git history and files; not specific to AI agents | none for gitleaks; trufflehog can verify keys with their providers | MIT; AGPL-3.0 |
| **agentprint** | keys, permissions, hooks and file modes across 9 agents' files, plus agent files in npm/PyPI packages; installs itself into 10 agents as a pre-publish and pre-push check | none, offline; never uses a key it finds | MIT |

## How it works

One module per agent knows where that agent keeps its files; one module per check (AP001 to
AP008) decides what is risky; per-agent integrations translate hook payloads and replies. The
design and its safety decisions are in
[docs/ARCHITECTURE.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/ARCHITECTURE.md).

Tech: Python 3.11+, [uv](https://docs.astral.sh/uv/), typer, rich, the MCP Python SDK, pytest,
ruff. A thin npm wrapper runs the same Python tool.

## Help scan npm and PyPI

We measure how often agent files, and keys inside them, ship in newly published packages, and
tell maintainers privately first. Volunteers each scan a small shard and post counts only:
see [docs/VOLUNTEER_SCAN.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/VOLUNTEER_SCAN.md).

## Contributing

Adding an agent takes one new file and one registry line: see
[CONTRIBUTING.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/CONTRIBUTING.md) and the
[good first issues](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/good-first-issues.md).

## Security and license

Found a security problem in agentprint? Please report it privately, as
[SECURITY.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/SECURITY.md) explains.
agentprint is released under the [MIT License](https://github.com/adighatakdas-cloud/agentprint/blob/main/LICENSE).
