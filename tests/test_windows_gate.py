"""Windows: install writes hooks (through the PowerShell launcher) only for agents whose docs
say how a hook runs on Windows, the MCP server and the note for every agent, and never a /bin/sh
hook or launcher.

`platform.is_windows` is monkeypatched (never `os.name`, which would break pathlib), so these
run on every OS. Fake homes under tmp_path only.
"""

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from agentprint import install, platform
from agentprint.integrations import ALL_INTEGRATIONS
from agentprint.integrations.windsurf import devin_dir
from agentprint.roster import roster
from tests.conftest import home_vars

AGENTPRINT = ["C:/Tools/agentprint.exe"]


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(AGENTPRINT))


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    for folder in (".claude", ".codex", ".cursor", ".gemini", ".copilot"):
        (home / folder).mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text('{"theme": "x"}\n')
    (home / ".config" / "opencode").mkdir(parents=True)
    return home


def _run(home, fn=install.install, **kwargs):
    lines: list[str] = []
    kwargs.setdefault("dry_run", False)
    fn(home, {}, None, echo=lines.append, **kwargs)
    return "\n".join(lines)


def _files(home: Path) -> dict[str, str]:
    return {
        str(p.relative_to(home)): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(home.rglob("*"))
        if p.is_file()
    }


HOOKS_LINE = (
    "Hooks on Windows: Claude Code, Codex, Cursor, GitHub Copilot. Only the MCP server and the "
    "note (no Windows hooks yet): Gemini CLI, OpenCode."
)


def test_install_on_windows_hooks_only_agents_with_windows_hook_docs(win, home):
    out = _run(home)
    assert HOOKS_LINE in out.splitlines()
    written = _files(home)
    assert not any("/bin/sh" in text for text in written.values())
    assert not (home / ".agentprint" / "bin" / "agentprint-hook").exists()  # the POSIX one
    launcher = home / ".agentprint" / "bin" / "agentprint-hook.ps1"
    assert launcher.read_text() == install.windows_launcher_script(AGENTPRINT)
    assert "== agentprint ==" in out
    claude = json.loads((home / ".claude" / "settings.json").read_text())
    [handler] = claude["hooks"]["PreToolUse"][0]["hooks"]
    assert handler["shell"] == "powershell" and str(launcher) in handler["command"]
    [codex] = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]["PreToolUse"]
    assert str(launcher) in codex["hooks"][0]["commandWindows"]
    [cursor] = json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"][
        "beforeShellExecution"
    ]
    assert cursor["command"].startswith("powershell.exe -NoProfile")
    [copilot] = json.loads((home / ".copilot" / "hooks" / "agentprint.json").read_text())["hooks"][
        "preToolUse"
    ]
    assert "bash" not in copilot and str(launcher) in copilot["powershell"]
    assert "hooks" not in json.loads((home / ".gemini" / "settings.json").read_text())
    assert not (home / ".config" / "opencode" / "plugins").exists()


def test_windows_hooks_line_names_each_agent_once(win, home):
    found = install.select(None, home, {})
    line = install.windows_hooks_line(found)
    assert line == HOOKS_LINE
    assert all(line.count(i.label) == 1 for i in found)
    assert install.windows_hooks_line([ALL_INTEGRATIONS["hermes"]]) == (
        "Hooks on Windows: none of these agents. Only the MCP server and the note (no Windows "
        "hooks yet): Hermes Agent."
    )
    assert install.windows_hooks_line([ALL_INTEGRATIONS["codex"]]) == "Hooks on Windows: Codex."


def test_windows_reinstall_changes_nothing(win, home):
    _run(home)
    before = _files(home)
    assert "Nothing changed" in _run(home)
    assert _files(home) == before


def test_install_on_windows_still_adds_the_mcp_server_and_the_note(win, home):
    _run(home)
    assert "agentprint" in json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert "agentprint" in (home / ".codex" / "config.toml").read_text()
    assert "agentprint" in (home / ".claude" / "CLAUDE.md").read_text()
    assert "agentprint" in (home / ".copilot" / "mcp-config.json").read_text()


def test_dry_run_on_windows_says_the_same(win, home):
    out = _run(home, dry_run=True)
    assert HOOKS_LINE in out.splitlines()
    assert "agentprint-hook.ps1" in out
    assert not (home / ".agentprint").exists()


def _tree(home: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(home).as_posix(): None if p.is_dir() else p.read_bytes()
        for p in sorted(home.rglob("*"))
    }


def test_status_and_uninstall_agree_on_windows(win, home):
    before = _tree(home)
    _run(home)
    states = {s.name: s.armed for s in roster(home, {}) if s.found}
    assert states == dict.fromkeys(states, True)  # every agent found has agentprint in it
    _run(home, fn=install.uninstall)
    assert _tree(home) == before  # byte for byte, folders included
    assert not any(s.armed for s in roster(home, {}))


def test_windows_uninstall_also_removes_what_an_older_agentprint_wrote(monkeypatch, home):
    # agentprint 0.1.0 had no Windows gate: it wrote the /bin/sh launcher and /bin/sh hooks
    # (Gemini CLI's too) on Windows. Uninstall there takes every one of them out.
    before = _tree(home)
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(AGENTPRINT))
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    _run(home)
    assert (home / ".agentprint" / "bin" / "agentprint-hook").exists()
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    _run(home, fn=install.uninstall)
    assert _tree(home) == before


def test_windows_install_replaces_an_older_bin_sh_hook(monkeypatch, home):
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(AGENTPRINT))
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    _run(home)
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    _run(home)
    hooks = [
        (home / ".claude" / "settings.json").read_text(),
        (home / ".codex" / "hooks.json").read_text(),
        (home / ".cursor" / "hooks.json").read_text(),
        (home / ".copilot" / "hooks" / "agentprint.json").read_text(),
    ]
    assert all("/bin/sh" not in text and "agentprint-hook.ps1" in text for text in hooks)


def test_posix_install_does_not_print_the_windows_line(monkeypatch, home):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["/opt/agentprint"])
    out = _run(home, dry_run=True)
    assert "Windows" not in out and "agentprint-hook.ps1" not in out


@pytest.mark.parametrize(
    ("path", "parts"),
    [
        (PureWindowsPath("C:/Users/me/x.json"), ("C", "Users", "me", "x.json")),
        (PureWindowsPath("D:/Users/me/x.json"), ("D", "Users", "me", "x.json")),
        (PureWindowsPath("//server/share/dir/f.json"), ("UNC", "server", "share", "dir", "f.json")),
        (PurePosixPath("/etc/x/y"), ("etc", "x", "y")),
    ],
)
def test_drive_safe_parts_keep_the_drive(path, parts):
    assert platform.drive_safe_parts(path) == parts


def test_backup_paths_on_two_drives_never_collide_or_land_on_the_original(tmp_path):
    home = tmp_path / "home"
    # Built from Windows-style parts but joined under tmp_path: the point is the relative tail.
    c = platform.drive_safe_parts(PureWindowsPath("C:/a/f.json"))
    d = platform.drive_safe_parts(PureWindowsPath("D:/a/f.json"))
    assert c != d
    assert not any(":" in part or "\\" in part for part in (*c, *d))
    backup = install._backup_path(home, tmp_path / "elsewhere" / "f.json")
    assert home / ".agentprint" / "backups" / "abs" in backup.parents


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\me\AppData\Local\uv\cache\archive-v0\abc\Scripts\agentprint.exe",
        r"C:\Users\me\AppData\Local\Temp\tmpx\Scripts\agentprint.exe",
        r"c:\users\me\appdata\local\temp\x\agentprint.exe",
    ],
)
def test_ephemeral_knows_windows_paths(path):
    assert install._ephemeral(path)  # backslashes are handled on every OS


@pytest.mark.windows_only
def test_ephemeral_on_real_windows_backslash_paths():
    assert install._ephemeral(r"C:\Users\me\AppData\Local\uv\cache\archive-v0\x\agentprint.exe")


@pytest.mark.parametrize(
    "path",
    [
        "C:/Program Files/Python/Scripts/agentprint.exe",
        "C:/Users/me/.local/bin/agentprint.exe",
        "C:/Users/me/pipx/venvs/agentprint/Scripts/agentprint.exe",  # pipx install: permanent
        "/usr/local/bin/agentprint",
        "/home/me/.local/share/uv/tools/agentprint/bin/agentprint",
    ],
)
def test_permanent_installs_are_not_ephemeral(path):
    assert not install._ephemeral(path)


def test_devin_dir_is_appdata_on_windows(win, tmp_path):
    env = home_vars(tmp_path)
    assert devin_dir(tmp_path, env) == Path(env["APPDATA"]) / "devin"
    assert devin_dir(tmp_path, {}) == tmp_path / "AppData" / "Roaming" / "devin"


def test_devin_dir_is_unchanged_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    assert devin_dir(tmp_path, home_vars(tmp_path)) == tmp_path / ".config" / "devin"
    assert devin_dir(tmp_path, {"XDG_CONFIG_HOME": "/x"}) == Path("/x") / "devin"


def test_roaming_appdata_is_used_only_for_the_real_home(win, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("APPDATA", str(tmp_path / "Redirected"))
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    assert platform.roaming_appdata(Path.home()) == tmp_path / "Redirected"
    assert platform.roaming_appdata(other) == other / "AppData" / "Roaming"


def test_roaming_appdata_off_windows_is_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "x"))
    assert platform.roaming_appdata(Path.home()) == Path.home() / "AppData" / "Roaming"


@pytest.mark.parametrize("windows", [False, True])
def test_every_integration_has_hook_edits_the_gate_can_see(tmp_path, windows):
    # The gate drops edits whose description starts with "hook": keep that contract honest.
    launch = install.Launch(hook=str(tmp_path / "h"), mcp=("agentprint", "mcp"), windows=windows)
    for name, integration in ALL_INTEGRATIONS.items():
        whats = [e.what for e in integration.edits(tmp_path, {}, launch)]
        hooks = [w for w in whats if w.startswith("hook")]
        assert len(hooks) <= 1, name
        assert any("MCP" in w or "instructions" in w for w in whats), name


# ---- what was not checked travels with the result --------------------------------------------


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "index.js").write_text("module.exports = 1;\n")
    return root


def _cli(*args):
    from typer.testing import CliRunner

    from agentprint.cli import app

    return CliRunner().invoke(app, list(args))


@pytest.mark.parametrize("fmt", ["agent", "json", "table"])
def test_scan_says_permissions_were_not_checked_on_windows(win, proj, fmt):
    out = _cli("scan", str(proj), "--no-home", "--format", fmt).output
    assert "not checked" in out.lower() or "not_checked" in out
    assert "AP008" in out and "Windows" in out
    if fmt == "json":
        assert json.loads(out)["not_checked"] == [platform.NOT_CHECKED_WINDOWS]
    if fmt == "agent":
        assert out.splitlines()[0] == "agentprint: OK"  # line 1 stays the verdict
        assert f"not_checked: {platform.NOT_CHECKED_WINDOWS}" in out


@pytest.mark.parametrize("fmt", ["agent", "json", "table"])
def test_scan_on_posix_has_no_not_checked_note(monkeypatch, proj, fmt):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    out = _cli("scan", str(proj), "--no-home", "--format", fmt).output
    assert "not_checked" not in out and "not checked" not in out.lower()


@pytest.mark.parametrize("fmt", ["agent", "json", "table"])
def test_fix_says_permissions_were_not_checked_on_windows(win, proj, fmt):
    out = _cli("fix", str(proj), "--no-home", "--format", fmt).output
    assert "AP008" in out and "Windows" in out
    if fmt == "json":
        assert json.loads(out)["not_checked"] == [platform.NOT_CHECKED_WINDOWS]


def test_fix_on_posix_has_no_not_checked_note(monkeypatch, proj):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    out = _cli("fix", str(proj), "--no-home", "--format", "json").output
    assert "not_checked" not in json.loads(out)


def test_mcp_scan_tool_says_permissions_were_not_checked_on_windows(win, proj):
    from tests.test_mcp import call

    text = call("scan_secrets", path=str(proj))
    assert text.splitlines()[0] == "agentprint: OK"
    assert f"not_checked: {platform.NOT_CHECKED_WINDOWS}" in text


def test_mcp_scan_tool_on_posix_has_no_note(monkeypatch, proj):
    from tests.test_mcp import call

    monkeypatch.setattr(platform, "is_windows", lambda: False)
    assert "not_checked" not in call("scan_secrets", path=str(proj))


# ---- hook talk on Windows only where a hook goes in -------------------------------------------


def test_windows_install_without_hooked_agents_says_only_the_one_line(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    uvx = ["uvx", f"agentprint=={install.__version__}"]
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(uvx))
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text("{}\n")
    (home / ".hermes").mkdir()
    (home / ".hermes" / "config.yaml").write_text("model: x\n")
    out = _run(home, strict=True)
    line = (
        "Hooks on Windows: none of these agents. Only the MCP server and the note (no Windows "
        "hooks yet): Gemini CLI, Hermes Agent."
    )
    assert "hooks use" not in out and "/hooks" not in out and "approve" not in out
    assert "Strict mode" not in out and "hermes hooks" not in out
    talk = [
        ln for ln in out.splitlines() if "hook" in ln.lower() and not ln.lstrip().startswith("+")
    ]
    assert talk == [line]  # (the note's own text is a diff line, "+...")
    assert not (home / ".agentprint" / "bin").exists()


def test_windows_install_with_codex_keeps_its_hook_notes(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "is_windows", lambda: True)
    uvx = ["uvx", f"agentprint=={install.__version__}"]
    monkeypatch.setattr(install, "resolve_agentprint", lambda: list(uvx))
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".hermes").mkdir()
    out = _run(home, strict=True)
    lines = out.splitlines()
    assert (
        "Hooks on Windows: Codex. Only the MCP server and the note (no Windows hooks yet): "
        "Hermes Agent."
    ) in lines
    assert "Strict mode: a publish or push agentprint cannot check will be blocked." in lines
    assert "hooks use uvx" in out and "/hooks" in out  # Codex's trust note
    assert "hermes hooks" not in out and "hermes chat" not in out  # no Hermes hook, no note
    [codex] = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]["PreToolUse"]
    assert codex["hooks"][0]["commandWindows"].count("--strict") == 1


def test_posix_install_keeps_its_hook_notes(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    monkeypatch.setattr(install, "resolve_agentprint", lambda: ["uvx", "agentprint==0"])
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    out = _run(home, dry_run=True)
    assert "hooks use uvx" in out and "/hooks" in out


# ---- long-path and device prefixes; POSIX path matching unchanged -----------------------------


@pytest.mark.parametrize(
    ("raw", "parts"),
    [
        ("\\\\?\\C:\\x\\y", ("C", "x", "y")),
        ("\\\\?\\UNC\\server\\share\\x", ("UNC", "server", "share", "x")),
        ("\\\\.\\COM1", ("DEVICE", "COM1")),
    ],
)
def test_extended_prefixes_never_become_folder_names(raw, parts):
    got = platform.drive_safe_parts(PureWindowsPath(raw))
    assert got == parts
    assert not any(c in part for part in got for c in "?.:\\")


def test_ephemeral_leaves_posix_paths_as_they_were():
    assert not install._ephemeral("/usr/local//bin/./agentprint")
    assert install._ephemeral("/home/me/.cache/uv/archive-v0/x/bin/agentprint")
    assert install._ephemeral("/tmp//x/agentprint")
