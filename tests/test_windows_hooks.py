"""Windows hooks, checked as text on every OS: the PowerShell launcher agentprint-hook.ps1, the
hook commands that start it, and each agent's Windows hook entry.

What real PowerShell does with them is in tests/test_launcher_windows.py (Windows only). Here
the PowerShell expressions agentprint writes are evaluated by a small reader of the only forms
it writes ('text', [char]N, +), and the layers around a hook command are replayed: cmd.exe /C
(Codex), Windows' command-line rules, and an outer PowerShell's double-quote expansion.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from agentprint import install, powershell
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.base import (
    BLOCK_EXIT,
    POWERSHELL,
    WINDOWS_LAUNCHER,
    Launch,
    YamlEdit,
)
from agentprint.integrations.cursor import MATCHER, WINDOWS_MATCHER

WINDOWS_AGENTS = {"claude-code", "codex", "copilot", "cursor"}
HOOK = r"C:\Users\O'Brien Zoë\.agentprint\bin" + "\\" + WINDOWS_LAUNCHER
HARD = [
    "",
    "plain",
    r"C:\Users\me\.agentprint\bin\agentprint-hook.ps1",
    r"C:\Users\O'Brien\x",  # an apostrophe ends a PowerShell single-quoted string
    r"C:\Program Files (x86)\100% done\x",  # % expands inside cmd.exe's double quotes
    'say "hi" $env:USERNAME `n !x! ^&',  # quote, variable, escape, delayed expansion
    "C:\\Users\\Zoë\\日本\\x",  # non-ASCII: a .ps1 without a BOM is read as ANSI by 5.1
    "smart ‘quotes’ “too” „x‛",  # PowerShell reads these as quote characters
    "emoji 😀 tab\tnew\nline",  # outside the BMP: two UTF-16 units
    '{"permission": "allow"}',
]
# Characters cmd.exe, Windows' command-line rules or an outer PowerShell's double quotes treat
# specially; the -Command text of a hook has none of them.
SPECIAL = set('"$`%!^\t\n\r') | set("‘’‚‛“”„")
_PART = re.compile(r"\s*(?:'([^']*)'|\[char\](\d+))\s*")


def ps_value(expr: str) -> str:
    """The value of an expression in the forms powershell.literal writes."""
    text = expr.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    data = bytearray()
    pos = 0
    while True:
        match = _PART.match(text, pos)
        assert match, (expr, pos)
        if match.group(1) is not None:
            data += match.group(1).encode("utf-16-le")
        else:
            data += int(match.group(2)).to_bytes(2, "little")
        pos = match.end()
        if pos == len(text):
            return data.decode("utf-16-le", "surrogatepass")
        assert text[pos] == "+", (expr, pos)
        pos += 1


def windows_argv(line: str) -> list[str]:
    """A command line split as Windows programs split theirs (CommandLineToArgvW)."""
    args: list[str] = []
    current: list[str] = []
    quoted = have = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\":
            count = 0
            while i < len(line) and line[i] == "\\":
                count += 1
                i += 1
            if i < len(line) and line[i] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    i += 1
            else:
                current.append("\\" * count)
            have = True
        elif char == '"':
            quoted, have = not quoted, True
            i += 1
        elif char in " \t" and not quoted:
            if have:
                args.append("".join(current))
                current, have = [], False
            i += 1
        else:
            current.append(char)
            have = True
            i += 1
    if have:
        args.append("".join(current))
    return args


def cmd_c(raw: str) -> str:
    """What cmd.exe runs for `cmd.exe /C "<raw>"`, Codex's way: with more than two quotes, the
    first and the last are stripped. Fails if a character cmd.exe acts on is outside quotes."""
    line = f'"{raw}"'
    last = line.rfind('"')
    run = line[1:last] + line[last + 1 :]
    assert "%" not in run  # %VAR% expands even inside double quotes
    quoted = False
    for char in run:
        if char == '"':
            quoted = not quoted
        elif not quoted:
            assert char not in "&|<>^()", run
    return run


def allow_part(text: str) -> str:
    """The value a hook command's -Command text prints when the launcher is gone."""
    before, last = text.rsplit("; ", 1)
    assert last == "exit 0"
    return ps_value(before.rsplit("; ", 1)[-1])


def command_text(command: str) -> str:
    """The -Command text of a hook command, as powershell.exe receives it."""
    argv = windows_argv(command)
    head = POWERSHELL.split()
    assert argv[: len(head)] == head and argv[len(head)] == "-Command", argv
    [text] = argv[len(head) + 1 :]
    return text


# ---- powershell.literal -----------------------------------------------------------------------


@pytest.mark.parametrize("text", HARD)
def test_literal_round_trips_and_is_plain_ascii(text):
    expr = powershell.literal(text)
    assert ps_value(expr) == text
    assert expr.isascii() and expr.isprintable()
    assert not SPECIAL & set(expr)
    inside = re.findall(r"'([^']*)'", expr)
    assert all(set(run) <= powershell.PLAIN for run in inside)


@pytest.mark.parametrize("text", HARD)
def test_literal_in_a_file_round_trips_and_is_ascii(text):
    expr = powershell.literal(text, powershell.IN_FILE)
    assert ps_value(expr) == text and expr.isascii()


def test_a_plain_string_stays_one_quoted_word():
    assert powershell.literal(r"C:\x\y.ps1") == r"'C:\x\y.ps1'"
    assert powershell.literal("") == "''"
    assert powershell.literal("'") == "([char]39)"


@pytest.mark.parametrize(
    ("text", "bare"), [("--agent", True), ("claude-code", True), ("--strict", True),
                       ("-x", False), ("a b", False), ("1", False), ("", False)],
)  # fmt: skip
def test_word_is_bare_only_for_plain_words(text, bare):
    out = powershell.word(text)
    assert (out == text) is bare
    if not bare:
        assert ps_value(out) == text


# ---- hook commands ----------------------------------------------------------------------------


@pytest.mark.parametrize("hook", [HOOK, r"C:\Program Files (x86)\100%\agentprint-hook.ps1"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("allow", ["", '{"permission": "allow"}'])
def test_windows_command_survives_every_layer_around_it(hook, strict, allow):
    launch = Launch(hook=hook, mcp=("agentprint", "mcp"), strict=strict, windows=True)
    command = launch.windows_command("cursor", allow)
    assert command.isascii() and command.count('"') == 2  # only around the -Command text
    text = command_text(command)
    assert command_text(cmd_c(command)) == text  # Codex: cmd.exe /C "<command>"
    assert not SPECIAL & set(text)  # an outer PowerShell's "..." leaves it as it is
    launcher = powershell.literal(hook)
    assert ps_value(launcher) == hook
    flags = " --strict" if strict else ""
    assert text.startswith(f"if (Test-Path -LiteralPath {launcher}) {{ try {{ & {launcher} ")
    assert f"& {launcher} --agent cursor{flags}; exit (Get-Variable -Name LASTEXITCODE" in text
    if allow:
        assert allow_part(text) == allow
    else:
        assert text.endswith(" catch { } }; exit 0")
    assert "$" not in text and "input" not in text  # never $input: stdin stays agentprint's


def test_powershell_command_passes_the_exit_code_on():
    launch = Launch(hook=HOOK, mcp=("agentprint", "mcp"), windows=True)
    assert launch.powershell_command("cursor", "x") == (
        launch.windows_command("cursor", "x") + "; exit $LASTEXITCODE"
    )


def test_posix_hook_command_is_unchanged_by_the_windows_flag():
    posix = Launch(hook="/h/.agentprint/bin/agentprint-hook", mcp=("agentprint", "mcp"))
    assert posix.hook_command("codex") == (
        '/bin/sh -c \'[ -x "$0" ] || exit 0; exec "$0" "$@"\' '
        "/h/.agentprint/bin/agentprint-hook --agent codex"
    )


# ---- each agent's Windows hook entry ------------------------------------------------------------


def _hook_entry(agent: str, launch: Launch) -> dict:
    for edit in ALL_INTEGRATIONS[agent].edits(Path("/nonexistent/home"), {}, launch):
        if edit.what.startswith("hook"):
            return json.loads(edit.apply(None))
    raise LookupError(agent)


WIN = Launch(hook=HOOK, mcp=("agentprint", "mcp"), windows=True)
POSIX = Launch(hook="/h/.agentprint/bin/agentprint-hook", mcp=("agentprint", "mcp"))


def test_only_agents_with_documented_windows_hooks_get_them():
    assert {n for n, i in ALL_INTEGRATIONS.items() if i.windows_hooks} == WINDOWS_AGENTS


def test_claude_code_windows_entry_runs_in_powershell_for_both_shell_tools():
    [group] = _hook_entry("claude-code", WIN)["hooks"]["PreToolUse"]
    assert group["matcher"] == "Bash|PowerShell"
    assert group["hooks"] == [
        {
            "type": "command",
            "command": WIN.powershell_command("claude-code"),
            "shell": "powershell",
            "timeout": 60,
        }
    ]


def test_codex_windows_entry_uses_command_windows():
    [group] = _hook_entry("codex", WIN)["hooks"]["PreToolUse"]
    command = WIN.windows_command("codex")
    assert group == {
        "matcher": "^Bash$",
        "hooks": [
            {"type": "command", "command": command, "commandWindows": command, "timeout": 60}
        ],
    }


def test_copilot_windows_entry_uses_the_powershell_field():
    [entry] = _hook_entry("copilot", WIN)["hooks"]["preToolUse"]
    assert entry == {
        "type": "command",
        "powershell": WIN.powershell_command("copilot"),
        "timeoutSec": 60,
        "matcher": "bash|powershell",
    }
    assert re.fullmatch(entry["matcher"], "powershell")  # Copilot anchors it: ^(?:...)$


def test_cursor_windows_entry_answers_allow_when_the_launcher_is_gone():
    [entry] = _hook_entry("cursor", WIN)["hooks"]["beforeShellExecution"]
    allow = ALL_INTEGRATIONS["cursor"].reply(None).stdout
    assert entry == {
        "command": WIN.powershell_command("cursor", allow),
        "timeout": 60,
        "matcher": WINDOWS_MATCHER,
    }
    windows = entry["command"].removesuffix("; exit $LASTEXITCODE")
    assert allow_part(command_text(windows)) == allow


@pytest.mark.parametrize("agent", sorted(WINDOWS_AGENTS))
def test_windows_entries_never_use_bin_sh_and_name_the_ps1_launcher(agent):
    text = json.dumps(_hook_entry(agent, WIN))
    assert "/bin/sh" not in text and WINDOWS_LAUNCHER in text


@pytest.mark.parametrize("agent", sorted(ALL_INTEGRATIONS))
def test_posix_entries_have_no_windows_fields(agent):
    [edit] = [
        e
        for e in ALL_INTEGRATIONS[agent].edits(Path("/nonexistent/home"), {}, POSIX)
        if e.what.startswith("hook")
    ]
    text = edit.apply("") if isinstance(edit, YamlEdit) else edit.apply(None)
    for key in (
        '"shell":',
        '"commandWindows":',
        '"powershell":',
        "powershell.exe",
        WINDOWS_LAUNCHER,
    ):
        assert key not in text, (agent, key)


def test_shell_tools_on_windows_reach_the_guard():
    claude = ALL_INTEGRATIONS["claude-code"].parse_hook(
        {"tool_name": "PowerShell", "tool_input": {"command": "npm publish"}, "cwd": "C:/p"}, {}
    )
    copilot = ALL_INTEGRATIONS["copilot"].parse_hook(
        {"toolName": "powershell", "toolArgs": '{"command": "npm publish"}', "cwd": "C:/p"}, {}
    )
    assert claude and claude.command == "npm publish"
    assert copilot and copilot.command == "npm publish"
    assert ALL_INTEGRATIONS["claude-code"].parse_hook({"tool_name": "Read"}, {}) is None


WINDOWS_PUBLISHES = [
    "npm.cmd publish",
    '& "C:\\Program Files\\nodejs\\npm.cmd" publish',
    "uv publish",
    "twine upload dist\\*",
    "cd /d D:\\pkg && npm publish",
    "Set-Location C:\\pkg; git push",
    'cmd /c "npm publish"',
    'powershell -Command "npm publish"',
    "git.exe push origin main",
    "NPM PUBLISH",
    "npx np",
    "pnpm -r Publish",
]


@pytest.mark.parametrize("command", WINDOWS_PUBLISHES)
def test_cursor_windows_matcher_fires_for_windows_publishes(command):
    assert re.search(WINDOWS_MATCHER, command), command


def test_cursor_windows_matcher_fires_wherever_the_posix_one_does():
    from tests.test_hooks import CURSOR_MATCHES

    for command in CURSOR_MATCHES:
        assert re.search(MATCHER, command) and re.search(WINDOWS_MATCHER, command), command


@pytest.mark.parametrize("command", ["ls", "dir", "Get-ChildItem", "npm test", "git status"])
def test_cursor_windows_matcher_skips_ordinary_commands(command):
    assert not re.search(WINDOWS_MATCHER, command), command


# ---- the launcher script ------------------------------------------------------------------------

AGENTPRINTS = [
    [r"C:\Users\O'Brien Zoë\.local\bin\agentprint.exe"],
    [r"C:\Users\me\.local\bin\uvx.exe", "agentprint==0.1.1"],
    [r"C:\Program Files\Python\python.exe", r"C:\x y\fake.py", r"C:\x y\config.json"],
]


@pytest.mark.parametrize("agentprint", AGENTPRINTS)
def test_launcher_is_ascii_and_runs_the_resolved_agentprint(agentprint):
    script = install.windows_launcher_script(agentprint)
    assert script.isascii() and "\r" not in script
    [exe] = re.findall(r"^\$start\.FileName = (.+)$", script, re.M)
    assert ps_value(exe) == agentprint[0]
    [line] = re.findall(r"^\$start\.Arguments = (.+) \+ ' --agent ' \+ \$agent$", script, re.M)
    assert ps_value(line) == subprocess.list2cmdline([*agentprint[1:], "guard"])
    assert windows_argv(ps_value(line))[-1] == "guard"
    assert windows_argv(ps_value(line))[:-1] == agentprint[1:]


def test_launcher_exits_only_0_or_2_and_never_reads_stdin():
    script = install.windows_launcher_script(["agentprint.exe"])
    assert sorted(set(re.findall(r"\bexit (\d+)", script))) == ["0", "2"]
    assert f"if ($status -eq {BLOCK_EXIT}) {{ Send-Bytes $buffer.ToArray(); exit 2 }}" in script
    assert "if ($status -eq 0) { Send-Bytes $buffer.ToArray(); exit 0 }" in script
    assert "$input" not in script.lower() and "readtoend" not in script.lower()
    assert "RedirectStandardInput" not in script  # agentprint reads the hook input itself
    assert "$env:PYTHONUTF8 = '1'" in script
    assert script.startswith("# agentprint-hook: Written by `agentprint install`")


def test_launcher_answers_each_agents_own_allow_when_agentprint_fails():
    script = install.windows_launcher_script(["agentprint.exe"])
    for name, integration in ALL_INTEGRATIONS.items():
        allow = integration.reply(None).stdout.strip()
        if allow:
            arm = f"{powershell.literal(name, powershell.IN_FILE)} {{ $allow = "
            [value] = re.findall(re.escape(arm) + r"(.+) \}$", script, re.M)
            assert ps_value(value) == allow, name
    assert "Exit-Allowed ('exit ' + $status)" in script
