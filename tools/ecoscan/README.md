# ecoscan: methodology

`ecoscan` is the research tool behind agentprint's package-scan write-up. It answers one
question: **how many recently published npm and PyPI packages ship files written by AI
coding agents, and how many of those files contain strings shaped like API keys?**

This page describes how the scan works. It holds no results. To run a shard of a scan
round yourself, read [docs/VOLUNTEER_SCAN.md](../../docs/VOLUNTEER_SCAN.md).

## Ground rules

1. **Keys are never used.** Detection is pattern-based only. No key is sent anywhere,
   tested, or "confirmed". We say "key-shaped strings" or "keys matching real formats",
   never "live keys".
2. **Keys are never stored.** Every value is redacted at the moment it is found, to its
   first 4 characters and its length (`sk-a…(len 108)`). Output files, logs and the
   summary never hold a full value.
3. **Results stay private until disclosure is done.** Output goes to a git-ignored
   `scans/` folder. Affected maintainers and the registry security teams are told first and
   given time to rotate keys; only counts are published.
4. **Polite to every service.** See [Politeness](#politeness).
5. **Nothing is installed or run.** Archives are read in memory; no package code runs and
   nothing is unpacked to disk.

## 1. Finding recent packages

Checked against the live services on 2026-11-05.

### npm: the replication feed

`https://replicate.npmjs.com/registry/_changes` lists every change to the registry, one
entry per package at its latest change. npm replaced the old CouchDB endpoints on
29 May 2025 ([announcement](https://github.blog/changelog/2025-02-27-changes-and-deprecation-notice-for-npm-replication-apis/),
[migration thread](https://github.com/orgs/community/discussions/152515)). What works today:

| Request | Result |
|---|---|
| `GET /registry/` | `update_seq` (the newest change number) |
| `_changes?since=N&limit=10000` | up to 10,000 entries after `N`, oldest first: `seq`, `id` (package name), `deleted` |
| `limit=20000` | `400 Bad Request` (10,000 is the maximum) |
| `include_docs=true` | `400 Bad Request` (documents are no longer included) |
| `descending=true&since=N` | newest first, but `since` is ignored, so it cannot page backwards |

So ecoscan reads forward from `update_seq - window`, keeps live (not deleted) packages, and
widens the window until it has enough. In November 2026 the feed moved about 15,000 change
numbers an hour, and 10,000 unique packages spanned about 36,000 change numbers.

**Which version.** The feed has package names, not versions. ecoscan scans the `latest`
dist-tag (`GET registry.npmjs.org/<name>/latest`, about 1.3 KB), which is what
`npm install <name>` gets. The alternative, the package document with publish times, costs a
median 24 KB (abbreviated form: 15 KB, and it has no times), with a long tail into megabytes.
In a 37-package sample, `latest` was the most recently published version for 29.

**Skip the newest hour.** ecoscan leaves out the newest 15,000 change numbers. In a test run
on the very newest entries, jsDelivr did not know 9 of 20 versions yet, and npm had already
deleted 3 tarballs (spam is removed fast). A one-hour lag avoids both.

### PyPI: the changelog

PyPI's [docs](https://docs.pypi.org/api/) recommend RSS for new releases, but the feeds are
short: `rss/updates.xml` holds the last 100 releases and `rss/packages.xml` the last 40 new
projects. ecoscan uses the changelog instead, which PyPI still supports for mirrors even though
the rest of XML-RPC is being retired ([XML-RPC docs](https://warehouse.pypa.io/api-reference/xml-rpc/)):

- `changelog_last_serial()` returns the newest event number.
- `changelog_since_serial(n)` returns every event after `n`: `(name, version, time, action, serial)`.
  A 20,000-event request took about 3 seconds and covered about 10 hours, with about
  4,500 `new release` events.

ecoscan keeps the newest `new release` per project (names compared after PEP 503
normalization), drops releases removed again in the window (`remove release`,
`remove project`), and skips the newest 2,000 events (about an hour) so file uploads have
finished.

If XML-RPC goes away: the PEP 691 JSON index (`GET /simple/` with
`Accept: application/vnd.pypi.simple.v1+json`, about 10 MB gzipped) has `_last-serial` for
every project, which gives the same "recently changed" order.

## 2. Getting each package's file list cheaply

The goal is to download file *contents* only when an agent-file path is present.

### npm

| Method | Cost per package (37-package sample) | Notes |
|---|---|---|
| Registry tarball (`dist.tarball`) | median 176 KB, mean 1.4 MB | ground truth, but heavy |
| jsDelivr file list: `data.jsdelivr.com/v1/packages/npm/<name>@<version>?structure=flat` | median under 1 KB | lists every file, dotfiles included |

In a 39-package check the jsDelivr list matched the tarball's file list exactly for all 38
that jsDelivr could serve (232 KB of lists against 62 MB of tarballs). So ecoscan:

1. gets the jsDelivr list;
2. downloads the tarball **only** if the list shows a candidate path, or if jsDelivr fails
   (it does not serve packages over 150 MB, and may not know a minutes-old version yet);
3. downloads the tarball for a fixed 5% validation sample as well (chosen by CRC32 of the
   package name), and records whether the two lists agree.

jsDelivr's API has no hard rate limit but asks heavy users to stay under 100 requests a
minute or get in touch first ([README](https://github.com/jsdelivr/data.jsdelivr.com)).
ecoscan sends at most 1.6 requests a second (96 a minute) to it.

### PyPI

`GET pypi.org/pypi/<name>/<version>/json` (median about 6 KB) lists the release's files with
their sizes. ecoscan reads the **sdist** and the **smallest wheel**. The wheels of one release
are built from the same source tree, so one stands in for the rest.

- **Wheels (and .zip sdists)** keep a directory of all names at the end of the file.
  ecoscan reads them with HTTP Range requests through a seekable remote-file view:
  first the last 64 KB, then, only if needed, the rest of the directory, then only the bytes
  of candidate files. `files.pythonhosted.org` answers Range requests with
  `206 Partial Content`. Files smaller than 64 KB answer `416` to a suffix range, so those are
  fetched whole. In a 30-release sample the last 64 KB held the whole directory for 21 wheels,
  and reading them cost 1.8 MB, against 49 MB to download the same wheels.
- **.tar.gz sdists** have no directory: names are spread through a compressed stream, so the
  sdist is downloaded whole (sample: median 414 KB, mean 1.3 MB) and read in memory. This is
  where most of the bandwidth goes, and it is also where agent files usually are: an sdist
  carries the project root, where `.claude/` and friends live.

## 3. What counts as an agent file

Two layers, both on file **paths** only:

- **AP006**: the path matches one of agentprint's own package rules (the same rules
  `agentprint pack` uses), for example `.claude/settings.local.json`,
  `.claude/**/*.jsonl`, `.claude.json`, `CLAUDE.local.md`, `.codex/sessions/`,
  `.codex/config.toml`.
- **Candidates**: a wider net, so results can be re-checked as agentprint adds agents:

| Agent | Paths |
|---|---|
| Claude Code | `.claude/`, `.claude.json`, `CLAUDE.md`, `CLAUDE.local.md` |
| MCP config | `.mcp.json` |
| Codex | `.codex/` |
| AGENTS.md | `AGENTS.md` |
| Cursor | `.cursor/`, `.cursorrules` |
| Gemini CLI | `.gemini/`, `GEMINI.md` |
| Windsurf | `.windsurf/`, `.codeium/`, `.windsurfrules` |
| OpenCode | `.opencode/`, `opencode.json(c)` |
| Aider | `.aider*` |
| Continue | `.continue/` |
| Cline / Roo / Kiro / Amazon Q / Junie | `.clinerules`, `.cline/`, `.roo/`, `.roomodes`, `.kiro/`, `.amazonq/`, `.junie/` |
| Copilot / Goose | `.github/copilot-instructions.md`, `.goosehints` |

Each candidate is labelled with a **kind**: `history` (chat logs, sessions, input history),
`settings` (settings, config, MCP, auth files), `instructions` (Markdown rules and prompts)
or `other`. Instruction files are often published on purpose. They are counted separately
and are not called leaks.

## 4. Key-shaped strings

Candidate files are scanned with agentprint's own patterns (`agentprint/secrets.py`):
Anthropic, OpenAI, GitHub, AWS access key IDs, Stripe live keys, Slack, Google API keys, and
a generic "secret-looking name = random-looking value" rule with an entropy check. Each hit
is stored as rule id, file, first line, redacted value and how often the same value appears
in that file. Generic-rule hits are reported separately, because they include more false
positives. Every hit is checked by hand before any disclosure.

## Politeness

- `User-Agent: agentprint-research/0.1 (+https://github.com/adighatakdas-cloud/agentprint)` on every request: the repo is the contact point.
- Fixed request rates, shared by all worker threads: 4 a second per host, 1.6 for jsDelivr,
  1 for the npm feed (never above 5). There is no option to raise them.
- `429` and `5xx` replies and network errors are retried with exponential backoff (1, 2, 4 …
  seconds plus jitter, up to 5 retries). `Retry-After` (seconds or a date) is honored, and
  it pauses **every** thread's requests to that host, not just the one that got it.
- Timeouts on every connection; keep-alive connections; gzip for JSON.
- Size caps: 32 MB per JSON reply and 64 MB of Range reads per zip. By default, npm packages
  that unpack to more than 150 MB are skipped (jsDelivr does not list them either) and PyPI
  sdists over 10 MB are not downloaded: in the pilot these were a few percent of packages
  but most of the bandwidth. `--npm-max-mb` and `--sdist-max-mb` raise them, up to 256 MB.
  `--max-gb` stops a run at a download budget.
- Archives are untrusted input. agentprint's reader caps entries per archive (100,000) and
  the unpacked size of a tar (2 GB), caps the content it reads, never follows links, and
  never writes to disk.

## Output

Everything goes to the folder given with `--out` (keep it under the git-ignored `scans/`),
split into two parts that are never mixed:

| File | Contents |
|---|---|
| `public/summary.json` | **PUBLIC.** Counts only: no package names, versions, paths or values. Safe to post. |
| `private/findings.jsonl` | **PRIVATE.** One line per key-shaped string: package, version, path, line, rule, redacted value. Sent privately, never posted. |
| `private/targets-<eco>.jsonl` | the package list, fetched once; a resumed run reuses it |
| `private/meta-<eco>.json` | the round (window, shard) and what fetching the list cost |
| `private/results-<eco>.jsonl` | one line per finished package: status, method, file count, candidate paths, redacted findings, bytes, requests, seconds |
| `private/sessions-<eco>.jsonl` | start, end and package count of each run |

Statuses: `ok`; `partial` (one of a release's archives could not be read, the rest was);
`skipped` (over a size cap); `gone` (removed from the registry); `error`.

A run can be stopped at any time (Ctrl-C) and started again with the same command. It picks
up where it stopped; a half-written last line is ignored. A folder belongs to one round and
shard: resuming it with another `--window` or `--shard` is refused.

## Rounds and shards

A **round** is a fixed stretch of registry history, the same for everyone in it:

- npm: `--window FROM:TO` is a range of replication-feed change numbers; the round holds every
  live package whose latest change is in it. (A package changed again after `TO` moves out of
  the window and is picked up by the next round.)
- PyPI: `--window FROM:TO` is a range of changelog serials; the round holds the newest release
  of every project released in it. The changelog never changes, so every volunteer gets the
  same list.

`--shard K/N` splits a round between N volunteers: a package belongs to shard K when the
SHA-256 hash of its name (PEP 503-normalized for PyPI) modulo N is K - 1. The shards never
overlap and together cover the whole round. `python -m tools.ecoscan window npm|pypi` prints
current bounds for a new round, and `merge` adds the shards' public summaries together.

## Running it

```bash
uv run python -m tools.ecoscan window npm --packages 50000      # organizer: pick the bounds
uv run python -m tools.ecoscan run npm --window FROM:TO --shard 3/10 --out scans/round-1/npm-3
uv run python -m tools.ecoscan summary --out scans/round-1/npm-3  # rebuild both outputs
uv run python -m tools.ecoscan merge round-1-summaries/*.json --out round-1-total.json
```

The November 2026 pilot used `--limit 2000` (the newest packages, newest hour left out)
instead of a window. npm and PyPI use different hosts, so the two can run at the same time.

## Known limits

- npm: the `latest` tag is scanned, not every version, so a leak only in a prerelease is missed.
- PyPI: one wheel per release; a leak in only one platform's wheel would be missed.
- npm windows are read at scan time, so volunteers who start days apart can see slightly
  different lists (packages republished in between move out of the window).
- jsDelivr's lists are trusted for packages with no candidate path. The validation sample
  measures how far that trust holds.
- Files bigger than the size caps are not downloaded; they are counted as `too_large`.
- Paths only show that an agent file was published. Whether a key-shaped string is a real
  key is decided by hand, without using it.
