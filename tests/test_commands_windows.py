"""The guard's reader on Windows command lines: PowerShell and cmd syntax (agentprint.winshell).

Pure parsing, so it runs on every OS: folders are compared the way pathlib joins them on the OS
the test runs on (`CWD / "C:/pkg"`), which is how the reader joins them too. The corpus of real
Windows forms lives in tests/data/windows_commands.txt.
"""

import base64
import json
import shlex
from pathlib import Path

import pytest

from agentprint import commands, platform, winshell
from agentprint.commands import Action, detect
from agentprint.guard import STRICT_HINT, evaluate
from tests import fakes
from tests.test_commands import CASES as POSIX_CASES
from tests.test_guard import LEAKY, _tgz, _wheel

CWD = Path("/work/app")
CORPUS = Path(__file__).parent / "data" / "windows_commands.txt"
P, G = Action.publish, Action.push


def _corpus() -> list[tuple[str, list[tuple]]]:
    cases = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        command, _, want = line.partition("  =>  ")
        assert want, f"no expectation on line: {line!r}"
        expected = []
        if want.strip() != "nothing":
            for part in want.split(" ; "):
                action, tool, folder, *rest = shlex.split(part)
                unsure = rest[-1:] == ["unsure"]
                targets = tuple(rest[:-1] if unsure else rest)
                expected.append((Action(action), tool, CWD / folder, targets, unsure))
        cases.append((command.strip(), expected))
    return cases


CORPUS_CASES = _corpus()


def _seen(command: str, **kwargs) -> list[tuple]:
    return [
        (r.action, r.tool, r.cwd, r.targets, bool(r.unsure)) for r in detect(command, CWD, **kwargs)
    ]


def test_the_corpus_is_a_real_one():
    assert len(CORPUS_CASES) >= 100
    assert sum(1 for _, want in CORPUS_CASES if not want) >= 30  # negatives too
    assert CORPUS.read_bytes().isascii()


@pytest.mark.parametrize(("command", "want"), CORPUS_CASES, ids=[c for c, _ in CORPUS_CASES])
def test_windows_corpus(command, want):
    """As the guard reads it on Windows."""
    assert _seen(command, windows=True) == want


@pytest.mark.parametrize(
    ("command", "want"),
    [case for case in CORPUS_CASES if winshell.looks_windows(case[0])],
    ids=[c for c, _ in CORPUS_CASES if winshell.looks_windows(c)],
)
def test_windows_markers_alone_pick_the_windows_reader(command, want):
    """The same command lines, with agentprint not told the agent is on Windows."""
    assert _seen(command) == want


def test_most_of_the_corpus_has_windows_markers():
    marked = [c for c, _ in CORPUS_CASES if winshell.looks_windows(c)]
    assert len(marked) >= 60


@pytest.mark.parametrize("command", [c for c, _ in POSIX_CASES])
def test_posix_command_lines_have_no_windows_markers(command):
    """So they keep the POSIX reader byte for byte (tests/test_commands.py checks what it says)."""
    assert not winshell.looks_windows(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo -e 'line1\\nline2' && git push",
        "printf '%s\\n' x",
        "find . -name \\*.py -exec cat {} \\;",
        "echo foo\\ bar",
        "curl -H 'Content-Type: application/json' https://example.com/x",
        "rsync -a dist/ host:/srv/www",
        "npm start",
        "date +%Y%m%d",
        "git push origin HEAD:refs/heads/main",
        'cd "$(dirname "$0")" && npm publish',
    ],
)
def test_ordinary_posix_text_is_not_mistaken_for_windows(command):
    assert not winshell.looks_windows(command)


@pytest.mark.parametrize(
    "command",
    [
        "cd C:\\pkg",
        "cd D:/pkg",
        ".\\release.ps1",
        "twine upload dist\\*",
        "$env:NPM_TOKEN",
        "Set-Location pkg",
        "npm.cmd publish",
        "git.exe push",
        "cmd /c npm publish",
        "powershell -c x",
        "pwsh -File release.ps1",
        "cd /d pkg",
        "\\\\server\\share\\pkg",
    ],
)
def test_windows_markers(command):
    assert winshell.looks_windows(command)


# ---- details the corpus format does not show ------------------------------------------------


def test_workspaces_and_releasers_keep_their_scope():
    (npm,) = detect("npm.cmd publish --workspace packages\\web", CWD, windows=True)
    assert npm.workspaces == ("packages/web",)
    (changeset,) = detect(".\\node_modules\\.bin\\changeset.cmd publish", CWD, windows=True)
    assert changeset.all_workspaces
    (pnpm,) = detect("pnpm.cmd -r publish", CWD, windows=True)
    assert pnpm.all_workspaces


def test_could_not_check_says_why():
    (risky,) = detect("cd $env:PKG; npm publish", CWD, windows=True)
    assert "$env:PKG" in risky.unsure and "only known when the command runs" in risky.unsure
    (risky,) = detect("& $npm publish", CWD, windows=True)
    assert risky.label == "$npm publish" and "variable" in risky.unsure
    (risky,) = detect("cd /d D:\\pkg; npm publish", CWD, windows=True)
    assert "PowerShell may reject" in risky.unsure


def test_encoded_command_is_decoded():
    code = base64.b64encode("cd pkg; git push".encode("utf-16-le")).decode()
    assert _seen(f"powershell -enc {code}", windows=True) == [(G, "git", CWD / "pkg", (), False)]
    assert detect("powershell -EncodedCommand not-base64!", CWD, windows=True) == []


def test_comments_and_here_strings():
    assert detect("# npm publish", CWD, windows=True) == []
    assert detect("npm test <# npm publish\n#> ; git status", CWD, windows=True) == []
    here = "$notes = @'\nnpm publish\n'@\nWrite-Host $notes"
    assert detect(here, CWD, windows=True) == []
    expanding = '$notes = @"\nrun $(npm publish)\n"@'
    assert _seen(expanding, windows=True) == [(P, "npm", CWD, (), False)]


def test_line_continuations():
    assert _seen("npm publish `\n  --access public", windows=True) == [(P, "npm", CWD, (), False)]
    assert _seen("npm publish \\\n  --tag next", windows=True) == [(P, "npm", CWD, (), False)]
    assert _seen("npm ^\npublish", shell="cmd", windows=True) == [(P, "npm", CWD, (), False)]


def test_cmd_comments_and_quotes():
    assert detect('cmd /c "rem npm publish"', CWD, windows=True) == []
    assert detect('cmd /c ":: npm publish"', CWD, windows=True) == []
    # cmd has no single quotes: the apostrophe is just a character
    assert _seen('cmd /c "git commit -m it\'s && git push"', windows=True) == [
        (G, "git", CWD, (), False)
    ]
    # ^ escapes the &, so there is no second command
    assert detect('cmd /c "echo a ^& npm publish"', CWD, windows=True) == []


def test_bash_from_windows_is_read_as_bash():
    """Git Bash or WSL's bash.exe, started from PowerShell."""
    assert _seen("bash -c 'cd lib && npm publish'", windows=True) == [
        (P, "npm", CWD / "lib", (), False)
    ]
    assert _seen('bash.exe -lc "git push"', windows=True) == [(G, "git", CWD, (), False)]


def test_git_bash_drive_paths():
    """In Git Bash, /c/Users/me is C:\\Users\\me."""
    (risky,) = detect("cd /c/Users/me/pkg && npm publish", CWD, shell="sh", windows=True)
    assert risky.cwd == CWD / "C:/Users/me/pkg" and not risky.unsure
    (risky,) = detect("cd /c/Users/me/pkg; npm publish", CWD, windows=True)  # PowerShell?
    assert risky.unsure  # /c/x is C:\x to Git Bash but \c\x to PowerShell


def test_pwsh_in_a_posix_shell_is_unwrapped():
    """pwsh on Linux or macOS, or in a shell script: the code it runs is PowerShell."""
    assert _seen("pwsh -c 'Set-Location pkg; npm.cmd publish'", shell="sh") == [
        (P, "npm", CWD / "pkg", (), False)
    ]
    assert _seen("bash -c \"pwsh -Command 'git push'\"") == [(G, "git", CWD, (), False)]


def test_nested_shells_stop_at_the_depth_limit():
    command = "npm publish"
    for level in range(commands.MAX_DEPTH + 3):
        command = f"cmd /c {command}" if level % 2 else f"pwsh -c {command}"
    found = detect(command, CWD, windows=True)
    assert found and all(r.action is P and r.tool == "npm" for r in found)


def test_the_guard_reads_windows_text_on_windows(monkeypatch, tmp_path):
    """`evaluate` asks `platform.is_windows()`: on Windows, a command without markers is still
    read as PowerShell (here a `cd` in ( ), which PowerShell keeps)."""
    _wheel(tmp_path / "dist" / "demo-1.0-py3-none-any.whl", {"demo/.codex/auth.json": "{}"})
    (tmp_path / "pkg").mkdir()
    command = "(cd pkg); twine upload dist/*"
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    assert evaluate(command, tmp_path).block  # POSIX: the cd stays in its subshell
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    verdict = evaluate(command, tmp_path)
    assert not verdict.block and "could not check" in verdict.warning


# ---- the guard on Windows forms -------------------------------------------------------------


@pytest.fixture
def leaky(tmp_path):
    (tmp_path / "pkg").mkdir()
    _tgz(tmp_path / "pkg" / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    _wheel(tmp_path / "dist" / "demo-1.0-py3-none-any.whl", {"demo/.codex/auth.json": "{}"})
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "twine.exe upload dist\\*",
        "py -m twine upload dist\\*",
        "cd pkg; npm.cmd publish demo-1.0.0.tgz",
        '& "C:\\Program Files\\nodejs\\npm.cmd" publish pkg\\demo-1.0.0.tgz',
        'cmd /c "cd /d pkg && npm publish demo-1.0.0.tgz"',
        'powershell -NoProfile -Command "Set-Location pkg; npm publish demo-1.0.0.tgz"',
        "Start-Process npm -ArgumentList 'publish','demo-1.0.0.tgz' -WorkingDirectory pkg",
    ],
)
def test_a_leaky_windows_publish_blocks(leaky, command):
    verdict = evaluate(command, leaky, windows=True)
    assert verdict.block
    assert verdict.reason.startswith("agentprint: BLOCK")
    assert fakes.ANTHROPIC not in verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "cd $env:PKG; npm.cmd publish demo-1.0.0.tgz",
        'cmd /c "cd %PKG% && npm publish demo-1.0.0.tgz"',
        "& $npm publish",
        "foreach ($p in 'pkg') { Push-Location $p; npm publish demo-1.0.0.tgz; Pop-Location }",
    ],
)
def test_a_windows_publish_it_cannot_follow_is_never_ok(leaky, command):
    verdict = evaluate(command, leaky, windows=True)
    assert not verdict.block and "could not check" in verdict.warning
    strict = evaluate(command, leaky, windows=True, strict=True)
    assert strict.block and STRICT_HINT in strict.reason


@pytest.mark.parametrize(
    "command", ["dir", "Get-ChildItem -Recurse", "npm test", "cargo build", "npm.cmd run build"]
)
def test_ordinary_windows_commands_are_not_our_business(tmp_path, command):
    assert evaluate(command, tmp_path, windows=True) is None
    assert evaluate(command, tmp_path, windows=True, strict=True) is None


def test_package_scripts_on_windows(tmp_path):
    """npm runs scripts in cmd on Windows: one with Windows paths is read as cmd; one written
    for sh (single quotes) is still read as sh."""
    (tmp_path / "packages" / "web").mkdir(parents=True)
    _tgz(tmp_path / "packages" / "web" / "demo-1.0.0.tgz", {"package/index.js": b"1"})
    scripts = {
        "release": "cd packages\\web && npm publish demo-1.0.0.tgz",
        "pub": "bash -c 'git push origin main'",
    }
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (rel,) = detect("npm.cmd run release", tmp_path, windows=True)
    assert rel.cwd == tmp_path / "packages/web" and rel.targets == ("demo-1.0.0.tgz",)
    (pub,) = detect("npm run pub", tmp_path, windows=True)
    assert (pub.action, pub.tool, pub.cwd) == (G, "git", tmp_path)


# ---- PowerShell and batch scripts -----------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # as written: CRLF stays CRLF
    return path


def _found(command: str, cwd: Path, **kwargs) -> list[tuple]:
    return [
        (r.action, r.tool, r.cwd.resolve(), r.targets, bool(r.unsure))
        for r in detect(command, cwd, **kwargs)
    ]


@pytest.mark.parametrize(
    "command",
    [
        ".\\scripts\\release.ps1",
        "& .\\scripts\\release.ps1",
        "& '.\\scripts\\release.ps1' -Version 1.0",
        ". .\\scripts\\release.ps1",
        "pwsh -File scripts\\release.ps1",
        "pwsh scripts/release.ps1",
        "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\release.ps1",
        "scripts/release.ps1",
    ],
)
def test_a_powershell_script_is_followed(tmp_path, command):
    _write(tmp_path, "scripts/release.ps1", "$ErrorActionPreference = 'Stop'\nnpm.cmd publish\n")
    assert _found(command, tmp_path, windows=True) == [(P, "npm", tmp_path.resolve(), (), False)]


def test_a_powershell_script_is_followed_on_any_os(tmp_path):
    """pwsh runs .ps1 files on Linux and macOS too: no Windows markers needed."""
    _write(tmp_path, "release.ps1", "#!/usr/bin/env pwsh\ngit push --follow-tags\n")
    assert _found("pwsh -File release.ps1", tmp_path) == [(G, "git", tmp_path.resolve(), (), False)]
    _write(tmp_path, "release.sh", "#!/bin/sh\npwsh -NoProfile -c 'git push'\n")
    assert _found("./release.sh", tmp_path) == [(G, "git", tmp_path.resolve(), (), False)]


def test_ps_script_root_is_the_scripts_folder(tmp_path):
    _write(
        tmp_path,
        "scripts/release.ps1",
        "Set-Location $PSScriptRoot\\..\\pkg\ntwine upload dist\\*\n"
        'Push-Location "$PSScriptRoot"; git push; Pop-Location\n',
    )
    assert _found(".\\scripts\\release.ps1", tmp_path, windows=True) == [
        (P, "twine", (tmp_path / "pkg").resolve(), ("dist/*",), False),
        (G, "git", (tmp_path / "scripts").resolve(), (), False),
    ]


def test_ps_script_root_with_a_space_in_it(tmp_path):
    _write(tmp_path, "my scripts/release.ps1", "Set-Location $PSScriptRoot; npm publish\n")
    assert _found("& '.\\my scripts\\release.ps1'", tmp_path, windows=True) == [
        (P, "npm", (tmp_path / "my scripts").resolve(), (), False)
    ]


def test_a_batch_file_is_read_as_cmd(tmp_path):
    _write(
        tmp_path,
        "tools/release.cmd",
        "@echo off\r\nrem npm publish happens below\r\nsetlocal\r\ncd /d %~dp0..\r\n"
        "call npm publish\r\nif errorlevel 1 exit /b 1\r\n",
    )
    assert _found(".\\tools\\release.cmd", tmp_path, windows=True) == [
        (P, "npm", tmp_path.resolve(), (), False)
    ]
    assert _found("cmd /c tools\\release.cmd", tmp_path, windows=True) == [
        (P, "npm", tmp_path.resolve(), (), False)
    ]


def test_a_powershell_script_calls_another(tmp_path):
    _write(tmp_path, "release.ps1", "& $PSScriptRoot\\steps\\publish.ps1\n")
    _write(tmp_path, "steps/publish.ps1", "Set-Location $PSScriptRoot\\..\nuv publish\n")
    assert _found(".\\release.ps1", tmp_path, windows=True) == [
        (P, "uv", tmp_path.resolve(), (), False)
    ]


@pytest.mark.parametrize(
    "text",
    [
        'Write-Host "Now run npm publish"\n',
        "# npm publish\n<# git push #>\nnpm run build\n",
        "$steps = @'\nnpm publish\ngit push\n'@\nWrite-Output $steps\n",
        "function Show-Help { Write-Host 'twine upload dist\\*' }\n",
        "npm publish --dry-run\n",
    ],
    ids=["message", "comments", "here-string", "help-text", "dry-run"],
)
def test_a_powershell_script_that_only_mentions_a_publish_is_left_alone(tmp_path, text):
    _write(tmp_path, "build.ps1", text)
    assert detect(".\\build.ps1", tmp_path, windows=True) == []


def test_a_powershell_script_it_cannot_follow_says_so(tmp_path):
    _write(tmp_path, "release.ps1", "$npm = Get-Command npm\n& $npm publish\n")
    (risky,) = detect(".\\release.ps1", tmp_path, windows=True)
    assert risky.unsure and risky.action is P
    _write(tmp_path, "big.ps1", "Write-Host x\n" * 7000 + "npm.cmd publish\n")  # over 64 KB
    (risky,) = detect(".\\big.ps1", tmp_path, windows=True)
    assert (risky.action, risky.tool, risky.label) == (P, "npm", "big.ps1")
    assert "larger than 64 KB" in risky.unsure


def test_other_languages_naming_a_windows_program(tmp_path):
    _write(tmp_path, "release.py", 'import subprocess\nsubprocess.run(["npm.cmd", "publish"])\n')
    _write(tmp_path, "push.js", "require('child_process').execSync('git.exe push')\n")
    (py,) = detect("python release.py", tmp_path)
    assert (py.action, py.tool, bool(py.unsure)) == (P, "npm", True)
    (js,) = detect("node push.js", tmp_path)
    assert (js.action, js.tool, bool(js.unsure)) == (G, "git", True)


def test_a_leaky_publish_from_a_powershell_script_blocks(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    _write(
        tmp_path,
        "scripts/release.ps1",
        "Set-Location $PSScriptRoot\\..\nnpm publish demo-1.0.0.tgz\n",
    )
    verdict = evaluate(".\\scripts\\release.ps1", tmp_path, windows=True)
    assert verdict.block and fakes.ANTHROPIC not in verdict.reason
