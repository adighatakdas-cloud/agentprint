"""AP008 and the fix lock on Windows: findings with the icacls recipe, "could not check" notes,
the journal's saved access list and --undo.

A pretend Windows (tests/fakewin.py) keeps access lists in a dict, so all but the last test run
on every OS; the `windows_only` one does the same on real NTFS with real icacls. Fake homes
under tmp_path only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import advice, fix, fixreport, platform, winacl
from agentprint.agents.base import HomeArtifact
from agentprint.checks import ap008_readable_by_others as ap008
from agentprint.checks.ap008_readable_by_others import CHECK as AP008
from agentprint.cli import app
from agentprint.models import Artifact, Kind, Severity
from agentprint.sanitize import quote
from agentprint.scanner import scan
from tests.fakewin import (
    FULL,
    INHERITED,
    ME,
    PRIVATE,
    READ_EXECUTE,
    SOMEONE,
    FakeWindows,
    allow,
    deny,
)

EVERYONE, USERS = winacl.EVERYONE, winacl.USERS
EXPOSED = (allow(EVERYONE), *PRIVATE)  # someone ran: icacls f /grant *S-1-1-0:R
SHARED = (allow(USERS, READ_EXECUTE, INHERITED), *PRIVATE)  # a folder that lets Users read


@pytest.fixture
def win(monkeypatch) -> FakeWindows:
    return FakeWindows().install(monkeypatch)


def _file(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _home(agent: str, kind: Kind, path: Path, home: Path) -> HomeArtifact:
    return HomeArtifact(agent, kind, path, home=home)


# ---- the check ------------------------------------------------------------------------------


def test_a_file_everyone_can_read_gets_the_icacls_recipe(win, tmp_path):
    home = tmp_path / "home"
    state = _file(home / ".claude.json")
    win.set(state, EXPOSED)
    [finding] = AP008.run([_home("claude-code", Kind.SETTINGS, state, home)])
    assert finding.fix == winacl.recipe(str(state.absolute()), ME, (EVERYONE,))
    assert finding.fix.endswith("/remove:g *S-1-1-0") and "chmod" not in finding.fix
    assert finding.detail == "settings file is readable by other users (access list: Everyone)"
    assert (finding.severity, finding.paths) == (Severity.MEDIUM, (state,))
    assert platform.scan_notes() == ()


def test_inherited_access_needs_no_remove(win, tmp_path):
    personal = _file(tmp_path / "proj" / ".claude" / "settings.local.json")
    win.set(personal, SHARED)
    [finding] = AP008.run([Artifact("claude-code", Kind.SETTINGS, personal, personal=True)])
    assert finding.fix == winacl.recipe(str(personal.absolute()), ME)
    assert "(access list: Users)" in finding.detail


def test_an_agent_folder_points_at_agentprint_fix(win, tmp_path):
    home = tmp_path / "home"
    hermes = home / ".hermes"
    files = [_file(hermes / "sessions" / f"s{i}.json") for i in range(3)]
    env = _file(hermes / ".env", "KEY=x\n")
    for path in (*files, env):
        win.set(path, SHARED)
    artifacts = [_home("hermes", Kind.HISTORY, p, home) for p in files]
    artifacts.append(_home("hermes", Kind.CREDENTIALS, env, home))
    [finding] = AP008.run(artifacts)
    assert (finding.location, finding.count, finding.severity) == (str(hermes), 4, Severity.HIGH)
    assert finding.fix.startswith("Make the files here private to you: run agentprint fix --only")
    assert winacl.recipe("<file>", ME) in finding.fix and "chmod" not in finding.fix
    assert "4 agent files here are readable by other users" in finding.detail


def test_the_only_file_of_an_agent_folder_gets_its_own_recipe(win, tmp_path):
    home = tmp_path / "home"
    history = _file(home / ".claude" / "history.jsonl")
    win.set(history, EXPOSED)
    [finding] = AP008.run([_home("claude-code", Kind.HISTORY, history, home)])
    assert finding.location == str(home / ".claude")
    assert finding.fix == winacl.recipe(str(history.absolute()), ME, (EVERYONE,))
    odd = _file(home / ".agent" / "h$x.jsonl")  # a hostile name: no path in the command
    win.set(odd, EXPOSED)
    [finding] = AP008.run([_home("x", Kind.HISTORY, odd, home)])
    assert "$" not in finding.fix and finding.fix.startswith("Make the files here private")


def test_private_files_and_other_users_files_are_not_reported(win, tmp_path):
    home = tmp_path / "home"
    private = _file(home / ".a" / "auth.json")  # its mode (0644 here) means nothing on Windows
    admins = _file(home / ".b" / "auth.json")
    theirs = _file(home / ".c" / "auth.json")
    win.set(private, PRIVATE)
    win.set(admins, (allow(winacl.SYSTEM, FULL), allow(winacl.ADMINISTRATORS, FULL)))
    win.set(theirs, EXPOSED, owner=SOMEONE)
    artifacts = [_home("codex", Kind.CREDENTIALS, p, home) for p in (private, admins, theirs)]
    assert AP008.run(artifacts) == []
    assert platform.scan_notes() == ()


def test_denied_readers_are_not_reported(win, tmp_path):
    home = tmp_path / "home"
    f = _file(home / ".a" / "history.jsonl")
    win.set(f, (deny(EVERYONE), *SHARED))
    assert AP008.run([_home("x", Kind.HISTORY, f, home)]) == []


@pytest.mark.parametrize("elevated", [True, False])
def test_files_an_elevated_run_made_count_as_mine_only_when_elevated(
    monkeypatch, tmp_path, elevated
):
    win = FakeWindows(elevated=elevated).install(monkeypatch)
    home = tmp_path / "home"
    f = _file(home / ".a" / "history.jsonl")
    win.set(f, EXPOSED, owner=winacl.ADMINISTRATORS)
    assert len(AP008.run([_home("x", Kind.HISTORY, f, home)])) == int(elevated)


WINDOWS_HOSTILE = ['d"q', "a$b", "tick`id`", "pc%PATH%t", "bang!x", "semi;x", "amp&x", "nl\nx"]


@pytest.mark.parametrize("name", WINDOWS_HOSTILE)
def test_hostile_names_never_reach_the_icacls_command(win, tmp_path, name):
    personal = _file(tmp_path / f"proj {name}" / ".claude" / "settings.local.json")
    win.set(personal, EXPOSED)
    [finding] = AP008.run([Artifact("claude-code", Kind.SETTINGS, personal, personal=True)])
    assert name not in finding.fix and "proj" not in finding.fix
    assert finding.fix == ap008._WIN_PLAIN_FILE.format(
        args=f"/inheritance:r /grant:r *{ME}:F *S-1-5-18:F *S-1-5-32-544:F /remove:g *S-1-1-0"
    )


def test_files_that_cannot_be_checked_are_named_in_the_sweep_note(win, tmp_path):
    home = tmp_path / "home"
    odd = _file(home / ".a" / "history.jsonl")
    win.set(odd, ((0x9, 0, 0x1, ""), *PRIVATE))  # a conditional entry
    assert AP008.run([_home("x", Kind.HISTORY, odd, home)]) == []
    [note] = platform.scan_notes()
    assert note.startswith("AP008 could not check who can read 1 agent file on Windows")
    assert quote(str(odd)) in note and winacl.UNUSUAL in note and "icacls" in note

    win.set(odd, PRIVATE)
    AP008.run([_home("x", Kind.HISTORY, odd, home)])
    assert platform.scan_notes() == ()  # each sweep says only what it could not check


def test_a_drive_without_permissions_and_an_unknown_user_are_not_checked(
    win, monkeypatch, tmp_path
):
    home = tmp_path / "home"
    files = [_file(home / ".a" / f"h{i}.jsonl") for i in range(2)]
    win.no_acls = True
    AP008.run([_home("x", Kind.HISTORY, f, home) for f in files])
    [note] = platform.scan_notes()
    assert "2 agent files" in note and ", the first" in note and winacl.NO_ACLS in note

    def nobody():
        raise OSError("no token")

    monkeypatch.setattr(winacl, "current_user", nobody)
    assert ap008.exposed_paths(_home("x", Kind.HISTORY, files[0], home)) == []
    AP008.run([_home("x", Kind.HISTORY, files[0], home)])
    assert winacl.NO_USER in platform.scan_notes()[0]


def test_exposed_paths_on_windows_is_what_the_check_reports(win, tmp_path):
    home = tmp_path / "home"
    f = _file(home / ".a" / "history.jsonl")
    artifact = _home("x", Kind.HISTORY, f, home)
    assert ap008.exposed_paths(artifact) == []
    win.set(f, EXPOSED)
    assert ap008.exposed_paths(artifact) == [f]
    assert ap008.exposed_paths(_home("x", Kind.HISTORY, home / ".a" / "gone.jsonl", home)) == []


def test_explain_on_windows_says_icacls_not_chmod(win):
    text = advice.explain("AP008")
    assert "icacls" in text and "chmod" not in text


def test_explain_off_windows_still_says_chmod(monkeypatch):
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    assert "chmod" in advice.explain("AP008")


# ---- the note in every output ---------------------------------------------------------------


@pytest.fixture
def odd_project(win, tmp_path) -> Path:
    root = tmp_path / "proj"
    personal = _file(root / ".claude" / "settings.local.json", "{}\n")
    win.set(personal, ((0x9, 0, 0x1, ""),))
    return root


@pytest.mark.parametrize("fmt", ["agent", "json", "table"])
def test_scan_says_which_files_could_not_be_checked(odd_project, fmt):
    out = CliRunner().invoke(app, ["scan", str(odd_project), "--no-home", "--format", fmt]).output
    if fmt == "json":
        [note] = json.loads(out)["not_checked"]
        assert note.startswith("AP008 could not check")
    elif fmt == "agent":
        assert out.splitlines()[0] == "agentprint: OK"  # line 1 stays the verdict
        assert "not_checked: AP008 could not check who can read 1 agent file on Windows" in out
    else:
        assert "not checked" in out and "AP008" in out


def test_fix_and_mcp_say_it_too(odd_project):
    from tests.test_mcp import call

    out = CliRunner().invoke(app, ["fix", str(odd_project), "--no-home", "--format", "json"])
    assert json.loads(out.output)["not_checked"][0].startswith("AP008 could not check")
    text = call("scan_secrets", path=str(odd_project))
    assert "not_checked: AP008 could not check" in text


# ---- fix: lock, journal, undo ---------------------------------------------------------------


@pytest.fixture
def exposed_home(win, tmp_path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    history = _file(home / ".claude" / "history.jsonl", '{"display": "ls"}\n')
    win.set(history, (allow(EVERYONE), allow(USERS, READ_EXECUTE, INHERITED), *PRIVATE))
    return home, history


def _ap008(home: Path) -> list:
    return [f for f in scan(home, None) if f.check_id == "AP008"]


def test_fix_locks_with_icacls_journals_the_list_and_undo_puts_it_back(win, exposed_home):
    home, history = exposed_home
    before = win.get(history)
    p = fix.plan(scan(home, None))
    [change] = p.changes
    assert (change.kind, change.shown, change.old_mode) == ("lock", fix.WINDOWS_LOCK, None)
    [row] = fixreport.rows(p.changes, p.findings)
    assert fixreport.describe(row) == f"{fix.WINDOWS_LOCK} (private to the owner)"

    outcome = fix.apply(p, home)

    assert outcome.skipped == [] and outcome.applied == [change]
    real = str(history.resolve())
    assert win.calls == [winacl.lock_args(real, ME, (EVERYONE,))]
    after = win.get(history)
    assert after.protected and winacl.classify(after.aces, {ME}) == winacl.Access()
    assert winacl.allows(after.aces, {ME}, winacl.FILE_WRITE_DATA)
    assert outcome.journal is not None
    [entry] = json.loads(outcome.journal.read_text())["changes"]
    assert entry["undo"] == {"sddl": before.sddl, "now": after.sddl}
    assert _ap008(home) == []

    restored, cannot = fix.undo(home)

    assert restored == [f"restored the permissions of {quote(str(history))}"] and cannot == []
    assert win.get(history).sddl == before.sddl
    assert _ap008(home)
    assert fix.undo(home) == ([], [])  # nothing is undone twice


def test_undo_leaves_permissions_changed_since(win, exposed_home):
    home, history = exposed_home
    fix.apply(fix.plan(scan(home, None)), home)
    win.set(history, PRIVATE)
    restored, [reason] = fix.undo(home)
    assert restored == [] and "changed again since fix" in reason
    assert win.get(history).aces == PRIVATE


def test_undo_of_a_windows_lock_elsewhere_says_where(win, exposed_home, monkeypatch):
    home, _ = exposed_home
    fix.apply(fix.plan(scan(home, None)), home)
    monkeypatch.setattr(platform, "is_windows", lambda: False)
    restored, [reason] = fix.undo(home)
    assert restored == [] and "undo it there" in reason


def _skip_reason(home: Path, before_apply) -> str:
    p = fix.plan(scan(home, None))
    before_apply()
    outcome = fix.apply(p, home)
    assert outcome.applied == [] and outcome.journal is None
    [(_, reason)] = outcome.skipped
    return reason


def test_lock_leaves_a_file_now_owned_by_someone_else(win, exposed_home):
    home, history = exposed_home
    reason = _skip_reason(home, lambda: win.set(history, EXPOSED, owner=SOMEONE))
    assert reason == fix.OTHER_OWNER == "the file belongs to another user; left as is"
    assert win.calls == []


def test_lock_on_a_drive_without_permissions(win, exposed_home):
    home, _ = exposed_home
    reason = _skip_reason(home, lambda: setattr(win, "no_acls", True))
    assert reason == fix.NO_ACLS and "cannot make the file private on this drive" in reason


def test_lock_of_a_file_already_private_or_not_checkable(win, exposed_home):
    home, history = exposed_home
    assert _skip_reason(home, lambda: win.set(history, PRIVATE)) == "the file is already private"
    win.set(history, EXPOSED)
    reason = _skip_reason(home, lambda: win.set(history, ((0x9, 0, 1, ""),)))
    assert "could not check who can read the file" in reason


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (5, "the file belongs to another user; left as is"),
        (50, "cannot make the file private on this drive"),
        (-1, "agentprint could not run icacls; left as is"),
        (1, "icacls could not change the file's permissions (exit code 1); left as is"),
    ],
)
def test_an_icacls_failure_changes_nothing(win, exposed_home, code, reason):
    home, history = exposed_home
    before = win.get(history)
    win.icacls_works, win.exit_code = False, code
    assert reason in _skip_reason(home, lambda: None)
    assert win.get(history) == before and win.restored == []


def test_a_lock_that_leaves_others_in_is_rolled_back(win, exposed_home, monkeypatch):
    home, history = exposed_home
    before = win.get(history)
    monkeypatch.setattr(winacl, "lock", lambda path, me, remove=(): win.lock(path, me))
    reason = _skip_reason(home, lambda: None)
    assert (
        reason == "the lock did not work (others could still read it); the old permissions are back"
    )
    assert win.get(history).sddl == before.sddl


def test_a_lock_that_shuts_the_owner_out_is_rolled_back(win, exposed_home, monkeypatch):
    home, history = exposed_home
    before = win.get(history)

    def shut_out(path, me, remove=()):
        win.set(Path(path), (deny(ME, winacl.FILE_WRITE_DATA), allow(ME, FULL)), protected=True)
        return 0

    monkeypatch.setattr(winacl, "lock", shut_out)
    assert "you would have lost access" in _skip_reason(home, lambda: None)
    assert win.get(history).sddl == before.sddl


def test_a_partial_icacls_failure_is_rolled_back_and_a_failed_rollback_is_said(
    win, exposed_home, monkeypatch
):
    home, history = exposed_home
    before = win.get(history)
    win.exit_code = 1  # changes the list, then fails
    reason = _skip_reason(home, lambda: None)
    assert "icacls stopped with exit code 1" in reason and win.get(history).sddl == before.sddl

    def broken(path, sddl):
        raise OSError(5, "denied")

    monkeypatch.setattr(winacl, "restore", broken)
    reason = _skip_reason(home, lambda: None)
    assert "could not be put back" in reason and "icacls" in reason


def test_lock_reports_an_unreadable_list(win, exposed_home, monkeypatch):
    home, _ = exposed_home
    p = fix.plan(scan(home, None))

    def unreadable(path):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(winacl, "read", unreadable)
    [(_, reason)] = fix.apply(p, home).skipped
    assert (
        reason
        == "agentprint could not change the file's permissions (Access is denied); left as is"
    )


def test_lock_of_a_file_gone_since_the_plan(win, exposed_home):
    home, history = exposed_home
    assert _skip_reason(home, history.unlink) == fix.GONE


def test_the_person_sees_the_access_list_not_a_mode(win, tmp_path):
    from agentprint.ui import fixview

    change = fix.Change("lock", tmp_path / "f", None, "AP008", fix.WINDOWS_LOCK)
    row = fixreport.Row("lock", "AP008", tmp_path / "f", None, (change,))
    assert fixview._what(row).plain == "access list → owner, SYSTEM and Administrators only"


def test_a_folder_of_locks_is_one_icacls_row(win, tmp_path):
    changes = [
        fix.Change("lock", tmp_path / f"f{i}", None, "AP008", fix.WINDOWS_LOCK) for i in range(2)
    ]
    row = fixreport.Row("lock", "AP008", tmp_path, None, tuple(changes))
    assert fixreport.describe(row) == "2 files made private to the owner (icacls)"


def test_cli_fix_on_windows_plans_applies_and_reports_the_lock(win, exposed_home, tmp_path):
    home, history = exposed_home
    project = tmp_path / "proj"
    project.mkdir()
    args = ["fix", str(project), "--home", str(home), "--format", "json"]
    planned = json.loads(CliRunner().invoke(app, args).output)
    assert [(c["kind"], c["shown"]) for c in planned["planned"]] == [("lock", fix.WINDOWS_LOCK)]
    assert "not_checked" not in planned
    result = CliRunner().invoke(app, [*args[:-1], "agent", "--yes"])
    assert "lock AP008" in result.output and "private to the owner" in result.output
    assert "chmod" not in result.output
    assert winacl.classify(win.get(history).aces, {ME}) == winacl.Access()


# ---- real Windows ----------------------------------------------------------------------------


@pytest.mark.windows_only
def test_real_windows_ap008_lock_and_undo(tmp_path):
    home = tmp_path / "home"
    history = _file(home / ".claude" / "history.jsonl", '{"display": "ls"}\n')
    where = str(history.resolve())
    grant = [winacl.icacls(), where, "/grant", "*S-1-1-0:R"]
    subprocess.run(grant, check=True, capture_output=True, **platform.subprocess_kwargs())
    saved = winacl.read(where).sddl

    [finding] = _ap008(home)
    assert "Everyone" in finding.detail and "/remove:g *S-1-1-0" in finding.fix
    p = fix.plan(scan(home, None))
    assert [c.kind for c in p.changes] == ["lock"]
    outcome = fix.apply(p, home)
    assert outcome.skipped == [], outcome.skipped

    me, elevated = winacl.current_user()
    assert winacl.exposure(where, me, elevated) == winacl.Access()  # Everyone is gone
    after = winacl.read(where)
    for right in (winacl.FILE_READ_DATA, winacl.FILE_WRITE_DATA):
        assert winacl.allows(after.aces, {me}, right), after.sddl  # the owner keeps access
    assert history.read_text() == '{"display": "ls"}\n'
    assert _ap008(home) == []

    restored, cannot = fix.undo(home)
    assert len(restored) == 1 and cannot == []
    assert winacl.read(where).sddl == saved
    assert _ap008(home)
