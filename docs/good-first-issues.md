# Good first issues

Concrete, self-contained tasks, each opened as a GitHub issue at launch. Every one fits the
"one module per agent, one module per check" layout, so you can do it without knowing the
whole codebase. Read [CONTRIBUTING.md](../CONTRIBUTING.md) first; it explains the steps and
the rules (no real keys, ever).

Agent paths below come from each agent's docs as of November 2026. Check them against the
current docs (and a real install, if you have one) before you code, and link the docs in your
module's docstring.

## 1. Sort `pack` findings most severe first

`agentprint scan` returns findings most severe first; `agentprint pack` returns them in the
order the files were found. A person's view groups them by severity and the agent format sorts
them, but in `pack --format json` and `--format sarif` a `high` finding can sit above a
`critical` one.

- **Where:** `run_pack_command()` in `agentprint/cli.py`; `scan()` in `agentprint/scanner.py`
  shows the sort to reuse.
- **Done when:** the JSON and SARIF output of `pack` are sorted like `scan`, with a test (the
  output snapshots in `tests/data/snapshots/` change on purpose).

## 2. Understand heredocs when spotting a publish

`commands.py` reads the command an agent is about to run, but not the body of a heredoc, so
this publish is not checked:

```bash
bash <<'EOF'
cd packages/core && npm publish
EOF
```

- **Where:** `agentprint/commands.py` (its docstring lists what it follows today).
- **Done when:** a heredoc fed to `bash`, `sh` or `zsh` is read like `bash -c`, and tests cover
  quoted and unquoted delimiters and `<<-`. Anything it can't read stays "could not check",
  never "OK".

## 3. Hooks on Windows

The hook launcher is a POSIX shell script (`~/.agentprint/bin/agentprint-hook`), so on Windows
`install` adds only the MCP server and the instructions note.

- **Where:** `launcher_script()` and `Launch` in `agentprint/install.py` and
  `agentprint/integrations/base.py`.
- **Done when:** Windows gets a PowerShell (or `.cmd`) launcher with the same rules: exit 0
  with the agent's own "allow" on any failure, block only on a clear verdict. Start with one
  agent whose Windows hook format is documented (Claude Code or Codex), and keep the POSIX path
  unchanged.

## 4. Qwen Code integration

A sweep already reads Qwen Code in full (`agentprint/agents/qwen_code.py`). Its docs describe
Claude-style hooks, so `install` can arm it the way it arms Claude Code.

- **Install:** a hook and MCP entry in `~/.qwen/settings.json`; the note in `~/.qwen/QWEN.md`.
- **Done when:** `agentprint/integrations/qwen_code.py` exists with tests, and
  `site/src/content/docs/agents.md` is updated (then run `uv run python -m tools.gen_docs`).

## 5. Goose integration

A sweep already reads Goose in full (`agentprint/agents/goose.py`). Per its docs it supports a
blocking pre-tool hook (exit 2) in plugin folders, MCP servers as `extensions:` in
`~/.config/goose/config.yaml`, and global hints in `~/.config/goose/.goosehints`.

- **Install:** hook, MCP entry and note, through Goose's own files. YAML edits must keep the
  user's comments and layout (see how `integrations/hermes.py` does it).
- **Done when:** `agentprint/integrations/goose.py` exists with tests; mark it "docs-verified" in
  `site/src/content/docs/agents.md`.

## 6. Kilo Code: reuse the OpenCode plugin

Kilo Code is built on OpenCode's core, and its plugin API (`tool.execute.before`, throw to
block) matches the plugin agentprint already installs for OpenCode.

- **Where:** `agentprint/integrations/opencode.py` (the plugin and its JSONC handling).
- **Install:** the plugin under `~/.config/kilo/`, MCP in `~/.config/kilo/kilo.jsonc`, the note
  in `~/.config/kilo/AGENTS.md`.
- **Done when:** `install --agent kilo` works with tests, sharing code with OpenCode rather
  than copying it.

## 7. Scan Devin Desktop's new folders

`install` already supports Devin Desktop and the Devin CLI, but `scan` reads only the older
Windsurf paths. Devin's own files live in `.devin/` (projects) and `~/.config/devin/` (home).

- **Where:** `agentprint/agents/windsurf.py` (its docstring lists what is not covered).
- **Done when:** Devin settings, MCP config and hooks are scanned (AP002 to AP005), its
  personal files are covered by AP007/AP008, and package rules cover `.devin/` files that must
  not ship.

## 8. Cline integration

A sweep already reads Cline in full (`agentprint/agents/cline.py`), including its plain-text
`secrets.json` (permission check only). Cline runs hook files named after the event:
`~/.cline/hooks/PreToolUse` for the CLI, `~/Documents/Cline/Hooks/PreToolUse` for the
extension (opt-in, "Enable Hooks"), and blocks on stdout `{"cancel": true}`.

- **Where:** a new `agentprint/integrations/cline.py`.
- **Done when:** `install --agent cline` writes the hook file and the MCP entry in
  `~/.cline/data/settings/cline_mcp_settings.json` with tests, and a missing or failing
  agentprint answers "allow".
