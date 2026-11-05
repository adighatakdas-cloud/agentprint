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
Set Height 1000
Set Padding 24
Set WindowBar Colorful
Set TypingSpeed 25ms
Set WaitTimeout 60s

Hide
Type `export DEMO=/tmp/agentprint-demo && python3 demo/build_demo.py "$DEMO" > /dev/null`
Type ` && cd "$DEMO" && export HOME="$DEMO" && unset GEMINI_CLI_HOME CODEX_HOME CLAUDE_CONFIG_DIR`
Enter
Wait
Ctrl+L
Sleep 300ms
Show

# 1. What AI agents left behind: on this (fake) machine and in the project.
Type "agentprint scan --home fake-home my-app"
Sleep 400ms
Enter
Wait
Sleep 4s
Ctrl+L

# 2. Before `npm publish`: the package would ship .claude/settings.local.json with a key in it.
#    This is the answer an AI agent gets (from the hook, the MCP tool or this command):
#    the verdict first, then fix steps it can do itself, then what only the human can do.
Type "agentprint pack my-app --format agent"
Sleep 400ms
Enter
Wait
Sleep 5s
Ctrl+L

# 3. Wire agentprint into every agent it finds (dry run: shows the changes, writes nothing).
Type "agentprint install --dry-run --home fake-home"
Sleep 400ms
Enter
Wait
Sleep 3500ms
