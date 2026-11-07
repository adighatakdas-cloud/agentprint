# Contributing to agentprint

Thanks for helping. agentprint is small on purpose: one module per agent, one module per check.
Most contributions touch one new file plus one line in a registry. For the big picture, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first (five minutes). Ready-made tasks are in
[docs/good-first-issues.md](docs/good-first-issues.md).

## Set up and run the tests

You need [uv](https://docs.astral.sh/uv/). Node.js (for `npm`) is optional: without it, the
tests that run a real `npm pack` are skipped.

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
uv sync
uv run ruff format . && uv run ruff check . && uv run pytest -q -m "integration or not integration"
```

That last line is what CI runs, and it must pass before every commit. Plain `uv run pytest`
runs the fast unit tests only.

The live tests (`tests/live/`, `-m live`) start real agent CLIs against a mock model. They
download those CLIs, so they run in CI (`.github/workflows/live-agents.yml`); you don't need to
run them locally.

## Ground rules

1. **No real keys, ever.** Not in tests, fixtures, docs, issues or commit messages. Fake keys
   come from [`tests/fakes.py`](tests/fakes.py): they are assembled at runtime from pieces
   (`"sk-ant-" + "api03-" + "FAKE" * 23`), so no key-shaped text is committed and GitHub push
   protection never trips. Need a new key format? Add it there the same way.
2. **Never print a full secret.** Everything agentprint shows goes through `redact()` (first 4
   characters and the length). A test that checks output should also check that the full fake
   key is absent.
3. **Never use a key agentprint finds**, not even to check whether it works. Detection is by
   pattern only.
4. **No network during a scan, no telemetry.**
5. **Real scan results never enter the repo.** `scans/` is git-ignored. Don't paste output from
   your own machine into a test or an issue; build a fake one instead.
6. **Defensive only.** agentprint reads, reports and helps fix your own files.

## Fixtures

Tests build fake home folders and projects at test time ([`tests/conftest.py`](tests/conftest.py)),
in a temporary folder. Copy the **structure** of the agent's real files (file names, nesting,
key names) as you see them on a real install or in the agent's docs; make every **value**
synthetic. Note in the agent module where and when you checked the layout (for example
"checked on macOS on 2026-11-05, names only"). Never point a test at your real home folder.

## Add an agent to the scan (one file)

1. Create `agentprint/agents/<agent>.py` with a class that has:
   - `name` (the id shown in findings) and `package_rules` (files that must never ship in a
     package, each with a severity and a one-line reason);
   - `discover_home(home)`, decorated with `@from_home`, and `discover_project(root)`: yield
     each file with its kind (`HISTORY`, `SETTINGS`, `PERSONAL` or `CREDENTIALS`).
     Credential stores (where a key is meant to be) are never read, only checked for who can
     read them;
   - `read_config(artifact)`: the grants, hooks and MCP servers in a settings file. It must
     never raise; return an empty `AgentConfig()` for anything it doesn't understand.
     `agents/base.py` has shared helpers (`hook_commands`, `mcp_servers`, `is_blanket_command`);
   - `pure_history(path)`: True for a history file that is only a record (a transcript, chat
     log, paste cache or prompt history). `agentprint fix` masks keys only in these; a skill,
     rule, memory or scheduled job the agent reads back must return False. `matches_any` in
     `agents/base.py` checks a path against a list of patterns.
   Start the module with a docstring linking the docs you followed. `agents/gemini.py` is a
   good short example.
2. Add one line to `ALL_AGENTS` in `agentprint/agents/__init__.py`. Every check picks it up.
3. Add fake files for it in `tests/conftest.py` and a `tests/test_agent_<agent>.py` that checks
   which files are found, what `read_config` returns, and that a scan reports the planted keys.
4. Add the agent to `site/src/content/docs/agents.md` and `site/check-notes/ap003.md`, then run
   `uv run python -m tools.gen_docs` (it rewrites the site's agent data and landing text).

**Only package rules?** If agentprint doesn't read the agent's home folder yet, add one
`_agent(...)` entry to `agentprint/agents/scan_only.py` instead.

## Add an integration (install, hook, MCP)

1. Create `agentprint/integrations/<agent>.py` with an `INTEGRATION = Integration(...)`: its
   config folder, the edits `install` makes (hook, MCP server entry, instructions note),
   `parse_hook` (the agent's hook JSON → command and folder) and `reply` (a verdict → the
   agent's own "deny" output and exit code). `integrations/claude_code.py` is the reference.
2. Register it in `agentprint/integrations/__init__.py`.
3. Follow the agent's docs for exit codes and link them in the docstring. Two rules hold for
   every agent: **allow** is exit 0 with the agent's own "allow" output, and agentprint never
   answers "allow" in a way that would skip the user's own permission prompt.
4. Tests: install, re-install (no change), uninstall (byte for byte), and the hook replies.
   See `tests/test_install_new_agents.py` and `tests/test_hooks.py`.
5. If the agent has a command-line mode that runs without a login, add a live test:
   `tests/live/test_live_<agent>.py`, a pinned version in `tests/live/install_clis.py`, and a
   row in `.github/workflows/live-agents.yml`.

## Add a check

1. Create `agentprint/checks/apNNN_<what>.py` with an id, severity, title, a one-line `fix`,
   and `run(artifacts) -> list[Finding]`. Read agent settings through `agent_configs()`, never
   by parsing an agent's files in the check.
2. Register it in `agentprint/checks/__init__.py`.
3. Add an adviser in `agentprint/advice.py` (`@adviser("APNNN")`) that splits the fix into
   `fix_steps` (what an AI agent can do) and `ask_human` (what only a person can do).
4. Fixture-backed tests for what it finds and what it must not flag.
5. Write the check's page text in `site/check-notes/apNNN.md` (copy an existing note for the
   frontmatter), then run `uv run python -m tools.gen_docs`. That generates the check's page
   on the docs site and its row in the README's checks table. Without the note, `gen_docs`
   fails with `FileNotFoundError`.

Text taken from scanned files (paths, names, values) is untrusted: show it through
`sanitize.clean()` / `quote()`, and never put it into a shell command unless every character is
known safe (see AP008).

## Docs and generated files

The README is short; the user docs live on the docs site.

- User docs: `site/src/content/docs/` (Markdown). Check prose: `site/check-notes/`.
- Generated, never edit by hand: `site/src/content/docs/checks/`, the checks table between the
  `<!-- checks:start -->` markers in `README.md`, `site/src/data/`, and `docs/brand/` plus
  `site/src/assets/` logos.
- After changing a check, a check note, an agent, the scan or fix views, the demo world
  (`demo/build_demo.py`, which the landing terminal is drawn from) or `__version__`, run
  `uv run python -m tools.gen_docs`. After changing the mascot, run
  `uv run python -m tools.gen_brand`. Add `--check` to either to see whether files are current;
  CI fails on drift.
- Build the site: `cd site && corepack enable && pnpm install --frozen-lockfile && pnpm build &&
  pnpm run audit-dist`. Preview with `pnpm preview`.

## Commits and pull requests

- Work on a branch; `main` stays green.
- Small commits with conventional prefixes: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
- Third-party GitHub Actions are pinned to a full commit SHA with a version comment.
- Security problems in agentprint itself: please report them privately, as
  [SECURITY.md](SECURITY.md) explains, not in a public issue.
