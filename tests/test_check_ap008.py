import os
import shlex
import sqlite3
import sys

import pytest

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
    root = tmp_path / "box"
    root.mkdir()
    for folder in (tmp_path, root):
        folder.chmod(0o755)

    def parents(path):
        return [p for p in path.parents if p == tmp_path or tmp_path in p.parents]

    monkeypatch.setattr(ap008, "_parents", parents)
    return root


def _file(path, text="x\n", mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_ap008_is_registered():
    assert AP008 in SCAN_CHECKS
    assert (AP008.id, AP008.severity) == ("AP008", Severity.MEDIUM)


def test_world_readable_credential_store_is_high_with_exact_chmod(box):
    env = _file(box / "my agent" / ".env", f"KEY={fakes.OPENAI}\n")
    (box / "my agent").chmod(0o755)
    [finding] = AP008.run([Artifact("hermes", Kind.CREDENTIALS, env)])
    assert finding.severity is Severity.HIGH and finding.check_id == "AP008"
    assert finding.location == str(env) and finding.line is None
    assert "group and other users" in finding.detail and "0644" in finding.detail
    assert finding.fix == f"chmod 600 {shlex.quote(str(env))}"
    assert fakes.OPENAI not in repr(finding)


def test_private_file_or_private_folder_is_fine(box):
    private = _file(box / "a" / "auth.json", mode=0o600)
    (box / "a").chmod(0o755)
    hidden = _file(box / "b" / "auth.json", mode=0o644)
    (box / "b").chmod(0o700)
    artifacts = [Artifact("codex", Kind.CREDENTIALS, p) for p in (private, hidden)]
    assert AP008.run(artifacts) == []


def test_group_only_exposure(box):
    f = _file(box / "g" / "history.jsonl", mode=0o640)
    (box / "g").chmod(0o750)
    [finding] = AP008.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert "readable by its group" in finding.detail
    assert finding.severity is Severity.MEDIUM


def test_history_with_a_key_is_high(box):
    f = _file(box / "h" / "s.jsonl", f"key {fakes.GITHUB}\n")
    (box / "h").chmod(0o755)
    [finding] = AP008.run([Artifact("claude-code", Kind.HISTORY, f)])
    assert finding.severity is Severity.HIGH


def test_many_files_in_one_folder_are_one_finding(box):
    folder = box / "sessions"
    files = [_file(folder / f"s{i}.jsonl") for i in range(8)]
    folder.chmod(0o755)
    [finding] = AP008.run([Artifact("hermes", Kind.HISTORY, f) for f in files])
    assert finding.location == str(folder) and finding.count == 8
    assert (
        finding.fix == f"find {shlex.quote(str(folder))} -maxdepth 1 -type f -exec chmod 600 {{}} +"
    )


def test_a_few_files_in_one_folder_are_listed(box):
    files = [_file(box / "p" / name) for name in (".aider.chat.history.md", ".aider.conf.yml")]
    (box / "p").chmod(0o755)
    [finding] = AP008.run([Artifact("aider", Kind.HISTORY, f) for f in files])
    assert finding.fix == "chmod 600 " + " ".join(shlex.quote(str(f)) for f in files)


def test_sqlite_write_ahead_log_is_checked_too(box):
    db = box / "hermes" / "state.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE m (t TEXT)")
    con.commit()
    con.close()
    db.chmod(0o600)
    wal = _file(box / "hermes" / "state.db-wal", "log\n", mode=0o644)
    db.parent.chmod(0o755)
    [finding] = AP008.run([Artifact("hermes", Kind.HISTORY, db)])
    assert finding.location == str(wal)


def test_windows_is_skipped(box, monkeypatch):
    f = _file(box / "w" / "auth.json")
    (box / "w").chmod(0o755)
    monkeypatch.setattr(ap008, "_is_windows", lambda: True)
    assert AP008.run([Artifact("codex", Kind.CREDENTIALS, f)]) == []


def test_missing_file_is_fine(box):
    assert AP008.run([Artifact("codex", Kind.CREDENTIALS, box / "gone.json")]) == []


def test_hermes_home_with_default_modes_end_to_end(box):
    # Recreate the CVE-2026-53870 situation: Hermes files created world-readable.
    hermes = box / ".hermes"
    _file(hermes / ".env", f"OPENROUTER_API_KEY={fakes.OPENAI}\n")
    _file(hermes / "memories" / "MEMORY.md", "notes\n")
    for folder in (hermes, hermes / "memories"):
        folder.chmod(0o755)
    found = {
        (f.location.rsplit("/", 1)[1], f.severity) for f in scan(box, None) if f.check_id == "AP008"
    }
    assert found == {(".env", Severity.HIGH), ("MEMORY.md", Severity.MEDIUM)}
