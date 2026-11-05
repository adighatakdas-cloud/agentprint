## What this changes

<!-- One or two sentences. Link the issue if there is one: "Fixes #123". -->

## Checklist

- [ ] `uv run ruff format . && uv run ruff check . && uv run pytest -q -m "integration or not integration"` passes.
- [ ] New behavior has a test built on fake files (no real home folder, no real keys).
- [ ] Any output that could include a secret goes through `redact()`, and the test checks the full fake key is absent.
- [ ] No network calls during a scan.
- [ ] Docs updated if a check, agent or command changed (`README.md`, `docs/supported-agents.md`).
