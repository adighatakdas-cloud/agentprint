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


def test_run_writes_targets_results_and_sessions(tmp_path):
    enum = Enumerator(["p0", "p1", "p2"])
    session = run(
        "npm", tmp_path, 3, None, workers=2, recent=enum, scan=_ok_scan, log=lambda m: None
    )
    assert session["processed"] == 3 and not session["interrupted"]
    assert [t["name"] for t in _lines(tmp_path / "targets-npm.jsonl")] == ["p0", "p1", "p2"]
    results = _lines(tmp_path / "results-npm.jsonl")
    assert sorted(r["name"] for r in results) == ["p0", "p1", "p2"]
    assert all(r["bytes"] == 10 and r["requests"] == 1 for r in results)
    meta = json.loads((tmp_path / "meta-npm.json").read_text())
    assert meta["enumeration_bytes"] == 1000


def test_interrupted_run_resumes_without_redoing_work(tmp_path):
    enum = Enumerator([f"p{i}" for i in range(5)])

    def flaky(http, target, tally, *, validate=False):
        if target["name"] == "p2":
            raise KeyboardInterrupt
        return _ok_scan(http, target, tally)

    first = run("npm", tmp_path, 5, None, workers=1, recent=enum, scan=flaky, log=lambda m: None)
    assert first["interrupted"]
    done = {r["name"] for r in _lines(tmp_path / "results-npm.jsonl")}
    assert {"p0", "p1"} <= done and "p2" not in done

    seen = []

    def record(http, target, tally, *, validate=False):
        seen.append(target["name"])
        return _ok_scan(http, target, tally)

    second = run("npm", tmp_path, 5, None, workers=1, recent=enum, scan=record, log=lambda m: None)
    assert not second["interrupted"]
    assert enum.calls == 1  # the target list is reused, not fetched again
    assert sorted(seen) == sorted({f"p{i}" for i in range(5)} - done)
    names = [r["name"] for r in _lines(tmp_path / "results-npm.jsonl")]
    assert sorted(names) == [f"p{i}" for i in range(5)]
    assert len(_lines(tmp_path / "sessions-npm.jsonl")) == 2


def test_half_written_result_line_is_ignored_on_resume(tmp_path):
    enum = Enumerator(["p0", "p1"])
    run("npm", tmp_path, 2, None, workers=1, recent=enum, scan=_ok_scan, log=lambda m: None)
    results = tmp_path / "results-npm.jsonl"
    first_line = results.read_text().splitlines()[0]
    results.write_text(first_line + "\n" + '{"key": "p1", "na')  # killed mid-write
    seen = []

    def record(http, target, tally, *, validate=False):
        seen.append(target["name"])
        return _ok_scan(http, target, tally)

    run("npm", tmp_path, 2, None, workers=1, recent=enum, scan=record, log=lambda m: None)
    assert len(seen) == 1


def test_redo_rescans_only_packages_with_the_given_status(tmp_path):
    enum = Enumerator(["p0", "p1", "p2"])

    def first(http, target, tally, *, validate=False):
        rec = _ok_scan(http, target, tally)
        if target["name"] == "p1":
            rec["status"] = "skipped"
        return rec

    run("npm", tmp_path, 3, None, workers=1, recent=enum, scan=first, log=lambda m: None)
    seen = []

    def again(http, target, tally, *, validate=False):
        seen.append(target["name"])
        return _ok_scan(http, target, tally)

    run("npm", tmp_path, 3, None, recent=enum, scan=again, redo={"skipped"}, log=lambda m: None)
    assert seen == ["p1"]
    assert summarize(tmp_path)["npm"]["status"] == {"ok": 3}  # the newest line per package wins


def test_scan_errors_are_recorded_not_raised(tmp_path):
    def broken(http, target, tally, *, validate=False):
        raise KeyError("dist")

    run("npm", tmp_path, 1, None, recent=Enumerator(["p0"]), scan=broken, log=lambda m: None)
    [rec] = _lines(tmp_path / "results-npm.jsonl")
    assert (rec["status"], rec["reason"]) == ("error", "KeyError: 'dist'")


def test_end_to_end_output_never_holds_a_full_key(tmp_path):
    files = {"index.js": b"1", ".claude/settings.local.json": LEAKY}
    http = _http(_npm_web(files, files))
    run("npm", tmp_path, 1, http, recent=Enumerator(["demo"]), log=lambda m: None)
    summary = summarize(tmp_path)
    assert summary["npm"]["with_secrets"] == 1
    blobs = "".join(p.read_text() for p in tmp_path.iterdir())
    assert fakes.ANTHROPIC not in blobs
    assert fakes.ANTHROPIC[:4] + "…(len" in blobs  # the redacted form is what is kept


def _rec(name, status="ok", candidates=(), secrets=(), **extra):
    rec = new_record("npm", name, "1.0")
    rec.update(status=status, key=name, bytes=1000, requests=2, seconds=1.0, files=10, **extra)
    rec["candidates"] = [
        {"archive": "tarball", "path": p, "agent": a, "kind": k, "ap006": ap}
        for p, a, k, ap in candidates
    ]
    rec["candidate_count"] = len(rec["candidates"])
    rec["secrets"] = [
        {
            "archive": "tarball",
            "path": p,
            "agent": a,
            "kind": k,
            "ap006": ap,
            "line": 1,
            "rule": r,
            "redacted": "xxxx…(len 40)",
            "count": 1,
        }
        for p, a, k, ap, r in secrets
    ]
    return rec


def test_summary_counts_packages_by_agent_kind_and_rule(tmp_path):
    settings = (".claude/settings.local.json", "claude-code", "settings", True)
    agents_md = ("AGENTS.md", "agents-md", "instructions", False)
    rows = [
        _rec(
            "secret-pkg-a", candidates=[settings, agents_md], secrets=[(*settings, "anthropic")] * 2
        ),
        _rec("secret-pkg-b", candidates=[agents_md]),
        _rec("secret-pkg-c"),
        _rec("secret-pkg-d", status="error", reason="HttpError"),
        _rec("secret-pkg-e", status="gone"),
    ]
    (tmp_path / "targets-npm.jsonl").write_text(
        "".join(json.dumps({"name": r["name"]}) + "\n" for r in rows)
    )
    (tmp_path / "results-npm.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "meta-npm.json").write_text(
        json.dumps(
            {"enumeration_bytes": 500, "enumeration_requests": 1, "enumeration_seconds": 2.0}
        )
    )
    (tmp_path / "sessions-npm.jsonl").write_text(
        json.dumps({"start": 0, "end": 50, "processed": 5, "interrupted": False}) + "\n"
    )
    s = summarize(tmp_path, full=50_000)["npm"]
    assert s["processed"] == 5
    assert s["status"] == {"ok": 3, "error": 1, "gone": 1}
    assert s["error_rate"] == 0.2
    assert s["with_candidates"] == 2
    assert s["with_ap006"] == 1
    assert s["by_agent"] == {"claude-code": 1, "agents-md": 2}
    assert s["by_agent_kind"] == {"claude-code/settings": 1, "agents-md/instructions": 2}
    assert s["with_secrets"] == 1
    assert s["secrets_by_rule"] == {"anthropic": {"packages": 1, "findings": 2}}
    assert s["bytes"]["total"] == 5500
    assert s["projection"]["packages"] == 50_000
    assert s["projection"]["bytes"] == 50_000 * 1000 + 500 * 50_000 // 5
    assert s["projection"]["hours"] == round((50 / 5 * 50_000 + 2.0 * 50_000 / 5) / 3600, 2)
    assert "secret-pkg" not in json.dumps(s)  # counts only, never package names


def test_validation_downloads_are_left_out_of_the_projection(tmp_path):
    agents_md = ("AGENTS.md", "agents-md", "instructions", False)
    rows = [_rec(f"p{i}") for i in range(3)]
    rows.append(_rec("v", method="jsdelivr+tarball", validated=True, validate_diff=0))
    rows[-1]["bytes"] = 100_000  # tarball fetched only to check jsDelivr's list
    rows.append(_rec("c", candidates=[agents_md], method="jsdelivr+tarball", validated=True))
    rows[-1]["bytes"] = 5_000  # tarball needed anyway: it has a candidate path
    (tmp_path / "results-npm.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "meta-npm.json").write_text(json.dumps({"enumeration_bytes": 500}))
    s = summarize(tmp_path, full=50_000)["npm"]
    assert s["bytes"]["total"] == 500 + 3_000 + 100_000 + 5_000
    assert s["bytes"]["validation_only"] == 100_000
    assert s["bytes"]["per_package_mean"] == 2_000  # (3 * 1000 + 5000) / 4
    assert s["projection"]["bytes"] == 50_000 * 2_000 + 500 * 50_000 // 5
    assert s["projection"]["bytes_if_every_tarball_downloaded"] == 50_000 * 52_500


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


def test_summary_command_prints_counts_only(tmp_path, capsys):
    run(
        "npm",
        tmp_path,
        1,
        None,
        recent=Enumerator(["secret-pkg"]),
        scan=_ok_scan,
        log=lambda m: None,
    )
    assert main(["summary", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "npm" in out and "packages scanned" in out
    assert "secret-pkg" not in out
    assert json.loads((tmp_path / "summary.json").read_text())["npm"]["processed"] == 1
