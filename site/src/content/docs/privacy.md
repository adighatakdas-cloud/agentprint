---
title: Privacy
description: agentprint is offline, has no telemetry, masks every key it finds, and never uses one.
---

- **Offline.** `scan` makes no network calls. No telemetry, no account.
- **Keys are masked the moment they are found**, to their first 4 characters and length (`sk-a…(len 108)`), in every output format.
- **Found keys are never used.** They are not sent anywhere, not tested, not even to check whether they still work.
- **Credential stores are never read.** Files where a key belongs (`~/.codex/auth.json`, `~/.hermes/.env`, and others) are only checked for who can read them.
- **Downloads happen only when you start them:** `uvx` or `npx` fetching agentprint, and `pack` running your own `npm pack --dry-run` or `uv build`.

Chat databases (SQLite) are opened read-only, and at most 64 MB of text is read from each. Archives are read in memory and never unpacked to disk.

For how this is built, see [How it works](/agentprint/how-it-works/#safety-decisions).
