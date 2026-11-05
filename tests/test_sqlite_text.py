import json
import sqlite3

from agentprint import sqlite_text
from agentprint.checks.ap001_history_secret import CHECK as AP001
from agentprint.files import iter_lines
from agentprint.models import Artifact, Kind
from tests import fakes


def _db(path, rows, table="cursorDiskKV"):
    con = sqlite3.connect(path)
    con.execute(f'CREATE TABLE "{table}" (key TEXT, value BLOB)')
    con.executemany(f'INSERT INTO "{table}" VALUES (?, ?)', rows)
    con.commit()
    con.close()
    return path


def test_text_and_blob_values_are_scanned(tmp_path):
    bubble = json.dumps({"text": f"here is my key {fakes.ANTHROPIC}"}).encode()
    db = _db(
        tmp_path / "state.vscdb",
        [("bubbleId:a:b", bubble), ("composerData:x", f"use {fakes.GITHUB}")],
    )
    found = AP001.run([Artifact("cursor", Kind.HISTORY, db)])
    assert sorted(f.detail.split(":")[0] for f in found) == ["Anthropic API key", "GitHub token"]
    assert all(f.line is None for f in found)  # rows have no line number
    assert fakes.ANTHROPIC not in repr(found)


def test_binary_blob_is_skipped(tmp_path):
    db = _db(tmp_path / "x.db", [("img", b"\x89PNG\x00\x00" + fakes.AWS.encode())])
    assert [line for _, line in iter_lines(db) if fakes.AWS in line] == []


def test_invalid_utf8_text_does_not_crash(tmp_path):
    db = _db(tmp_path / "x.db", [("k", b"caf\xe9 " + fakes.SLACK.encode())])
    assert any(fakes.SLACK in line for _, line in iter_lines(db))


def test_corrupt_database_yields_nothing(tmp_path):
    db = tmp_path / "broken.vscdb"
    db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    assert list(iter_lines(db)) == []


def test_reading_never_modifies_the_database(tmp_path):
    db = _db(tmp_path / "state.vscdb", [("k", "v")])
    before = (db.read_bytes(), sorted(p.name for p in tmp_path.iterdir()))
    list(iter_lines(db))
    assert (db.read_bytes(), sorted(p.name for p in tmp_path.iterdir())) == before


def test_total_size_cap_stops_reading(tmp_path, monkeypatch):
    rows = [(str(i), "x" * 1000) for i in range(50)] + [("last", fakes.GITHUB)]
    db = _db(tmp_path / "big.db", rows)
    monkeypatch.setattr(sqlite_text, "MAX_TOTAL", 10_000)
    assert not any(fakes.GITHUB in line for _, line in iter_lines(db))


def test_lines_are_split_and_capped(tmp_path, monkeypatch):
    db = _db(tmp_path / "x.db", [("k", "one\ntwo\n" + "y" * 100)])
    monkeypatch.setattr(sqlite_text, "MAX_VALUE", 20)
    assert [line for _, line in iter_lines(db)] == ["k", "one", "two", "y" * 12]


def test_views_are_not_queried_and_odd_table_names_work(tmp_path):
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE "we""ird name" (v TEXT)')
    con.execute('INSERT INTO "we""ird name" VALUES (?)', (fakes.GOOGLE,))
    con.execute('CREATE VIEW v AS SELECT nosuchfn(v) FROM "we""ird name"')
    con.commit()
    con.close()
    assert any(fakes.GOOGLE in line for _, line in iter_lines(db))
