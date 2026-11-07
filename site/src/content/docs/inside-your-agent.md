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

## Good to know

- **Codex** runs a new hook only after you trust it. Type `/hooks` in Codex once after installing.
- **Hermes Agent** asks before it runs any new hook, and agentprint never approves its own. Start `hermes chat` once and accept it. Until then, and in gateway or cron runs that never ask, the hook is off. The MCP server still works.
- **Hooks need macOS or Linux.** On Windows, `agentprint install` adds only the MCP server and the note, and says so ("Hooks are not installed on Windows yet; the MCP server and the note are."). `status` and `uninstall` follow the same rule. Hooks for Windows come in a later step. The permission check (AP008) and its fix are not run on Windows yet and print a note saying so.
- **agentprint only ever blocks, never approves.** Your agents' own approval rules still apply. If agentprint itself fails, the command runs, with a note that it could not check (unless you turn on strict mode, above).
- **Spotting a publish is best effort.** agentprint reads the command line. It is not a shell. It follows `cd`, `&&`, `bash -c`, package scripts, workspaces and release tools. A publish hidden in a Makefile, a script file or a heredoc can slip past, though the note still tells the agent to check. When agentprint is unsure what would be uploaded, it says "could not check", never "OK".

See which agents are armed with `agentprint status`.
