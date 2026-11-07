---
title: Fix what it finds
description: agentprint fix masks leaked keys, adds ignore lines and locks agent files after showing you the plan. What it changes, what stays with you, how to undo it.
---

`agentprint fix` makes the fixes that are safe to automate, then checks again. What only you can do, mostly rotating a key with its provider, it lists for you.

```bash
uvx agentprint fix
```

It starts with a sweep: a scan of your home folder and the current folder, the same as `agentprint scan`. Each problem it finds is a print, something an AI coding agent left behind. Then `fix` shows you its plan and asks `Apply 3 fixes? [y/N]`. Nothing changes until you answer `y`. Afterwards it sweeps again and ends on a verdict: **CLEAR** when no print at or above `--fail-on` is left, **BLOCK** when one is. The [glossary](/agentprint/glossary/) has these words.

## What it changes

Each line of the plan starts with what it will do: `mask`, `env`, `ignore` or `lock`.

| Print | What `fix` does | Undo |
|---|---|---|
| [AP001](/agentprint/checks/ap001/): a key in an agent's transcript, chat log, paste cache, prompt history or backup of an edited file | `mask`: masks the key where it is | No |
| [AP002](/agentprint/checks/ap002/): a key inside a permission entry, such as a pasted command in `permissions.allow` | `mask`: masks the key where it is | No |
| [AP005](/agentprint/checks/ap005/): a key written into a Claude Code MCP server's `env` | `env`: replaces the value with `"${NAME}"`, so Claude Code reads the key from your shell | No |
| [AP006](/agentprint/checks/ap006/): an agent file would ship in your npm package (with `--pack`) | `ignore`: adds a line naming that file to `.npmignore` and to `.gitignore` | Yes |
| [AP007](/agentprint/checks/ap007/): a personal agent file is not in `.gitignore` | `ignore`: adds it to `.gitignore` | Yes |
| [AP008](/agentprint/checks/ap008/): other users on this machine can read agent files | `lock`: makes exactly those files private to you | Yes |

`mask` and `env` can't be undone: the key is gone, and agentprint never kept a copy. `lock` is `chmod go-rwx` on exactly the files the check found.

On Windows, `lock` is one `icacls` call per file: `icacls "<file>" /inheritance:r /grant:r *<your SID>:F *S-1-5-18:F *S-1-5-32-544:F`, plus `/remove:g` for any other account the file names itself. Other users lose their access, and you, SYSTEM and Administrators get full control. agentprint runs only Windows' own `icacls.exe` from `System32`, never one found in the current folder or on `PATH`. It then reads the access list again. If anyone else can still read the file, or you could no longer read and write it, it puts the old list back and tells you. A file owned by another account is left as it is, and so is a file on a drive that keeps no permissions (FAT, exFAT): `fix` says it cannot make it private on that drive. A file owned by Administrators (made by a program run as administrator) is locked only when agentprint runs as administrator too.

A masked key keeps its first four characters, and every character after them becomes `*`. The line keeps its length, so nothing else in the file moves. In agentprint's output a key is always shown masked, as its first four characters and its length: `sk-a…(len 105)`.

An `ignore` line names just the file, from the project root: `/.claude/settings.local.json`. It never names a whole folder such as `.claude/` or `.github/`, because those also hold files you commit and ship on purpose, like workflows and shared agent settings. The one exception is a folder that holds nothing but agent data, such as `.codex/sessions/`: then the line names that folder.

If your npm package has no `.npmignore` yet, `fix` starts one as a copy of `.gitignore`: once `.npmignore` exists, npm stops reading `.gitignore`. If `package.json` has a `files` list that includes the agent file, `fix` adds the line to `.gitignore` only, and the `files` list is yours to change.

### What stays with you

`fix` lists these under **Needs you**, each with the exact steps:

- **Rotate every key it found.** Revoke it with its provider and make a new one. A masked key still counts as exposed: it sat in a plain file, so treat it as public. Neither agentprint nor your AI agent can rotate a key for you.
- **Set the variable.** After `env`, set `NAME` (for example `GITHUB_TOKEN`) to the new key in the shell that starts Claude Code.
- **Agent permissions and hooks** ([AP003](/agentprint/checks/ap003/), [AP004](/agentprint/checks/ap004/)). They change how your agent behaves, so you decide.
- **A key your agent uses as a setting**, such as an `env` block or an `apiKey` field. Masking it would break the agent, so move it to an environment variable yourself.
- **A key in a file your agent reads back**: a skill, a rule, a memory or a scheduled job, such as Hermes skills and cron jobs, Windsurf rules, Antigravity knowledge or Claude Code's auto memory. The key is in use there, and masking it would break what uses it. `fix` masks only history that is a record and nothing more: transcripts, chat logs, paste caches, prompt history and backups of files the agent edited.
- **A key in a chat database** (Cursor, OpenCode). Delete that chat in the app.
- **An MCP key in another agent's config**, or in a header, an argument or a URL.
- **A PyPI package.** Build tools differ, so you get the exclude steps.
- **A file that is already committed.** You get the `git rm --cached` command: `fix` never rewrites git history.
- **A `files` list in `package.json`** that includes the agent file: `fix` never edits `package.json`.
- **What lets an AI rebuild a closed-source package** ([AP009](/agentprint/checks/ap009/)): source maps, original source, AI instruction files. `fix` never edits your build settings. An open-source package's source maps, and a package for a private registry, are only low notes: `fix` lists them under **Notes**, with nothing for you to do.

## How it keeps you safe

- **Plan first.** `fix` changes nothing until you answer `y`, or pass `--yes`. In an AI agent, a pipe or CI it never asks: without `--yes` it shows the plan, changes nothing and says to re-run with `--yes`.
- **No copy of a key.** A file that held a key is never backed up. The plan, the journal and the output show masked keys only.
- **A running agent's writes are kept.** The AI agent that runs `fix` may be writing to its own transcript at that very moment. To mask a key, agentprint reads its bytes again, checks they are still the key, and writes over exactly those bytes. The file is never replaced or cut short, so anything the agent adds meanwhile stays.
- **It refuses if a file changed.** If a file changed or was deleted between the plan and the fix, that one change is skipped and reported ("the file changed while fixing; run agentprint fix again"). The others go ahead. Its own edits don't count: when one file gets both `mask` and `env`, the `env` change checks the file as the masks left it.
- **A journal of every change.** Each run that changes something writes `~/.agentprint/fix/<time>.json`, readable only by you: the file, the kind of change, the line, the masked key and the old file mode (on Windows, the old access list, as SDDL text). A run that changed nothing leaves no journal. Before it edits an ignore file, `fix` copies it to `~/.agentprint/fix/backups/`. Ignore files hold no keys.

### Undo

```bash
agentprint fix --undo
```

This puts back the `ignore` and `lock` changes of the last run: ignore files come back from their copies, an ignore file that `fix` created is removed, and files get their old modes (on Windows, their old access lists). It can't undo `mask` or `env`: agentprint never kept the key, so there is nothing to put back, and it tells you which changes those were. It leaves alone an ignore file you edited after the fix, or a mode or access list you changed again, and says so. With no journal, it says there is nothing to undo.

### One limit

To point a Claude Code MCP server at `${NAME}`, `fix` checks that the config file (usually `~/.claude.json`) has not changed, then replaces it in one step. Claude Code saves to that file too, and a change it saves in that same instant can be overwritten. This is rare: the gap is a few milliseconds. Running `agentprint fix` again checks the file afresh and fixes anything left.

## Inside your AI agent

Here is one agent turn, from a blocked publish to an allowed one:

![A simulated AI agent tries to run npm publish. The real agentprint hook blocks it and hands the agent its brief with masked keys. The agent runs agentprint fix --pack in the package folder and agentprint fix for the machine, retries, and is allowed through. It then names the keys the human has to rotate.](/agentprint/agent-loop.gif)

*The agent is simulated: its own lines are dim and start with `agent:`. Everything else is real agentprint output. The demo uses fake keys.*

Your AI agent runs it like this:

```bash
uvx agentprint fix --yes --format agent
```

The note that `agentprint install` adds to each agent's instructions tells it what to run when agentprint reports BLOCK: after a blocked publish, `uvx agentprint fix --pack --yes --format agent` in the package folder; for any other BLOCK, the command above. The agent gets a brief: the verdict after fixing, what was fixed, and what to ask you. Shortened:

```text
agentprint fix: CLEAR — 3 fixed, 5 for the human
fixed:
  - mask AP002 "/Users/you/code/my-app/.claude/settings.local.json" line 4: key "sk-p…(len 56)" masked in place
  - env AP005 "/Users/you/.claude.json" line 10: env.GITHUB_TOKEN of MCP server "github" set to "${GITHUB_TOKEN}"
  - mask AP001 "/Users/you/.claude/history.jsonl" line 1: key "sk-a…(len 105)" masked in place
ask_human:
  - AP001: Rotate the Anthropic API key that starts "sk-a…(len 105)": revoke it and create a new one at ...
  ...
Tell the human each ask_human item: these are theirs to do.
```

Without `--yes`, the agent gets the plan and the words `re-run with --yes to apply`, and nothing changes. See [Inside your AI agent](/agentprint/inside-your-agent/).

## Options

```bash
agentprint fix --dry-run              # show the plan, change nothing
agentprint fix --only AP001,AP008     # only these checks
agentprint fix --pack .               # what a preflight of this package finds
agentprint fix --no-home .            # only this project, not your home folder
agentprint fix --format json          # also: table, agent
```

- `--dry-run` shows the plan and changes nothing, like answering `n`.
- `--only` narrows the plan to some checks. The verdict still covers every print: fixing only file modes never says CLEAR while a key is still in a file.
- `--pack` works on what `agentprint pack` finds: a preflight, a check of what your npm or PyPI package would publish. For npm it adds the ignore lines, and masks a key inside a permission entry in the project file the package is built from. It never edits a built archive. For PyPI you get the steps.
- `--home`, `--no-home` and a folder work as in `agentprint scan`. `--fail-on` sets the lowest severity that counts (default `high`).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Nothing at or above `--fail-on` is left after fixing. `--undo` always exits `0`. |
| `1` | Something at or above `--fail-on` is still there, also when the plan was not applied. |
| `2` | Usage error. |
| `130` | You pressed Ctrl-C or Ctrl-D at the question. Nothing changed. |
