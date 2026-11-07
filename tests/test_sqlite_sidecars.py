"""Reading an agent's database creates nothing next to it (no -wal/-shm sidecars), and a
database that can't be read is named in the scan's not_checked notes, never skipped silently."""

import json
import os
import sqlite3

import pytest

from agentprint import platform, sqlite_text
from agentprint.cli import app
from agentprint.files import iter_lines
from agentprint.scanner import scan
from tests import fakes
from tests.fixtures import agent_homes as homes


def _wal_db(path, rows):
    """A WAL-mode database, closed cleanly (SQLite then removes its -wal and -shm files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    con.execute("CREATE TABLE part (id TEXT, data TEXT)")
    con.executemany("INSERT INTO part VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return path


def test_a_closed_wal_database_is_read_without_creating_sidecars(tmp_path):
    home = tmp_path / "home"
    db = _wal_db(home / ".local" / "share" / "kilo" / "kilo.db", [("p1", f"key {fakes.GITHUB}")])
    before = sorted(os.listdir(db.parent))
    assert before == ["kilo.db"]
    found = [f for f in scan(home, None) if f.check_id == "AP001"]
    assert [f.location for f in found] == [str(db)]
    assert sorted(os.listdir(db.parent)) == before
    assert not [n for n in platform.scan_notes() if "Could not read" in n]


def test_a_database_in_use_is_still_read_through_its_wal(tmp_path):
    db = _wal_db(tmp_path / "state.db", [("p1", "hello")])
    writer = sqlite3.connect(db)  # the agent is running: its sidecars exist
    writer.execute("INSERT INTO part VALUES ('p2', ?)", (f"later {fakes.SLACK}",))
    writer.commit()
    try:
        assert (tmp_path / "state.db-wal").exists()
        lines = [line for _, line in iter_lines(db)]
        assert any(fakes.SLACK in line for line in lines)  # the row only the WAL holds yet
    finally:
        writer.close()


def test_a_locked_database_is_named_in_not_checked(tmp_path, monkeypatch):
    home = tmp_path / "home"
    homes.kilo(home)

    def locked(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite_text, "_connect", locked)
    findings = scan(home, None)
    assert not [f for f in findings if f.check_id == "AP001" and f.location.endswith("kilo.db")]
    notes = [n for n in platform.scan_notes() if "Could not read" in n]
    assert len(notes) == 1 and "kilo.db" in notes[0] and "(locked)" in notes[0]
    assert fakes.ANTHROPIC not in notes[0]

    from typer.testing import CliRunner

    runner = CliRunner()
    (tmp_path / "proj").mkdir()
    agent = runner.invoke(app, ["scan", str(tmp_path / "proj"), "--home", str(home), "-f", "agent"])
    assert any(
        ln.startswith("not_checked: Could not read") and "(locked)" in ln
        for ln in agent.output.splitlines()
    )
    data = json.loads(
        runner.invoke(
            app, ["scan", str(tmp_path / "proj"), "--home", str(home), "-f", "json"]
        ).output
    )
    assert any("(locked)" in n for n in data["not_checked"])


def test_notes_are_forgotten_by_the_next_scan(tmp_path, monkeypatch):
    home = tmp_path / "home"
    homes.kilo(home)
    monkeypatch.setattr(
        sqlite_text, "_connect", lambda p: (_ for _ in ()).throw(sqlite3.DatabaseError("x"))
    )
    scan(home, None)
    assert [n for n in platform.scan_notes() if "Could not read" in n]
    monkeypatch.undo()
    scan(home, None)
    assert not [n for n in platform.scan_notes() if "Could not read" in n]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes, non-root")
def test_a_history_file_it_may_not_open_is_named_too(tmp_path):
    home = tmp_path / "home"
    homes.kilo(home)
    db = home / ".local" / "share" / "kilo" / "kilo.db"
    db.chmod(0o000)
    try:
        scan(home, None)
        notes = [n for n in platform.scan_notes() if "Could not read" in n]
    finally:
        db.chmod(0o600)
    assert len(notes) == 1 and "kilo.db" in notes[0] and "(permission denied)" in notes[0]


def test_a_file_gone_since_it_was_found_is_not_a_note(tmp_path):
    missing = tmp_path / "gone.jsonl"
    assert list(iter_lines(missing)) == []
    from agentprint import unread

    unread.forget()
    list(iter_lines(missing))
    assert unread.notes() == ()
