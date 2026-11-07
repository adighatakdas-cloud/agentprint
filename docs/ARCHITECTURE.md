# How agentprint works

A five-minute tour: the parts, how data moves through them, and the safety decisions behind
them. File names are relative to `agentprint/`.

## The idea in one paragraph

Every AI coding agent keeps files in its own places (`~/.claude/`, `~/.codex/`, ...) in its
own formats. agentprint separates **where files are** (one module per agent) from **what is
risky** (one module per check). A third layer, **integrations**, plugs agentprint into each
agent: it reads the agent's hook messages and answers in the agent's own format. All of it
produces the same thing, a list of findings, which are already redacted and are printed for a
person (grouped by severity, ending on BLOCK or CLEAR) or for an AI agent (a verdict, fix
steps, and what to ask the human). Who is running it decides which (`ui/detect.py`).

## The picture

```text
 HOW IT IS STARTED                 WHAT RUNS                            WHAT COMES OUT
 ─────────────────                 ─────────                            ──────────────
 agentprint scan ───► scanner.py ─► agents/*.py   find each agent's files
                                    checks/*.py   AP001-AP008: what is risky ──┐
 agentprint pack ───► pack.py ────► checks/ap006  agent files in a package    │
                                                                               ▼
 agent's hook ──────► hook.py ────► commands.py   publish or push?          Findings
   (JSON in,          integrations/ guard.py      check what would upload   (redacted)
    reply out)        (per agent)   pack.py / gitcheck.py ─────────────────────┤
                                                                               ▼
 agent's MCP call ──► mcp_server.py ─► the same checks ──────────► advice.py  fix_steps, ask_human
                                                                   report.py  json | agent
                                                                   ui/        a person's screens
                                                                   sarif.py   GitHub code scanning
                                                                   sanitize.py on every scanned string

 agentprint install ─► install.py + integrations/*.py: a hook, an MCP entry and a note per agent
```

## The parts

**`agents/` (where files are).** One module per agent. Each says:

- where the agent keeps files in your home folder (`discover_home`) and inside a project
  (`discover_project`), tagging each file with a kind: `history`, `settings`, `personal` or
  `credentials`;
- how to read its config (`read_config`): which settings let it act without asking (grants),
  which hooks it runs, and which MCP servers it starts;
- which of its history files are only records (`pure_history`), so `agentprint fix` may mask a
  key there, and which it reads back as instructions or config;
- which of its files must never ship in a package (`package_rules`).

`agents/scan_only.py` holds 15 lighter agents that are only package rules and project files.
Adding an agent never touches a check.

**`checks/` (what is risky).** One module per check, `ap001_...` to `ap008_...`. A check gets
the list of found files and returns findings. Checks never parse an agent's file format; they
use what the agent module understood. When two checks see the same key (an API key inside an
MCP server entry is both AP002 and AP005), the more precise one claims it and the other drops
it, so you get one finding.

**`secrets.py`.** Key patterns (Anthropic, OpenAI, GitHub, AWS, Stripe, Slack, Google, newer
formats, four of them matched only next to the provider's name, and a generic
name-plus-random-value rule with an entropy test) and `redact()`.

**`pack.py` (what a package ships).** Asks the package manager itself: `npm pack --dry-run
--ignore-scripts` (no package code runs) or `uv build` for Python projects, or reads an
already-built `.tgz`, `.whl`, `.tar.gz` or `.zip` in memory. AP006 then matches every file name
against all agents' package rules and scans matching files for keys.

**`integrations/` (inside each agent).** One module per agent (Devin shares Windsurf's). Each
gives the agent's config folder, the three edits `install` makes (hook, MCP server, note), how
to read the agent's hook message (`parse_hook`: which command, in which folder) and how to
answer (`reply`: the agent's own "deny" JSON or exit code). `install.py` applies the edits.

**`commands.py` and `guard.py` (is this a publish?).** `commands.py` reads the shell command an
agent is about to run and spots publishes and pushes. It is a careful reader, not a shell: it
follows `cd`, `&&`, `bash -c`, `npm run release` (by reading `package.json`), workspaces and
release tools. `guard.py` then works out what would be uploaded and checks it. Everything that
isn't a publish or a push passes straight through without any check.

**`mcp_server.py`.** An MCP server (official MCP Python SDK) with four tools:
`check_package`, `check_push`, `scan_secrets`, `explain_finding`. Same checks, agent format.

**Output.** `advice.py` turns each finding into `fix_steps` (what the agent can do itself) and
`ask_human` (what only a person can do, like rotating a key at the provider, with the link).
`report.py` renders JSON and the agent format; `sarif.py` writes SARIF for GitHub.

**`ui/` (a person's screens).** `detect.py` tells a person, an AI agent and CI apart: an agent
(found by an env marker such as `CLAUDECODE`) gets `--format agent` from a plain `scan` or
`pack`, and never color, animation or the menu; a pipe gets a person's layout without color.
`results.py` draws the findings grouped by severity, the fixes and BLOCK or CLEAR;
`welcome.py`, `sweep.py` and `menu.py` are what a bare `agentprint` shows; `layout.py` fits
lines to the terminal; every color lives in `theme.py`.

## Two paths through the code

**`agentprint scan`:** `cli.py` → `scanner.discover()` asks every agent module for its files
in your home and the project → every check in `checks/__init__.py` runs over that list →
duplicate claims are dropped → findings are sorted, most severe first → `ui/results.py` prints
them for a person, `report.py` for an AI agent.

**An agent about to run `npm publish`:** the agent runs its hook → the hook runs
`~/.agentprint/bin/agentprint-hook`, which runs `agentprint guard --agent <name>` → `hook.py`
reads the agent's JSON from stdin → the agent's integration extracts the command and folder →
`commands.py` says "npm publish in my-app/" → `guard.py` lists the package with
`npm pack --dry-run` → AP006 finds `.claude/settings.local.json` with a key in it → the verdict
is BLOCK → the integration replies in the agent's own format, and the agent's model reads the
fix steps.

## Safety decisions

**Keys are redacted at match time.** A full key exists only inside the check that found it,
for as long as that check is reading the line. The finding stores `redact(value)`: the first 4
characters and the length (`sk-a…(len 108)`). No output format can print a full key, because
no finding holds one. The research scanner in `tools/ecoscan` follows the same rule.

**Keys are never used.** Detection is pattern-only. agentprint never sends a key anywhere, not
even to check whether it still works. `scan` makes no network calls, and files where a key is
supposed to live (`~/.codex/auth.json`, `~/.hermes/.env`, ...) are never read at all.

**Hooks fail open.** A security tool that breaks your agent gets uninstalled. So any error
(bad input, unknown agent, a bug in agentprint) lets the command run, with a warning. The
launcher script answers "allow" itself if agentprint is missing, and it uses each agent's own
"allow": GitHub Copilot treats any non-zero exit as "deny", and Cursor blocks on empty output.
When agentprint can't tell what a publish would upload, it says "could not check", never "OK".

**Scanned names are untrusted.** File names inside a package are chosen by whoever built it.
Before any of that text is shown, `sanitize.clean()` removes terminal escape sequences, control
characters and invisible Unicode (such as bidi overrides) and caps the length. In output for an
AI agent, scanned text is a quoted JSON string, under a note that says it is data, not
instructions: a file name can't pose as an instruction to the model.

**Commands are never shortened.** A fix step that holds a shell command is shown whole and
unchanged, or replaced by a plain-words instruction (`sanitize.safe_step`). Cutting or editing a
shell-quoted command could turn part of a hostile file name into something that runs. AP008
puts a path into its `chmod` command (on Windows, its `icacls` command, from `winacl.py`) only
when every character is in a small safe set.

**Installs are reversible.** Every edit marks what it added (a marker comment, an `agentprint`
key, or the launcher's name in a hook command), so `uninstall` removes exactly that and keeps
your own changes. Files are backed up first (the backups are readable only by you) and
written atomically. A file agentprint can't edit safely (JSON with comments, YAML with anchors) is left alone, and the
lines to add by hand are printed instead.

**Reading is bounded.** SQLite chat databases are opened read-only. Archives are read in
memory, never unpacked to disk, with caps on entries, unpacked size and content read. YAML is
read with a safe loader that gives up after 100 aliases, so a crafted file can't blow up.

## How it is tested

- **Unit tests** build fake homes and projects at test time (`tests/conftest.py`), copying the
  real folder layouts with synthetic values. Every key comes from `tests/fakes.py`, assembled
  at runtime with a `FAKE` body, so no key-shaped text is ever committed.
- **Integration tests** (`-m integration`) run real `npm pack` and `uv build`.
- **Live tests** (`tests/live/`, `-m live`) start the real agent CLIs (Claude Code, Codex,
  OpenCode, Gemini CLI, Copilot CLI, Hermes; pinned versions) against a scripted mock model on
  `127.0.0.1` that speaks the Anthropic, OpenAI and Gemini APIs. The home folder is throwaway,
  `npm` and `git` are fakes that only leave marker files, and the test package points at a
  registry address that does not exist. No account, no real key. The mock model asks the agent
  to publish or push, and the test checks that agentprint's BLOCK reason reaches the model.

## Where to start reading

`cli.py` (the commands) → `scanner.py` (40 lines) → `agents/claude_code.py` (one agent) →
`checks/ap001_history_secret.py` (one check) → `hook.py` and `guard.py` (the hook path).
