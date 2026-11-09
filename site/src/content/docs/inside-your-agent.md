---
title: Inside your AI agent
description: What agentprint install adds to each AI coding agent, what the agent reads when a publish would leak, and how to undo it.
---

`agentprint install` finds the AI coding agents on your computer and arms each one: it adds three things.

- **A hook** that runs before the agent's shell commands. Only package publishes (`npm`, `pnpm`, `yarn` and `bun publish`, `uv publish`, `twine upload`, changesets, lerna, and so on) and `git push` are checked. Everything else runs untouched.
- **An MCP server** (`agentprint mcp`) with the tools `check_package`, `check_push`, `scan_secrets` and `explain_finding`, so the agent can check on its own.
- **A short note** in the agent's global instructions: check before publishing or pushing, and never print a key.

Together, the hook and the check it runs are the tripwire. See [What `install` changes](/agentprint/agents/#what-install-changes) for the exact file each agent gets.

Rather add agentprint through your agent's own plugin system? Most agents have an agentprint plugin with the same three things: see [Plugins and extensions](/agentprint/plugins/). Use the plugin or `install` for an agent, not both.

## What the agent reads

Under a minute, start to finish:

![A simulated AI agent tries to run npm publish. The real agentprint hook blocks it and hands the agent its brief with masked keys. The agent runs agentprint fix --pack in the package folder and agentprint fix for the machine, retries, and is allowed through. It then names the keys the human has to rotate.](/agentprint/agent-loop.gif)

*The agent is simulated: its own lines are dim and start with `agent:`. Everything else is real agentprint output. The demo uses fake keys.*

When a publish would leak, the agent gets a brief: the verdict first, then fix steps it can do itself, then what only a person can do. This is the brief, shortened:

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

The key appears masked: the first four characters and the length. A full key is never printed.

## Install shows its changes first

Before it writes anything, `install` prints the lines it adds to each file. It backs the file up in `~/.agentprint/backups`. Running it twice changes nothing.

Use `agentprint install --dry-run` to see the changes and write nothing.

**To undo it:** `agentprint uninstall` takes out only what agentprint added. Add `--agent codex` to disarm one agent. A file you have not touched since comes back byte for byte.

## Strict mode: block when it cannot check

By default a hook never gets in your way: if agentprint hits an internal error, or cannot tell what a publish or push would upload ("could not check"), the command runs with a note. If you would rather stop than risk it, turn on strict mode. The same cases then block, with the reason in your agent's own deny message.

- `agentprint install --strict` writes hooks that pass `--strict`. This is the reliable way to turn it on. Run a plain `agentprint install` to go back. If a permanent agentprint older than 0.1.1 is on your PATH, its hooks would ignore `--strict`, so `install --strict` has the hooks run this version through `uvx` instead, and says so.
- Switching strict mode on or off changes the hook command, so **Codex** and **Hermes Agent** see a new hook and keep it off until you approve it again: type `/hooks` in Codex, and accept it in a foreground `hermes chat`.
- `AGENTPRINT_STRICT=1` (or `agentprint guard --strict` in a hook you wrote yourself) works only where the agent passes its environment on to hooks. Gemini CLI runs hooks with a cleaned-up environment, so the variable does not reach agentprint there. Use `install --strict`.

What strict mode does and does not cover:

- A publish or push that agentprint cannot check is blocked. That includes a command agentprint cannot parse when it looks like a publish or push. An ordinary command it cannot parse still runs, with a note.
- If agentprint fails to start at all (the launcher is missing, `uvx` is unavailable, or it crashes before `guard` runs), the command still runs.
- An unknown agent name still runs.
- It is still limited to what agentprint can see: a secret assembled at run time, or one inside an arbitrary script, is not detected.

Blocks to expect in strict mode, and what to do:

- **Build and publish in one command** (`uv build && uv publish`) is always blocked: when the hook runs, nothing is built yet, so there is nothing to check. Run the build and the publish as two commands.
- **A push from a folder known only when the command runs** is blocked: `cd "$(git rev-parse --show-toplevel)" && git push`, `cd $REPO && git push`, or a loop such as `for r in a b; do (cd $r && git push); done`. Push from the repo folder itself: run `git push` there, or write the folder out (`cd my-repo && git push`), one repo per command.
- **A workspace publish of more than 30 packages** (`pnpm -r publish` in a large pnpm workspace) is blocked: checking each package would outlast the agent's hook time limit. Publish the packages in smaller groups.
- **A script that names a publish agentprint cannot check** is blocked: a release script in another language (`python scripts/release.py` running `twine upload`), a script over 64 KB that names `npm publish`, or a publish that only the one-level-further scan found. A release script agentprint cannot open is blocked too. Run the publish or push as its own command, so agentprint can check it.

## A second layer: `agentprint protect`

The agent hook reads the command before the agent runs it. Reading a command can be dodged: `CMD='git push'; eval "$CMD"`, or a push built while a `python -c` script runs. The hook then sees no push. Strict mode blocks some of these, not all.

`agentprint protect` adds a check at git and npm themselves, for one project. It fires however the push or publish was started.

```bash
agentprint protect            # this folder; or: agentprint protect path/to/project
agentprint protect --strict   # block, not allow, when agentprint is missing or cannot check
agentprint protect --undo     # take it out again
```

| | Agent hook (`agentprint install`) | `agentprint protect` |
|---|---|---|
| Where it runs | In the agent, before a shell command | In git (`pre-push`) and npm (`prepublishOnly`) |
| Catches | A push or publish it can read in the command, scripts and recipes | Any `git push` or `npm publish` in this project, hidden or not |
| Misses | Commands hidden from it (`eval`, one built at run time) | `git push --no-verify`, `npm publish --ignore-scripts`, PyPI uploads |
| Scope | Every project the agent works in | One project, opt-in |

Use both. Each covers what the other misses.

What `protect` adds:

- **A git pre-push hook** in the repository's hooks folder (the one `git rev-parse --git-path hooks` names, so worktrees work too). Before each push it runs `agentprint push-check`. If a key or an agent file would go out, the push stops with the same brief the agent hook gives. A clean push prints nothing. Deleting a remote branch is never blocked. The hook lives in `.git/hooks`, which git never shares: it is local to your clone, and teammates and CI are not changed.
- **A pre-push hook you already had** is kept, byte for byte, as `pre-push.agentprint-chained`. It runs first with the same arguments and input, and either hook failing stops the push. A hook that would break when renamed is left alone instead: one written by husky, lefthook, pre-commit or overcommit, or one that reads its own name (`$0`, `${BASH_SOURCE[0]}`, `sys.argv[0]`, `__filename`). `protect` then prints what to add to that manager.
- **If `core.hooksPath` points inside the repository** (husky, lefthook, a tracked folder), agentprint writes nothing there. It prints the lines to add to your hook manager instead. If it points to a folder outside the repository, other repositories may use that folder too, so agentprint writes there only with `agentprint protect --global`, and only to a folder you own.
- **If agentprint is missing, fails, or takes more than 60 seconds** when you push, the hook prints one line and lets the push go. `AGENTPRINT_HOOK_TIMEOUT` changes the 60. With `--strict` (or `AGENTPRINT_STRICT=1`) it blocks instead. The time limit is tested on macOS and Linux; on Windows it is written for Git for Windows' `sh` but not yet tested there.
- **If the folder has a `package.json`**, its `scripts.prepublishOnly` runs `npx --yes agentprint-cli@<version> pack --fail-on high --if-unavailable allow` first. A `prepublishOnly` you already had stays and runs first (`yours && npx ...`). Running `protect` again after an upgrade moves the pin to the new version. `pack` lists the package with `npm pack --dry-run --ignore-scripts`, so it never starts itself again. The wrapper never takes `uvx` or `pipx` from `node_modules/.bin`.
- **On Windows**, Git for Windows runs hooks with its own `sh`, so the same hook works there.

**What you commit is shared.** `package.json`, and a hook manager's files (`.husky/pre-push`, `lefthook.yml`, `.pre-commit-config.yaml`), are usually committed. Once they are, the check runs for every teammate and in CI too, where uv may not be installed. So by default these fail open when agentprint itself can't run: no uv or pipx, uvx can't fetch agentprint, or it crashes before giving a verdict. The publish or push then goes on with one line. Anything agentprint did find still blocks, and so does a package it can't check (say a `pyproject.toml` that doesn't build). With `--strict` they block in every case. The one thing that still stops a publish is npx failing to fetch `agentprint-cli` itself, which needs the same registry the publish does.

With `--if-unavailable allow`, `agentprint pack` exits 3 for a leak and 4 for a package it can't check (instead of 1 and 2), so the npm wrapper can tell its verdict from uv or Python failing to start it. Each ecosystem in a folder is checked on its own, so a broken `pyproject.toml` never hides what the npm package would leak. Inside an npm `prepublishOnly`, `pack` checks only the npm package, since that is what npm is publishing.

`agentprint status`, and `agentprint scan` inside an agent, add one line in a protected repository: `protected (agentprint protect): git pre-push hook, npm prepublishOnly`.

**Which tools run `prepublishOnly`** (from each tool's own docs):

| Tool | Runs `prepublishOnly` on publish? | Skipped by |
|---|---|---|
| npm (`npm publish`) | Yes, first, "ONLY on `npm publish`" ([scripts](https://docs.npmjs.com/cli/v11/using-npm/scripts)) | `--ignore-scripts`: "npm does not run scripts specified in package.json files" ([config](https://docs.npmjs.com/cli/v11/using-npm/config)) |
| pnpm (`pnpm publish`) | Yes, first in its lifecycle list ([publish](https://pnpm.io/cli/publish)) | `--ignore-scripts`: "Don't run the publish lifecycle scripts" |
| Yarn classic (`yarn publish`) | Yes, after `prepublish` and `prepare`. Its docs don't list this; its source does ([publish.js](https://github.com/yarnpkg/yarn/blob/master/src/cli/commands/publish.js)) | `--ignore-scripts` ([install](https://classic.yarnpkg.com/en/docs/cli/install)) |
| Yarn 2+ (`yarn npm publish`) | **No.** It runs `prepublish`, not `prepublishOnly` ([lifecycle scripts](https://yarnpkg.com/advanced/lifecycle-scripts)) | (does not run) |
| Bun (`bun publish`) | Yes, when Bun packs the package itself. Not when you publish a tarball ([publish](https://bun.sh/docs/cli/publish)) | `--ignore-scripts` |

So with Yarn 2+, or with `--ignore-scripts`, only the agent hook checks the publish. `protect` says so when it sees Yarn 2+.

npm runs `prepublishOnly` before `prepack` and `prepare`, and `pack` lists the package without running any script. Files those scripts would build (a `dist/` folder made in `prepare`, say) are checked only if they are already there from an earlier build. To check exactly what goes out, build first, or check the built tarball in CI with `agentprint pack <file.tgz>`.

**PyPI has no publish hook.** `twine upload` and `uv publish` run no script before they upload, so `protect` cannot add one. For Python packages the agent hook is the check, plus `agentprint pack dist/<file>` on the built package, by hand or in CI (see [Use it in CI](/agentprint/ci/)).

What the push check reads: what the push uploads, from git's own objects, never your working files. That is the agent files and agent settings files in the tree each pushed branch or tag points at, and in every commit on the way that the remote you push to doesn't have yet, merges included. Lockfiles, READMEs and other files no check reads are never read. "Doesn't have yet" means: not the remote's old commit for that branch, and not on this clone's remote-tracking branches for that remote (`origin/*`). What another remote has doesn't count, so pushing a private repository to a new public remote reads its whole history. A push to a URL leaves out only the remote's old commit.

A key added in one of those commits and deleted in a later one still goes out, so it still blocks. The brief then says to rewrite the commits that are not pushed yet (for example `git reset --soft` to the last pushed commit, then commit again without the key). Commits the remote already has are not read again: a key in those is already out, so rotate it. A push that is already up to date uploads nothing and goes quietly.

What the check could not read never cancels what it found: a key found anywhere blocks the push. If nothing was found but part of the push could not be read, the first line says so ("could not check all of this push"), never OK, and strict mode blocks. That happens for a tag on a single file (agentprint can't tell which file it is), for more than 10,000 commits in one push, and for more agent and settings files than it reads at once (64 MB).

Limits:

- **pre-commit framework.** pre-commit keeps git's input and names only one ref of a push in its own variables, which `push-check` reads. A push of several branches at once is checked for that one ref only. The snippet `protect` prints for it runs through `sh`, so on Windows `sh` (Git Bash) must be on `PATH`.
- **Run by hand**, or by a hook manager that keeps git's input, `push-check` checks the files git tracks now, not what a push would upload.
- **Git LFS.** A git-lfs pre-push hook you already had runs first, and it uploads the LFS files before agentprint can block the push. agentprint reads only the small pointer files that git stores in their place.
- **Submodules.** `git push --recurse-submodules` pushes each submodule with that submodule's own hooks. The hook `protect` adds checks only the repository it is in: run `agentprint protect` in each submodule too.

**To undo it:** `agentprint protect --undo` takes out exactly what `protect` added. Your own pre-push hook goes back under its own name, and `package.json` comes back byte for byte. If you changed either file since, agentprint leaves it alone and says what to remove by hand. What `protect` wrote is recorded in `~/.agentprint/protect.json`. That includes a copy of `package.json` from before, which is dropped once undo is done or leaves the file to you.

## Good to know

- **Codex** runs a new hook only after you trust it. Type `/hooks` in Codex once after installing.
- **Hermes Agent** asks before it runs any new hook, and agentprint never approves its own. Start `hermes chat` once and accept it. Until then, and in gateway or cron runs that never ask, the hook is off. The MCP server still works.
- **Agent hooks need macOS or Linux.** (The git hook from `agentprint protect` works on Windows too.) On Windows, `agentprint install` adds only the MCP server and the note, and says so ("Hooks are not installed on Windows yet; the MCP server and the note are."). `status` and `uninstall` follow the same rule. Hooks for Windows come in a later step. The permission check (AP008) and its fix work on Windows: they read the file's access list and set it with `icacls` (see [AP008](/agentprint/checks/ap008/)).
- **agentprint only ever blocks, never approves.** Your agents' own approval rules still apply. If agentprint itself fails, the command runs, with a note that it could not check (unless you turn on strict mode, above).
- **Spotting a publish is best effort.** agentprint reads the command line. It is not a shell. It follows `cd`, `&&`, `bash -c`, package scripts, workspaces and release tools. When agentprint is unsure what would be uploaded, it says "could not check", never "OK".
- **It reads the scripts a command runs**, as long as they are inside the folder your agent works in. It follows scripts that run other scripts up to 4 levels deep:
  - shell scripts: `./release.sh`, `bash scripts/release.sh`, `source x.sh`, also after `cd "$(dirname "$0")"`, and wrappers that pass on what they are given (`./with-env.sh npm publish`, a script ending `exec "$@"`). A function in a script counts only where the script calls it.
  - Makefile targets, with their prerequisites and `$(MAKE) <target>` calls
  - justfile recipes, with their dependencies
  - the `preversion`, `version` and `postversion` scripts that `npm version` runs

  The publishes and pushes in them are checked like ones typed on the command line. One level further down, agentprint still reads the commands, but it only scans the scripts they run for a publish or push.
- **Some scripts get "could not check" instead**, but only when they name a publish or push. agentprint does not read a script in another language as commands: `python scripts/release.py`, `node release.js`, a Taskfile task. It only scans it for words like `npm publish` or `git push`. A script over 64 KB is scanned the same way, up to 8 MB. "Could not check" lets the command run with a note; strict mode blocks it. A script that is over 8 MB, or one agentprint cannot open, gets "could not check" only when the command or the file name looks like a release (`release.sh`, `npm run publish`). An ordinary build is left alone.
- **Not followed yet:** scripts outside the project folder, compiled programs, a script path built from a variable (`"$DIR/release.sh"`), scripts deeper than that scan, a function from a sourced script that only the command line calls (`source lib.sh && release`), heredocs typed on the command line, and PowerShell commands such as `pwsh -File release.ps1`. A publish hidden in one of these can slip past. The note in the agent's instructions still tells it to check.
- **Cursor** sends agentprint only the commands that look like a publish or push, or that run a file a publish can hide in: `make`, `just`, `task`, `npm version`, `./script`, `bash script`, `python script`, `node script`, `tsx script`, `deno run`, `uv run` and the other runners. Each of those starts agentprint once, which adds a short pause.

See which agents are armed with `agentprint status`.
