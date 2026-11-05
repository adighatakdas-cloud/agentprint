import json

import pytest

from tests import fakes
from tests.test_ecoscan_registries import LEAKY, _http, _npm_web
from tools.ecoscan.__main__ import main
from tools.ecoscan.detect import new_record
from tools.ecoscan.run import run
from tools.ecoscan.summary import summarize


class Enumerator:
    def __init__(self, names):
        self.names, self.calls = names, 0

    def __call__(self, http, n, tally):
        self.calls += 1
        tally.add(1000)
        return [{"name": name} for name in self.names[:n]]


def _ok_scan(http, target, tally, *, validate=False):
    tally.add(10)
    return new_record("npm", target["name"], "1.0")


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _quiet(**kw):
    return {"log": lambda m: None, **kw}


def test_run_writes_targets_results_and_sessions_in_the_private_folder(tmp_path):
    enum = Enumerator(["p0", "p1", "p2"])
    session = run("npm", tmp_path, 3, None, **_quiet(workers=2, recent=enum, scan=_ok_scan))
    assert session["processed"] == 3 and not session["interrupted"]
    private = tmp_path / "private"
    assert [t["name"] for t in _lines(private / "targets-npm.jsonl")] == ["p0", "p1", "p2"]
    results = _lines(private / "results-npm.jsonl")
    assert sorted(r["name"] for r in results) == ["p0", "p1", "p2"]
    assert all(r["bytes"] == 10 and r["requests"] == 1 for r in results)
    meta = json.loads((private / "meta-npm.json").read_text())
    assert meta["enumeration_bytes"] == 1000
    assert "Do not post" in (private / "README.txt").read_text()


def test_interrupted_run_resumes_without_redoing_work(tmp_path):
    enum = Enumerator([f"p{i}" for i in range(5)])

    def flaky(http, target, tally, *, validate=False):
        if target["name"] == "p2":
            raise KeyboardInterrupt
        return _ok_scan(http, target, tally)

    first = run("npm", tmp_path, 5, None, **_quiet(workers=1, recent=enum, scan=flaky))
    assert first["interrupted"]
    results = tmp_path / "private" / "results-npm.jsonl"
    done = {r["name"] for r in _lines(results)}
    assert {"p0", "p1"} <= done and "p2" not in done

    seen = []

    def record(http, target, tally, *, validate=False):
        seen.append(target["name"])
        return _ok_scan(http, target, tally)

    second = run("npm", tmp_path, 5, None, **_quiet(workers=1, recent=enum, scan=record))
    assert not second["interrupted"]
    assert enum.calls == 1  # the target list is reused, not fetched again
    assert sorted(seen) == sorted({f"p{i}" for i in range(5)} - done)
    assert sorted(r["name"] for r in _lines(results)) == [f"p{i}" for i in range(5)]
    assert len(_lines(tmp_path / "private" / "sessions-npm.jsonl")) == 2


def test_half_written_result_line_is_ignored_on_resume(tmp_path):
    enum = Enumerator(["p0", "p1"])
    run("npm", tmp_path, 2, None, **_quiet(workers=1, recent=enum, scan=_ok_scan))
    results = tmp_path / "private" / "results-npm.jsonl"
    first_line = results.read_text().splitlines()[0]
    results.write_text(first_line + "\n" + '{"key": "p1", "na')  # killed mid-write
    seen = []

    def record(http, target, tally, *, validate=False):
        seen.append(target["name"])
        return _ok_scan(http, target, tally)

    run("npm", tmp_path, 2, None, **_quiet(workers=1, recent=enum, scan=record))
    assert len(seen) == 1


def test_redo_rescans_only_packages_with_the_given_status(tmp_path):
    enum = Enumerator(["p0", "p1", "p2"])

    def first(http, target, tally, *, validate=False):
        rec = _ok_scan(http, target, tally)
        if target["name"] == "p1":
            rec["status"] = "skipped"
        return rec

    run("npm", tmp_path, 3, None, **_quiet(workers=1, recent=enum, scan=first))
    seen = []

    def again(http, target, tally, *, validate=False):
        seen.append(target["name"])
        return _ok_scan(http, target, tally)

    run("npm", tmp_path, 3, None, **_quiet(recent=enum, scan=again, redo={"skipped"}))
    assert seen == ["p1"]
    assert summarize(tmp_path)["npm"]["status"] == {"ok": 3}  # the newest line per package wins


def test_scan_errors_are_recorded_not_raised(tmp_path):
    def broken(http, target, tally, *, validate=False):
        raise KeyError("dist")

    run("npm", tmp_path, 1, None, **_quiet(recent=Enumerator(["p0"]), scan=broken))
    [rec] = _lines(tmp_path / "private" / "results-npm.jsonl")
    assert (rec["status"], rec["reason"]) == ("error", "KeyError: 'dist'")


def test_end_to_end_output_never_holds_a_full_key(tmp_path):
    files = {"index.js": b"1", ".claude/settings.local.json": LEAKY}
    http = _http(_npm_web(files, files))
    run("npm", tmp_path, 1, http, **_quiet(recent=Enumerator(["demo"])))
    assert main(["summary", "--out", str(tmp_path)]) == 0
    assert summarize(tmp_path)["npm"]["with_secrets"] == 1
    blobs = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert fakes.ANTHROPIC not in blobs
    assert fakes.ANTHROPIC[:4] + "…(len" in blobs  # the redacted form is what is kept


def test_run_command_skips_about_an_hour_of_newest_changes(tmp_path, monkeypatch):
    seen = {}

    def fake_run(eco, out, n, http, **kw):
        seen.update(eco=eco, n=n, recent=kw["recent"], rates=http.limiter.rates)
        return {"interrupted": False}

    monkeypatch.setattr("tools.ecoscan.__main__.run", fake_run)
    assert main(["run", "npm", "--limit", "5", "--out", str(tmp_path)]) == 0
    assert seen["recent"].keywords == {"skip_newest": 15_000}
    assert seen["rates"]["data.jsdelivr.com"] <= 100 / 60  # jsDelivr's fair-use line
    main(["run", "pypi", "--limit", "5", "--out", str(tmp_path), "--skip-newest", "0"])
    assert seen["recent"].keywords == {"skip_newest": 0}


def test_request_rates_are_locked(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(eco, out, n, http, **kw):
        seen.update(default=http.limiter.default, rates=http.limiter.rates)
        return {"interrupted": False}

    monkeypatch.setattr("tools.ecoscan.__main__.run", fake_run)
    with pytest.raises(SystemExit):  # there is no way to ask for a faster rate
        main(["run", "npm", "--limit", "5", "--out", str(tmp_path), "--rate", "5"])
    main(["run", "npm", "--limit", "5", "--out", str(tmp_path)])
    assert seen["default"] == 4.0
    assert seen["rates"] == {"data.jsdelivr.com": 1.6, "replicate.npmjs.com": 1.0}
