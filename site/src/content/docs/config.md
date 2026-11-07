---
title: Config and baseline
description: Switch agentprint on for a team without noise. A project config file, ignore rules, and a baseline of findings you have accepted.
---

A team that switches agentprint on usually has a few findings it has already looked at and accepted. A project config and a baseline let you say so once, in the repository, so every run after that shows only what is new.

New in v0.1.1.

## The config file

Put `.agentprint.toml` at the root of the project:

```toml
fail_on = "medium"                      # like --fail-on (default: high)
strict = true                           # like guard --strict
baseline = ".agentprint-baseline.json"  # the default name

[[ignore]]
check = "AP003"
path = ".devcontainer/**"
reason = "throwaway dev container, approve-everything on purpose"

[[ignore]]
check = "AP006"
path = "**/.cursor/mcp.json"
reason = "an example MCP config we ship on purpose, no keys in it"
```

Or put the same keys under `[tool.agentprint]` in `pyproject.toml`:

```toml
[tool.agentprint]
fail_on = "medium"
```

agentprint uses the first one it finds:

1. the file you name with `--config <path>`,
2. `.agentprint.toml` at the project root,
3. `[tool.agentprint]` in the project's `pyproject.toml`.

`--no-config` reads none of them, and no baseline either: every print counts. `scan`, `pack`, `fix` and `baseline` take `--config`, `--no-config` and `--baseline <file>`.

The project root is the folder you check: the path you give `scan` or `fix`, the package folder for `pack` (for an already-built file, the folder you run it in), and the folder an AI agent runs `npm publish` or `git push` in for the [tripwire](/agentprint/inside-your-agent/). The MCP server's tools read the config of the folder they check (for a built file, the folder the server runs in).

## The keys

| Key | What it does |
|---|---|
| `fail_on` | The lowest severity that fails a run: `low`, `medium`, `high`, `critical` or `none`. A `--fail-on` you type wins. The GitHub Action always passes its `fail-on` input (default `high`), so there the input wins. |
| `strict` | `true` makes the tripwire block a publish or push it cannot check, like `guard --strict`. It can only turn strict mode on: `AGENTPRINT_STRICT=1` and `--strict` still work when the file says `false`. |
| `ignore` | A list of rules. Each has a `check` (an id such as `AP003`), a `path`, or both, and a `reason`. A finding a rule matches is known. A rule with neither `check` nor `path` is skipped, because it would hide everything. |
| `baseline` | The baseline file, from the project root. Default: `.agentprint-baseline.json`. |

An ignore `path` is a glob over the file's path from the project root, with forward slashes. `*` and `?` stay inside one folder or file name. `**` as a whole folder name (`**/x`, `x/**`) crosses folders; inside a name it is just `*`. A path that ends in `/` means everything under that folder. A path longer than 512 characters or with more than 16 wildcards is skipped with a warning. `.mcp.json` matches only the file at the root; `**/.mcp.json` matches it in any folder. Inside a package, the path is the file's name in the package. An npm tarball puts everything under `package/`, so start package rules with `**/`.

Ignore rules only match the project's own files. They never hide a finding in your home folder, so a repository you clone cannot silence a sweep of your own agent files. The same goes for the project's baseline (below).

## Mistakes in the file

A config never stops a run.

- An unknown key is one warning that names it. The rest of the file still applies.
- A value of the wrong kind, such as `fail_on = "sometimes"`, is a warning, and that key keeps its default.
- A file that is not valid TOML gives `could not read config <path>: …` and the defaults.

Warnings go to stderr, so `--format json` output stays valid JSON. The tripwire adds them to its warning, and the MCP tools end their answer with `warning:` lines.

## The baseline

```bash
agentprint baseline              # accept today's findings in this project
agentprint baseline --check      # change nothing: list stale entries, count new findings
agentprint baseline --update     # add new findings, keep every old entry
agentprint baseline --prune      # drop entries whose finding is gone
```

`agentprint baseline` sweeps the project and writes every finding that an ignore rule does not already cover to `.agentprint-baseline.json`. Each entry gets the reason `accepted <date>`, which you can edit. When you run it again, entries that are still found keep their reason, and entries whose finding is gone are dropped.

`--check` changes nothing and exits `0`. It lists stale entries, whose finding no longer exists, so you can prune them. It also counts findings the baseline does not cover yet.

### Your home folder: a personal baseline

A project's baseline and its ignore rules only ever cover the project's own files. They are files in the repository, written by whoever wrote it, and a repository can't decide what counts in your home folder: a fingerprint is made of parts anyone can guess, so a cloned repository could otherwise list your `~/.claude/history.jsonl` key as accepted.

Your home folder's findings can only be accepted by your personal baseline, `~/.agentprint/baseline.json`. `agentprint baseline --include-home` writes your home findings there (with `--home <folder>`, into that folder's `.agentprint/baseline.json`) and the project's findings to the project's file, as usual. The personal file is private to you (mode `0600`), never in a repository, and `scan`, `fix` and the MCP `scan_secrets` tool read it whenever they sweep that home folder. A file you name with `--baseline` outside the project is yours too, so it can cover home findings as well. `--no-config` reads none of them.

### The baseline never stores your keys

An entry holds a check id, a path, a reason, a count and a fingerprint. The fingerprint is a sha256 hash of four things:

- the check id, such as `AP002`,
- the path from the project root (`~/…` for a home-folder file),
- the key rule's name, such as `Anthropic API key`, or the check's title for a finding that is not a key,
- for a key, its masked stub, the first four characters and the length (`sk-a…(len 105)`).

It is never the key, a hash of the key, or a hash of the line it sits on, so nothing in the file can give a key back. The line number is not part of it either, so a finding stays known when lines move.

Two findings with the same fingerprint, such as two keys of the same kind and length in one history file, share an entry with a count. If a third one turns up, it is new.

The baseline covers what `agentprint scan` finds. A package's findings are named by their place in the package, so for a file you ship on purpose, write an ignore rule instead.

## Known findings

A finding that the baseline or an ignore rule covers is known. Known findings:

- never fail a run or change the verdict,
- are counted in the summary, as in `no prints · 2 known · keys masked`,
- are shown apart. A person sees a dim `known` section with counts and reasons. An AI agent gets one `known:` line. JSON gets a `known` list with the check id, severity, path and reason, plus `known` in `summary`, and never the finding's text,
- still come with "rotate this key" when they are keys, for the AI agent to tell you (in JSON, as `ask_human`): accepting a key quiets the scan, but the key is still live until you rotate it,
- are left out of SARIF, so code scanning shows only what counts,
- are left alone by `agentprint fix`,
- are counted in `verdict.json` when you write a [badge](/agentprint/badge/), so a reader can see that some findings were accepted.

A config and a baseline are part of the repository, written by whoever wrote the repository. In one you cloned from someone else, a CLEAR means "clear, except what this repository accepted": read the `known` lines and their reasons, or run with `--no-config` to see everything.

## A key on its way out always blocks

A key that would ship in a package being published, or in a push, always blocks. That holds whatever the config or the baseline says: an ignore rule or a baseline entry does not cover it, and a `fail_on` in the config cannot let it through. This is true in `pack`, `fix --pack`, the tripwire and the MCP tools `check_package` and `check_push`.

Ignore rules and the baseline still apply to everything else there. A non-secret agent file you ship on purpose (AP006), or a source map in an open-source package (AP009), can be known.

Only a `--fail-on none` you type yourself on `pack` reports without failing, as it always has.

## Next

- [Use it in CI](/agentprint/ci/): the GitHub Action and pre-commit.
- [What agentprint checks](/agentprint/checks/): every check id you can name in a rule.
