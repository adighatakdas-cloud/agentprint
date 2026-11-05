import os
import pathlib
import shlex
import sqlite3
import stat
import subprocess
import sys

import pytest

from agentprint.agents.base import HomeArtifact
from agentprint.checks import SCAN_CHECKS
from agentprint.checks import ap008_readable_by_others as ap008
from agentprint.checks.ap008_readable_by_others import CHECK as AP008
from agentprint.models import Artifact, Kind, Severity
from agentprint.scanner import scan
from tests import fakes

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, non-root"
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A folder tree whose parents are judged only up to tmp_path (the real parents of a test
    temp dir are private, which would hide every exposure)."""
    top = tmp_path.resolve()
    root = top / "box"
    root.mkdir()
    for folder in (top, root):
        folder.chmod(0o755)

    def parents(path):
        return [p for p in path.parents if p == top or top in p.parents]

    monkeypatch.setattr(ap008, "_parents", parents)
    return root


def _file(path, text="x\n", mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _open(*folders):
    for folder in folders:
        folder.chmod(0o755)


def _home(agent, kind, path, home):
    return HomeArtifact(agent, kind, path, home=home)


def _shell_safe(fix):
    assert subprocess.run(["bash", "-n", "-c", fix], capture_output=True).returncode == 0, fix
    shlex.split(fix)  # raises on unbalanced quotes
    assert "$(" not in fix and "`" not in fix, fix


def test_ap008_is_registered():
    assert AP008 in SCAN_CHECKS
    assert (AP008.id, AP008.severity) == ("AP008", Severity.MEDIUM)


def test_agent_folder_is_fixed_with_one_short_recursive_command(box):
    hermes = box / ".hermes"
    sessions = [_file(hermes / "sessions" / f"s{i}.json") for i in range(8)]
    env = _file(hermes / ".env", f"KEY={fakes.OPENAI}\n")
    _open(hermes, hermes / "sessions")
    artifacts = [_home("hermes", Kind.HISTORY, s, box) for s in sessions]
    artifacts.append(_home("hermes", Kind.CREDENTIALS, env, box))
    [finding] = AP008.run(artifacts)
    assert finding.location == str(hermes) and finding.count == 9
    assert finding.fix == f"chmod -R go-rwx {hermes}"
    assert finding.severity is Severity.HIGH  # a credential store is among them
    assert "group and other users" in finding.detail
    assert fakes.OPENAI not in repr(finding)


def test_file_directly_in_home_gets_a_file_command(box):
    state = _file(box / ".claude.json")
    [finding] = AP008.run([_home("claude-code", Kind.SETTINGS, state, box)])
    assert finding.location == str(state)
    assert finding.fix == f"chmod go-rwx {state}"


def test_fix_keeps_the_owner_execute_bit(box):
    script = _file(box / ".copilot" / "hooks" / "guard.sh", "#!/bin/sh\nexit 0\n", mode=0o755)
    _open(box / ".copilot", box / ".copilot" / "hooks")
    [finding] = AP008.run([_home("copilot", Kind.SETTINGS, script, box)])
    assert "600" not in finding.fix and "go-rwx" in finding.fix
    subprocess.run(["bash", "-c", finding.fix], check=True)
    assert stat.S_IMODE(script.stat().st_mode) == 0o700  # a hook script still runs


HOSTILE = ["a$(touch PWNED)b", "q'uote", 'd"q', "back\\slash", "tick`id`", "sp ace", "nl\nx"]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_never_reach_a_command(box, name):
    # 1) hostile names inside a safe agent folder: the folder command doesn't name them.
    agent = box / ".agent"
    inner = [_file(agent / "sessions" / f"{name}{i}.json") for i in range(3)]
    # 2) a home folder with a hostile name, and 3) a project with a hostile name.
    odd_home = box / f"home {name}"
    odd = _file(odd_home / ".agent" / "history.jsonl")
    project = box / f"proj {name}"
    personal = _file(project / ".claude" / "settings.local.json")
    _open(agent, agent / "sessions", odd_home, odd_home / ".agent", project, project / ".claude")
    artifacts = [_home("x", Kind.HISTORY, p, box) for p in inner]
    artifacts.append(_home("x", Kind.HISTORY, odd, odd_home))
    artifacts.append(Artifact("claude-code", Kind.SETTINGS, personal, personal=True))
    findings = AP008.run(artifacts)
    assert len(findings) == 3
    for finding in findings:
        _shell_safe(finding.fix)
    assert any(f.fix == f"chmod -R go-rwx {agent}" for f in findings)
    assert not (box / "PWNED").exists()


def test_shared_project_config_is_not_checked(box):
    project = box / "proj"
    for rel in (".mcp.json", ".claude/settings.json", ".github/hooks/h.json", ".vscode/mcp.json"):
        _file(project / rel, "{}\n")
    _file(project / "opencode.json", "{}\n")
    local = _file(project / ".claude" / "settings.local.json", "{}\n")
    for folder in (project, *[p for p in project.rglob("*") if p.is_dir()]):
        folder.chmod(0o755)
    findings = [f for f in scan(None, project) if f.check_id == "AP008"]
    assert [(f.location, f.fix) for f in findings] == [(str(local), f"chmod go-rwx {local}")]


def test_file_that_vanishes_mid_scan_is_skipped(box, monkeypatch):
    target = _file(box / ".agent" / "history.jsonl")
    _open(box / ".agent")
    calls = {"n": 0}
    real_stat = pathlib.Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self == target:
            calls["n"] += 1
            if calls["n"] > 1:  # rotated away after the first look
                raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)
    findings = AP008.run([_home("x", Kind.HISTORY, target, box)])
    assert all(f.location for f in findings)  # no crash; at most one finding


def test_missing_and_dangling_files_are_fine(box):
    (box / ".agent").mkdir()
    dangling = box / ".agent" / "gone.jsonl"
    dangling.symlink_to(box / "nowhere")
    artifacts = [
        _home("x", Kind.CREDENTIALS, box / "missing.json", box),
        _home("x", Kind.HISTORY, dangling, box),
    ]
    assert AP008.run(artifacts) == []


def test_symlinked_file_is_judged_by_its_target_folders(box):
    private, public = box / "private", box / ".agent"
    target = _file(private / "history.jsonl")
    public.mkdir()
    private.chmod(0o700)
    public.chmod(0o755)
    (public / "history.jsonl").symlink_to(target)
    hidden = _home("x", Kind.HISTORY, public / "history.jsonl", box)
    assert AP008.run([hidden]) == []

    exposed_target = _file(public / "real.jsonl")
    (private / "link.jsonl").symlink_to(exposed_target)
    [finding] = AP008.run([_home("x", Kind.HISTORY, private / "link.jsonl", box)])
    assert "other users" in finding.detail


def test_group_member_needs_group_bits_on_a_same_group_folder(box):
    folder = box / ".agent"
    f = _file(folder / "h.jsonl", mode=0o640)
    folder.chmod(0o701)  # others may enter, the group may not
    assert AP008.run([_home("x", Kind.HISTORY, f, box)]) == []
    folder.chmod(0o710)
    [finding] = AP008.run([_home("x", Kind.HISTORY, f, box)])
    assert "readable by its group" in finding.detail


def test_private_file_or_private_folder_is_fine(box):
    private = _file(box / ".a" / "auth.json", mode=0o600)
    hidden = _file(box / ".b" / "auth.json", mode=0o644)
    (box / ".a").chmod(0o755)
    (box / ".b").chmod(0o700)
    artifacts = [_home("codex", Kind.CREDENTIALS, p, box) for p in (private, hidden)]
    assert AP008.run(artifacts) == []


def test_severity_comes_from_the_kind_without_rereading_files(box):
    history = _file(box / ".agent" / "s.jsonl", f"key {fakes.GITHUB}\n")
    _open(box / ".agent")
    [finding] = AP008.run([_home("x", Kind.HISTORY, history, box)])
    assert finding.severity is Severity.MEDIUM  # the key itself is AP001's finding


def test_each_parent_folder_is_looked_at_once(box, monkeypatch):
    sessions = box / ".agent" / "sessions"
    files = [_file(sessions / f"s{i}.jsonl") for i in range(20)]
    _open(box / ".agent", sessions)
    seen = {"n": 0}
    real_stat = pathlib.Path.stat

    def counting_stat(self, *args, **kwargs):
        if self == sessions:
            seen["n"] += 1
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", counting_stat)
    AP008.run([_home("x", Kind.HISTORY, f, box) for f in files])
    assert seen["n"] <= 1


def test_sqlite_write_ahead_log_is_checked_too(box):
    db = box / ".hermes" / "state.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE m (t TEXT)")
    con.commit()
    con.close()
    db.chmod(0o600)
    _file(box / ".hermes" / "state.db-wal", "log\n", mode=0o644)
    _open(db.parent)
    [finding] = AP008.run([_home("hermes", Kind.HISTORY, db, box)])
    assert "state.db-wal" in finding.detail


def test_windows_is_skipped(box, monkeypatch):
    f = _file(box / ".w" / "auth.json")
    _open(box / ".w")
    monkeypatch.setattr(ap008, "_is_windows", lambda: True)
    assert AP008.run([_home("codex", Kind.CREDENTIALS, f, box)]) == []


def test_hermes_home_with_default_modes_end_to_end(box):
    # Recreate the CVE-2026-53870 situation: Hermes files created world-readable.
    hermes = box / ".hermes"
    _file(hermes / ".env", f"OPENROUTER_API_KEY={fakes.OPENAI}\n")
    _file(hermes / "memories" / "MEMORY.md", "notes\n")
    _open(hermes, hermes / "memories")
    [finding] = [f for f in scan(box, None) if f.check_id == "AP008"]
    assert (finding.location, finding.severity, finding.count) == (str(hermes), Severity.HIGH, 2)
    assert finding.fix == f"chmod -R go-rwx {hermes}"
