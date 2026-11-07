# agentprint demo for the README, rendered with VHS (https://github.com/charmbracelet/vhs).
#
# Rendered in CI by .github/workflows/demo-gif.yml (run it by hand from the Actions tab).
# Needs `agentprint`, `npm` and `python3` on PATH. Everything happens in a throwaway folder
# (/tmp/agentprint-demo) built by demo/build_demo.py: a fake home and a fake npm project with
# FAKE keys. HOME points there too, so no real agent settings are ever read.
#
# tests/test_demo.py runs every visible `agentprint` command below, so keep them in step.

Output docs/demo.gif

Require agentprint
Require npm
Require python3

Set Shell "bash"
Set FontSize 20
Set Width 1260
Set Height 1100
Set Padding 24
Set WindowBar Colorful
Set TypingSpeed 25ms
Set WaitTimeout 60s

Hide
Type `export DEMO=/tmp/agentprint-demo && python3 demo/build_demo.py "$DEMO" > /dev/null`
Type ` && cd "$DEMO" && export HOME="$DEMO" && unset GEMINI_CLI_HOME CODEX_HOME CLAUDE_CONFIG_DIR`
Type ` && unset CI GITHUB_ACTIONS CLAUDECODE GEMINI_CLI CODEX_SANDBOX_NETWORK_DISABLED CODEX_THREAD_ID CURSOR_AGENT OPENCODE COPILOT_CLI HERMES_SESSION_ID ANTIGRAVITY_AGENT`
Type ` && rm -rf "$DEMO/.agentprint" && mkdir -p "$DEMO/.claude" && export TERM=xterm-256color`
Enter
Wait
Ctrl+L
Sleep 300ms
Show

# 1. First run, as a person sees it: the mascot welcome sweeps in.
Type "agentprint"
Sleep 400ms
Enter
Sleep 6s
Wait
Sleep 3s
Ctrl+L

# 2. Next run: the arrow-key menu. Move down to Roster and pick it.
Type "agentprint"
Sleep 400ms
Enter
Sleep 2s
Down
Sleep 700ms
Down
Sleep 700ms
Down
Sleep 700ms
Down
Sleep 1s
Enter
Wait
Sleep 3s
Ctrl+L

# 3. What AI agents left behind: grouped results ending on the BLOCK line.
Type "agentprint scan --home fake-home my-app"
Sleep 400ms
Enter
Wait
Sleep 4s
Ctrl+L

# 4. Before `npm publish`: the package would ship .claude/settings.local.json with a key in it.
#    This is the answer an AI agent gets (from the hook, the MCP tool or this command):
#    the verdict first, then fix steps it can do itself, then what only the human can do.
Type "agentprint pack my-app --format agent"
Sleep 400ms
Enter
Wait
Sleep 6s
