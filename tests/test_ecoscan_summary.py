import json

from tools.ecoscan.__main__ import main
from tools.ecoscan.detect import new_record
from tools.ecoscan.summary import (
    EMAIL,
    REPORT_URL,
    finish,
    summarize,
)

SETTINGS = (".claude/settings.local.json", "claude-code", "settings", True)
AGENTS_MD = ("AGENTS.md", "agents-md", "instructions", False)


def _rec(name, status="ok", candidates=(), secrets=(), version="1.0", **extra):
    rec = new_record("npm", name, version)
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
            "line": 7,
            "rule": r,
            "redacted": redacted,
            "count": 1,
        }
        for p, a, k, ap, r, redacted in secrets
    ]
    return rec


def _write(out, rows, meta=None, sessions=()):
    private = out / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / "targets-npm.jsonl").write_text(
        "".join(json.dumps({"name": r["name"]}) + "\n" for r in rows)
    )
    (private / "results-npm.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (private / "meta-npm.json").write_text(json.dumps(meta or {}))
    (private / "sessions-npm.jsonl").write_text("".join(json.dumps(s) + "\n" for s in sessions))


def test_summary_counts_packages_by_agent_kind_and_rule(tmp_path):
    hit = (*SETTINGS, "anthropic", "xxxx…(len 40)")
    rows = [
        _rec("pkg-a", candidates=[SETTINGS, AGENTS_MD], secrets=[hit, hit]),
        _rec("pkg-b", candidates=[AGENTS_MD]),
        _rec("pkg-c"),
        _rec("pkg-d", status="error", reason="HttpError"),
        _rec("pkg-e", status="gone"),
    ]
    meta = {"enumeration_bytes": 500, "enumeration_requests": 1, "enumeration_seconds": 2.0}
    _write(tmp_path, rows, meta, [{"start": 0, "end": 50, "processed": 5}])
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


def test_partially_scanned_packages_still_count(tmp_path):
    _write(tmp_path, [_rec("p", status="partial", candidates=[AGENTS_MD])])
    s = summarize(tmp_path)["npm"]
    assert s["status"] == {"partial": 1}
    assert s["with_candidates"] == 1


def test_validation_downloads_are_left_out_of_the_projection(tmp_path):
    rows = [_rec(f"p{i}") for i in range(3)]
    rows.append(_rec("v", method="jsdelivr+tarball", validated=True, validate_diff=0))
    rows[-1]["bytes"] = 100_000  # tarball fetched only to check jsDelivr's list
    rows.append(_rec("c", candidates=[AGENTS_MD], method="jsdelivr+tarball", validated=True))
    rows[-1]["bytes"] = 5_000  # tarball needed anyway: it has a candidate path
    _write(tmp_path, rows, {"enumeration_bytes": 500})
    s = summarize(tmp_path, full=50_000)["npm"]
    assert s["bytes"]["total"] == 500 + 3_000 + 100_000 + 5_000
    assert s["bytes"]["validation_only"] == 100_000
    assert s["bytes_per_package"] == 2_000  # (3 * 1000 + 5000) / 4
    assert s["projection"]["bytes"] == 50_000 * 2_000 + 500 * 50_000 // 5
    assert s["projection"]["bytes_if_every_tarball_downloaded"] == 50_000 * 52_500


# --- public summary vs private findings -----------------------------------------------------

NAME, VERSION = "zz-unique-package-name", "9.8.7-zz"
PATH = "zzdir/.claude/settings.local.json"
REDACTED = "zzzz…(len 77)"


def _leaky_run(out):
    secret = (PATH, "claude-code", "settings", True, "anthropic", REDACTED)
    candidate = (PATH, "claude-code", "settings", True)
    rows = [
        _rec(NAME, version=VERSION, candidates=[candidate], secrets=[secret]),
        _rec("zz-clean-package"),
    ]
    _write(out, rows, {"window": [100, 200], "shard": [2, 5]})


def test_public_summary_never_holds_names_versions_paths_or_values(tmp_path):
    _leaky_run(tmp_path)
    finish(tmp_path)
    text = (tmp_path / "public" / "summary.json").read_text()
    for private in (NAME, "zz-clean-package", VERSION, PATH, "zzdir", REDACTED, "zzzz"):
        assert private not in text
    assert "…(len" not in text and "\\u2026(len" not in text
    public = json.loads(text)
    assert public["kind"] == "ecoscan-summary"
    assert public["ecosystems"]["npm"]["with_secrets"] == 1


def test_findings_go_to_one_private_file(tmp_path):
    _leaky_run(tmp_path)
    finish(tmp_path)
    findings = (tmp_path / "private" / "findings.jsonl").read_text().splitlines()
    [row] = [json.loads(line) for line in findings]
    assert {k: row[k] for k in ("eco", "package", "version", "path", "line", "rule")} == {
        "eco": "npm",
        "package": NAME,
        "version": VERSION,
        "path": PATH,
        "line": 7,
        "rule": "anthropic",
    }
    assert row["redacted"] == REDACTED


def test_finish_explains_private_reporting_when_there_are_findings(tmp_path):
    _leaky_run(tmp_path)
    message = finish(tmp_path)
    assert REPORT_URL == "https://github.com/adighatakdas-cloud/agentprint/security/advisories/new"
    assert REPORT_URL in message and EMAIL in message
    assert "Do not post" in message
    assert NAME not in message and PATH not in message and REDACTED not in message


def test_finish_without_findings_has_no_reporting_instructions(tmp_path):
    _write(tmp_path, [_rec("zz-clean-package")])
    message = finish(tmp_path)
    assert REPORT_URL not in message
    assert str(tmp_path / "public" / "summary.json") in message


def test_summary_command_writes_the_public_summary_and_prints_counts_only(tmp_path, capsys):
    _leaky_run(tmp_path)
    assert main(["summary", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "npm" in out and "packages scanned" in out
    assert NAME not in out and PATH not in out
    public = json.loads((tmp_path / "public" / "summary.json").read_text())
    assert public["ecosystems"]["npm"]["processed"] == 2
