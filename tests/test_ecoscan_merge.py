import json

import pytest

from tests.test_ecoscan_summary import AGENTS_MD, SETTINGS, _rec, _write
from tools.ecoscan.__main__ import main
from tools.ecoscan.merge import MergeError, merge
from tools.ecoscan.summary import finish

WINDOW = [1000, 2000]


def _volunteer(base, k, n=3, window=WINDOW, rows=None):
    out = base / f"shard-{k}"
    hit = (*SETTINGS, "anthropic", "zzzz…(len 77)")
    rows = rows or [
        _rec(f"pkg-{k}-a", candidates=[SETTINGS], secrets=[hit]),
        _rec(f"pkg-{k}-b", candidates=[AGENTS_MD]),
        _rec(f"pkg-{k}-c", status="error", reason="HttpError"),
        _rec(f"pkg-{k}-d"),
    ]
    _write(out, rows, {"window": window, "shard": [k, n], "targets": len(rows)})
    finish(out)
    return out / "public" / "summary.json"


def test_merge_adds_up_counts_and_recomputes_rates(tmp_path):
    files = [_volunteer(tmp_path, k) for k in (1, 2, 3)]
    merged = merge(files)
    assert merged["kind"] == "ecoscan-merged-summary"
    npm = merged["ecosystems"]["npm"]
    assert npm["processed"] == 12
    assert npm["status"] == {"ok": 9, "error": 3}
    assert npm["error_rate"] == 0.25
    assert npm["with_candidates"] == 6
    assert npm["by_agent"] == {"claude-code": 3, "agents-md": 3}
    assert npm["secrets_by_rule"] == {"anthropic": {"packages": 3, "findings": 3}}
    assert npm["round"] == {
        "window": WINDOW,
        "shards": 3,
        "merged": [1, 2, 3],
        "missing": [],
        "incomplete": [],
    }
    assert "projection" not in npm
    assert "pkg-" not in json.dumps(merged)


def test_merge_reports_missing_and_unfinished_shards(tmp_path):
    rows = [_rec("pkg-x")]
    unfinished = _volunteer(tmp_path, 1, rows=rows)
    meta_path = tmp_path / "shard-1" / "private" / "meta-npm.json"
    meta_path.write_text(json.dumps({"window": WINDOW, "shard": [1, 3], "targets": 5}))
    finish(tmp_path / "shard-1")
    npm = merge([unfinished])["ecosystems"]["npm"]
    assert npm["round"]["missing"] == [2, 3]
    assert npm["round"]["incomplete"] == [1]


def test_merge_refuses_a_findings_file(tmp_path):
    _volunteer(tmp_path, 1)
    findings = tmp_path / "shard-1" / "private" / "findings.jsonl"
    with pytest.raises(MergeError, match="findings"):
        merge([findings])


def test_merge_refuses_anything_with_names_or_paths(tmp_path):
    path = _volunteer(tmp_path, 1)
    data = json.loads(path.read_text())
    data["ecosystems"]["npm"]["candidates"] = [{"path": "x/.claude/settings.local.json"}]
    path.write_text(json.dumps(data))
    with pytest.raises(MergeError, match="not a public summary"):
        merge([path])


def test_merge_refuses_mixed_rounds_and_repeated_shards(tmp_path):
    a = _volunteer(tmp_path / "a", 1)
    b = _volunteer(tmp_path / "b", 2, window=[1000, 3000])
    with pytest.raises(MergeError, match="window"):
        merge([a, b])
    c = _volunteer(tmp_path / "c", 1)
    with pytest.raises(MergeError, match="twice"):
        merge([a, c])


def test_merge_command_writes_the_total_and_rejects_private_files(tmp_path, capsys):
    files = [str(_volunteer(tmp_path, k)) for k in (1, 2, 3)]
    out = tmp_path / "round-total.json"
    assert main(["merge", *files, "--out", str(out)]) == 0
    assert json.loads(out.read_text())["ecosystems"]["npm"]["processed"] == 12
    assert "packages scanned" in capsys.readouterr().out
    findings = str(tmp_path / "shard-1" / "private" / "findings.jsonl")
    assert main(["merge", findings]) == 2
    assert "findings" in capsys.readouterr().err
