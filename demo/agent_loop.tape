# agentprint agent-in-the-loop demo, rendered with VHS (https://github.com/charmbracelet/vhs).
#
# Rendered in CI by .github/workflows/demo-gif.yml (choose this tape when you run it by hand).
# Same setup and look as demo/demo.tape: a throwaway folder (/tmp/agentprint-demo) built by
# demo/build_demo.py, HOME pointed at it, so no real agent settings are ever read.
#
# The story (under a minute): a SIMULATED agent wants to `npm publish`; the REAL agentprint hook
# blocks it and gives the agent its brief; the agent runs the real `agentprint fix --pack` and
# `agentprint fix`, retries, is let through, and names the keys the human must rotate (read from
# fix's answers). The banner on screen says so; the agent's own lines are dim and start "agent:".
# The script is demo/agent_loop.py (it holds the pauses); tests/test_demo.py runs it against a
# fresh world.

Output docs/agent-loop.gif

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
Set WaitTimeout 90s

Hide
Type `export REPO=$PWD DEMO=/tmp/agentprint-demo && python3 demo/build_demo.py "$DEMO" > /dev/null`
Type ` && cd "$DEMO" && export HOME="$DEMO" && unset GEMINI_CLI_HOME CODEX_HOME CLAUDE_CONFIG_DIR`
Type ` && unset CI GITHUB_ACTIONS CLAUDECODE GEMINI_CLI CODEX_SANDBOX_NETWORK_DISABLED CODEX_THREAD_ID CURSOR_AGENT OPENCODE COPILOT_CLI HERMES_SESSION_ID ANTIGRAVITY_AGENT`
Type ` && rm -rf "$DEMO/.agentprint" && export TERM=xterm-256color`
Enter
Wait
Ctrl+L
Sleep 300ms
Show

# One command: the script prints the banner, clears the screen between the four moments,
# and holds on each so it can be read.
Type `python3 "$REPO/demo/agent_loop.py" "$DEMO"`
Sleep 400ms
Enter
Wait+Screen /Left for you/
Wait
Sleep 2s
