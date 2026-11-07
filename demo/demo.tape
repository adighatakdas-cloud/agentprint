# agentprint demo for the README, rendered with VHS (https://github.com/charmbracelet/vhs).
#
# Rendered in CI by .github/workflows/demo-gif.yml (run it by hand from the Actions tab).
# Needs `agentprint`, `npm` and `python3` on PATH. Everything happens in a throwaway folder
# (/tmp/agentprint-demo) built by demo/build_demo.py: a fake home and a fake npm project with
# FAKE keys. HOME points there too, so no real agent settings are ever read.
#
# The story (about 35 s): welcome, menu, `scan` ends on BLOCK, `fix` (answer y) ends on CLEAR,
# then `pack` shows the answer an AI agent gets. The Waits and pauses are there so each moment
# can be read. tests/test_demo.py runs every visible `agentprint` command below, so keep them in step.

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
Sleep 3500ms
Wait
Sleep 1500ms
Ctrl+L

# 2. Next run: the arrow-key menu. Move down to Roster and pick it.
Type "agentprint"
Sleep 400ms
Enter
Sleep 1s
Down
Sleep 400ms
Down
Sleep 400ms
Down
Sleep 400ms
Down
Sleep 600ms
Enter
Wait
Sleep 1500ms
Ctrl+L

# 3. What AI agents left behind: grouped results ending on the BLOCK line. Hold on it.
Type "agentprint scan --home fake-home my-app"
Sleep 400ms
Enter
Wait+Screen /BLOCK/
Wait
Sleep 4s
Ctrl+L

# 4. The fix, the moment the demo is about. The plan shows what changes and what stays with
#    the human; hold on it, answer y, then hold on CLEAR.
Type "agentprint fix --home fake-home my-app"
Sleep 400ms
Enter
Wait+Screen /Apply 5 fixes/
Sleep 5s
Type "y"
Sleep 400ms
Enter
Wait+Screen /CLEAR/
Wait
Sleep 4s
Ctrl+L

# 5. Before `npm publish`: the package would still ship .claude/settings.local.json.
#    This is the answer an AI agent gets (from the hook, the MCP tool or this command):
#    the verdict first, then fix steps it can do itself, then what only the human can do.
Type "agentprint pack my-app --format agent"
Sleep 400ms
Enter
Wait
Sleep 4s
