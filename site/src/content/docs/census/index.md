---
title: Census
sidebar:
  label: Take part
description: Help measure how often AI agent files and keys ship in new npm and PyPI packages, by scanning a small shard.
---

A census is a count: how many new npm and PyPI packages ship agent files, and how many of those hold keys. This page explains how to take part. Read the first round's results in the [pilot, November 2026](/agentprint/census/pilot-2026-11/).

AI coding agents (Claude Code, Codex, Cursor, Gemini CLI and others) write files into project
folders: settings, chat logs, instructions. Some of those files get published inside npm and
PyPI packages, sometimes with API keys in them. We want to know how often that happens in
recently published packages, and we want to tell affected maintainers before anyone else
finds the keys.

One machine scanning 100,000 packages is slow and heavy. Many volunteers each scanning a
small slice (a **shard**) is quick and light. This page explains how to take a shard.

## What the scan does

For each package in your shard, `ecoscan`:

1. asks the registry which version is current;
2. gets the package's **file list** (from jsDelivr for npm; for PyPI wheels it reads only the
   wheel's index with partial downloads);
3. downloads a package only when it needs to: npm tarballs only when an agent file shows up in
   the list, PyPI sdists (`.tar.gz`) always, because they have no index;
4. looks for agent files (`.claude/`, `.codex/`, `.cursor/`, `AGENTS.md`, `.mcp.json` and
   more) and checks those files, and only those, for strings shaped like API keys;
5. writes two outputs: a **public** summary (counts only) and a **private** findings file.

Nothing is installed or run. Packages are read in memory and never unpacked to disk. No
found key is ever used, tested, or sent anywhere. Every key-shaped string is cut to its
first 4 characters and its length (`sk-a…(len 105)`) the moment it is found.

The scan is polite by design. Request rates are fixed (at most 4 a second per site, 1.6 for
jsDelivr) and cannot be raised; it backs off when a site is busy and honors `Retry-After`.
Every request says who it is: `agentprint-research/0.1 (+https://github.com/adighatakdas-cloud/agentprint)`.

## What it costs you

A round of about 50,000 packages per registry is usually split into 10 shards each. One
shard is about 5,000 packages:

| Shard of ~5,000 | Download | Time |
|---|---|---|
| npm | about 1 GB | about 55 minutes |
| PyPI | about 3.7 GB | about 45 minutes |

These come from our November 2026 pilot with the default size caps. The scan prints its own
estimate for your shard before it starts. Time is set mostly by the fixed request rates, not
by your connection. You can run an npm and a PyPI shard at the same time.

Bandwidth controls:

- `--max-gb 2` stops after 2 GB. Run the same command again with a higher number to go on.
- By default, npm packages bigger than 150 MB and PyPI sdists bigger than 10 MB are skipped
  (they are a few percent of packages but most of the bytes). `--npm-max-mb 256` and
  `--sdist-max-mb 256` raise those limits. 256 is the most allowed.
- Ctrl-C stops at any time. Run the same command again and it carries on where it stopped.

## The rules

1. **Never use or test a key you find.** Not to "check if it works", not ever. Using
   someone else's credentials is wrong and in many places illegal.
2. **Never post findings publicly.** Not the private folder, not package names, not redacted
   values. Package names alone tell people where to look.
3. **Only post `public/summary.json`.** It holds counts only, and the tool checks that.
4. **Scan only your shard,** with the round's exact window, so nothing is scanned twice.
5. **Don't modify the tool to go faster.** The rates protect free services we all rely on.

Redaction is automatic: you never see a full key, and the tool never stores one.

## How to take part

### 1. Claim a shard

Each round has a GitHub issue labelled `scan-round` with the round's window and a checklist
of shards. Comment `claiming npm 3/10` (or whichever is free). The organizer ticks it off.

### 2. Run it

You need [git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/). Then copy the
command from the round issue, which looks like this:

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
uv sync
uv run python -m tools.ecoscan run npm --window 141824000:142000000 --shard 3/10 \
    --out scans/round-1/npm-3
```

`scans/` is ignored by git, so nothing from your scan can be committed by accident.

When it finishes it prints a summary and two paths:

```
PUBLIC  (counts only, safe to post on the scan-round issue): scans/round-1/npm-3/public/summary.json
PRIVATE (never post, not even redacted):                     scans/round-1/npm-3/private
```

### 3. Post the public summary

Comment on the round issue with your shard name and the contents of
`public/summary.json` (paste it in a ```` ```json ```` block, or attach the file). It only has
counts: how many packages, how many with agent files of each kind, how many with key-shaped
strings, bytes and time. The organizer adds all shards together with
`python -m tools.ecoscan merge`.

### 4. Send private findings privately

If the run found key-shaped strings, it says so and tells you where `private/findings.jsonl`
is. Send that file **privately**, through either:

- GitHub private vulnerability reporting:
  <https://github.com/adighatakdas-cloud/agentprint/security/advisories/new>
- or email: adi65das@gmail.com

Then delete it if you like. We check every finding by hand, without using the key, and tell
the package maintainers and the registry's security team first. Only counts are ever
published, and only after maintainers have had time to rotate their keys.

If there are no findings, there is nothing to send.

## For organizers: starting a round

1. Pick the bounds, for example for about 50,000 packages each:

   ```bash
   uv run python -m tools.ecoscan window npm  --packages 50000
   uv run python -m tools.ecoscan window pypi --packages 50000
   ```

   Each prints `--window FROM:TO` (the newest hour is left out so uploads have settled).
2. Open a new issue from the **Scan round** template, fill in both windows and the shard
   count, and post it.
3. Save each posted summary as a file, then add them up:

   ```bash
   uv run python -m tools.ecoscan merge round-1-summaries/*.json --out round-1-total.json
   ```

   `merge` refuses private files, summaries from another window, and a shard posted twice,
   and lists missing or unfinished shards.

How the scan works in detail (enumeration, file lists, what counts as an agent file):
[tools/ecoscan/README.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/tools/ecoscan/README.md).
