---
title: Inside your AI agent
description: What agentprint install adds to each AI coding agent, what the agent reads when a publish would leak, and how to undo it.
---

`agentprint install` finds the AI coding agents on your computer and arms each one: it adds three things.

- **A hook** that runs before the agent's shell commands. Only package publishes (`npm`, `pnpm`, `yarn` and `bun publish`, `uv publish`, `twine upload`, changesets, lerna, and so on) and `git push` are checked. Everything else runs untouched.
- **An MCP server** (`agentprint mcp`) with the tools `check_package`, `check_push`, `scan_secrets` and `explain_finding`, so the agent can check on its own.
- **A short note** in the agent's global instructions: check before publishing or pushing, and never print a key.

Together, the hook and the check it runs are the tripwire. See [What `install` changes](/agentprint/agents/#what-install-changes) for the exact file each agent gets.

## What the agent reads

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

- `agentprint install --strict` writes hooks that pass `--strict`. Run a plain `agentprint install` to go back.
- Or set `AGENTPRINT_STRICT=1` in the environment your agent runs in, or call `agentprint guard --strict`.

What strict mode does and does not cover:

- A publish or push that agentprint cannot check is blocked. That includes a command agentprint cannot parse when it looks like a publish or push. An ordinary command it cannot parse still runs, with a note.
- If agentprint fails to start at all (the launcher is missing, `uvx` is unavailable, or it crashes before `guard` runs), the command still runs.
- An unknown agent name still runs.
- It is still limited to what agentprint can see: a secret assembled at run time, or one inside an arbitrary script, is not detected.

## Good to know

- **Codex** runs a new hook only after you trust it. Type `/hooks` in Codex once after installing.
- **Hermes Agent** asks before it runs any new hook, and agentprint never approves its own. Start `hermes chat` once and accept it. Until then, and in gateway or cron runs that never ask, the hook is off. The MCP server still works.
- **Hooks need macOS or Linux.** On Windows, only the MCP server and the note are installed for now.
- **agentprint only ever blocks, never approves.** Your agents' own approval rules still apply. If agentprint itself fails, the command runs, with a note that it could not check (unless you turn on strict mode, below).
- **Spotting a publish is best effort.** agentprint reads the command line. It is not a shell. It follows `cd`, `&&`, `bash -c`, package scripts, workspaces and release tools. A publish hidden in a Makefile, a script file or a heredoc can slip past, though the note still tells the agent to check. When agentprint is unsure what would be uploaded, it says "could not check", never "OK".

See which agents are armed with `agentprint status`.
