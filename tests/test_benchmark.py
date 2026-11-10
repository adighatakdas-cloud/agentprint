import json
import re
from pathlib import Path

import pytest
import yaml

from agentprint.secrets import find_secrets
from bench import build_corpus
from bench.build_corpus import Case
from tools import benchmark, gen_docs

ROOT = Path(__file__).resolve().parents[1]


def _build(tmp_path: Path, seed: int = build_corpus.SEED) -> list[Case]:
    return build_corpus.build(tmp_path, seed)


def test_corpus_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    _build(a), _build(b)
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files and files == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert all((a / f).read_bytes() == (b / f).read_bytes() for f in files)
    c = tmp_path / "c"
    c.mkdir()
    _build(c, seed=1)
    assert (a / "manifest.json").read_text() == (c / "manifest.json").read_text()  # ids only
    assert (a / "cases/plain-claude-mcp-0/.mcp.json").read_bytes() != (
        c / "cases/plain-claude-mcp-0/.mcp.json"
    ).read_bytes()


def test_labels_are_consistent(tmp_path):
    cases = _build(tmp_path)
    assert len({c.id for c in cases}) == len(cases)
    assert build_corpus.load_manifest(tmp_path) == cases
    by_cat: dict[str, set[bool]] = {}
    for c in cases:
        by_cat.setdefault(c.category, set()).add(c.positive)
        assert (tmp_path / "cases" / c.id / c.file).is_file()
        assert (
            len([f for f in (tmp_path / "cases" / c.id).rglob("*") if f.is_file()]) == 1
        )  # one file per case
        assert c.positive == (not c.category.startswith("neg-"))
    assert all(len(v) == 1 for v in by_cat.values())  # no category mixes labels
    for needed in (
        "zero-width", "split-string", "yaml-folded", "line-continuation", "base64",
        "quotes-side-by-side",
    ):  # fmt: skip
        assert f"obf-{needed}" in by_cat
    for needed in (
        "git-sha", "uuid", "sha256", "base64-image", "minified-js", "lorem-ipsum",
        "empty-quotes", "short-base64",
    ):  # fmt: skip
        assert f"neg-{needed}" in by_cat


def test_plain_positives_hold_a_key_the_engine_finds_and_negatives_do_not(tmp_path):
    for case in _build(tmp_path):
        text = (tmp_path / "cases" / case.id / case.file).read_text(encoding="utf-8")
        if case.category.startswith("plain-"):
            assert find_secrets(text), case.id
        elif case.category == "neg-jwt-in-token-field":
            continue  # a hard case: allowed to trip the generic rule
        elif case.category.startswith("neg-"):
            assert not find_secrets(text), case.id


def test_every_key_body_carries_the_fake_marker():
    rng = build_corpus.random.Random(0)
    for kind in build_corpus.KINDS:
        assert "FAKE" in build_corpus.make_key(kind, rng), kind


def _cases():
    return [
        Case("p-0", "plain-x", True, "f", (2, 2)),
        Case("p-1", "plain-x", True, "f", (2, 2)),
        Case("o-0", "obf-y", True, "f", (3, 5)),
        Case("n-0", "neg-z", False, "f"),
        Case("n-1", "neg-z", False, "f"),
        Case("n-2", "neg-z", False, "f"),
        Case("n-3", "neg-z", False, "f"),
    ]


def test_score_math():
    hits = {"p-0": [(2, 2)], "o-0": [(5, 6)], "n-1": [(1, 1)]}
    result = benchmark.score(_cases(), hits)
    overall = result["overall"]
    assert overall["tp"] == 2 and overall["fn"] == 1 and overall["fp"] == 1 and overall["tn"] == 3
    assert overall["recall"] == 0.6667 and overall["precision"] == 0.6667
    assert overall["false_positive_rate"] == 0.25
    cats = result["categories"]
    assert cats["plain-x"]["found"] == 1 and cats["plain-x"]["recall"] == 0.5
    assert cats["obf-y"]["recall"] == 1.0  # a multi-line span is matched by overlap
    assert cats["neg-z"]["false_positives"] == 1 and cats["neg-z"]["false_positive_rate"] == 0.25


def test_score_splits_recall_into_plain_and_hidden_keys():
    hits = {"p-0": [(2, 2)], "o-0": [(5, 6)], "n-1": [(1, 1)]}
    overall = benchmark.score(_cases(), hits)["overall"]
    assert overall["plain_keys"] == {"found": 1, "cases": 2, "recall": 0.5}
    assert overall["hidden_keys"] == {"found": 1, "cases": 1, "recall": 1.0}
    assert benchmark.score(_cases(), {})["overall"]["hidden_keys"]["recall"] == 0.0


def _results(categories: dict[str, dict[str, dict]]) -> dict:
    """A results file for the page, with the given categories per tool."""
    tools = {}
    for tool, cats in categories.items():
        found = sum(c.get("found", 0) for c in cats.values())
        cases = sum(c["cases"] for c in cats.values() if c["kind"] == "positive")
        tools[tool] = {
            "version": "9.9.9",
            "overall": {
                "tp": found,
                "fn": cases - found,
                "fp": 0,
                "tn": 7,
                "recall": found / cases,
                "precision": 1.0,
                "any_hit": {"recall": found / cases},
            },
            "categories": cats,
        }
    return {
        "generated": "2026-11-07",
        "seed": 1,
        "corpus": {"cases": 21, "positives": 14, "negatives": 7},
        "tools": tools,
        "not_run": [],
    }


def _positive(found: int) -> dict:
    return {"kind": "positive", "cases": 7, "found": found}


def test_page_shows_plain_and_hidden_recall_per_tool():
    negative = {"kind": "negative", "cases": 7, "false_positives": 0}
    results = _results(
        {
            "agentprint": {"plain-a": _positive(7), "obf-b": _positive(6), "neg-c": negative},
            "gitleaks": {"plain-a": _positive(5), "obf-b": _positive(1), "neg-c": negative},
        }
    )
    page = gen_docs.benchmark_page(results)
    header = next(line for line in page.splitlines() if line.startswith("| Tool |"))
    assert "| Recall, plain keys | Recall, hidden keys |" in header
    rows = {line.split(" | ")[0]: line for line in page.splitlines() if line.startswith("| ")}
    assert "| 100% (7 of 7) | 86% (6 of 7) |" in rows["| agentprint"]
    assert "| 71% (5 of 7) | 14% (1 of 7) |" in rows["| gitleaks"]
    assert "7 of the 14 keys are hidden" in page


def test_page_says_the_hidden_rows_favour_agentprint_and_partial_matches_count():
    results = json.loads((ROOT / "bench" / "results.json").read_text(encoding="utf-8"))
    page = " ".join(gen_docs.benchmark_page(results).split())
    assert "The hidden-key rows favour agentprint:** they test the tricks this release" in page
    assert "written alongside the new detection" in page
    assert "counts even when only part of the key matched" in page
    assert "split string\" hits, for example, are matches of the key's first part" in page
    assert "jwt in token field" in page


def test_a_finding_off_the_key_line_does_not_count_but_shows_in_any_hit():
    hits = {"p-0": [(9, 9)], "p-1": [(0, 0)], "o-0": [(1, 2)]}
    result = benchmark.score(_cases(), hits)
    assert result["overall"]["tp"] == 0 and result["overall"]["recall"] == 0.0
    assert result["overall"]["any_hit"]["tp"] == 3 and result["overall"]["any_hit"]["recall"] == 1.0
    assert result["categories"]["plain-x"]["any_hit"] == 2


def test_negative_findings_are_false_positives_on_any_line():
    result = benchmark.score(_cases(), {"n-0": [(7, 7)], "n-3": [(0, 0)]})
    assert result["overall"]["fp"] == 2 and result["overall"]["precision"] == 0.0


def test_score_with_nothing_flagged_or_nothing_to_find():
    o = benchmark.score([Case("n-0", "neg-z", False, "f")], {})["overall"]
    assert o["recall"] is None and o["precision"] is None and o["false_positive_rate"] == 0.0


def test_agentprint_parser_counts_only_secret_checks():
    report = {
        "findings": [
            {"check_id": "AP002", "line": 4},
            {"check_id": "AP003", "line": 4},  # permission
            {"check_id": "AP004", "line": 4},  # hook
            {"check_id": "AP008", "line": None},  # file mode
            {"check_id": "AP001", "line": None},  # secret, no line: flagged, never key-matched
        ]
    }
    assert benchmark.parse_agentprint(report, "c") == {"c": [(4, 4), (0, 0)]}


def test_gitleaks_parser_reads_file_and_lines_only(tmp_path):
    cases = tmp_path / "cases"
    report = [
        {"File": str(cases / "a-0" / ".claude" / "h.jsonl"), "StartLine": 2, "EndLine": 3,
         "Secret": "REDACTED", "Match": "REDACTED"},
        {"File": "/elsewhere/x", "StartLine": 1, "EndLine": 1},
    ]  # fmt: skip
    assert benchmark.parse_gitleaks(report, cases) == {"a-0": [(2, 3)]}
    assert benchmark.parse_gitleaks(None, cases) == {}


def test_trufflehog_parser_reads_file_and_line_only(tmp_path):
    cases = tmp_path / "cases"
    item = {
        "DetectorName": "OpenAI",
        "Raw": "REDACTED",
        "SourceMetadata": {
            "Data": {"Filesystem": {"file": str(cases / "b-1" / "x.toml"), "line": 6}}
        },
    }
    lines = [json.dumps(item), "not json", json.dumps({"SourceMetadata": {}})]
    assert benchmark.parse_trufflehog(lines, cases) == {"b-1": [(6, 6)]}


def test_sanity_check_fails_loudly(tmp_path):
    cases = _build(tmp_path)
    plain = next(c for c in cases if c.category == "plain-claude-history")
    assert plain.lines
    with pytest.raises(RuntimeError, match="none of the plain keys"):
        benchmark.sanity_check("t", cases, {})
    with pytest.raises(RuntimeError, match="none of the plain keys"):
        benchmark.sanity_check("t", cases, {plain.id: [(99, 99)]})  # found, but not on the key
    benchmark.sanity_check("t", cases, {plain.id: [plain.lines]})
    mcp = next(c for c in cases if c.file == ".mcp.json" and c.lines)  # not in a hidden folder
    with pytest.raises(RuntimeError, match="hidden agent folders"):
        benchmark.sanity_check("t", [mcp], {mcp.id: [mcp.lines]})


def test_spans_cover_the_key_and_the_aws_pair(tmp_path):
    cases = _build(tmp_path)
    for c in cases:
        if not c.positive:
            assert c.lines is None
            continue
        assert c.lines
        lines = (tmp_path / "cases" / c.id / c.file).read_text(encoding="utf-8").split("\n")
        start, end = c.lines
        assert 1 <= start <= end <= len(lines), c.id
        span = "\n".join(lines[start - 1 : end])
        if c.category.startswith("plain-"):
            assert find_secrets(span), c.id  # the span holds the key
        text = "\n".join(lines)
        if c.id.endswith("-3") and c.category not in build_corpus.NEW_FAMILIES:
            # AWS is key kind index 3, in every category but the newer key families
            assert "AWS_SECRET_ACCESS_KEY" in text  # the pair is in the file ...
            assert "AWS_SECRET_ACCESS_KEY" not in span  # ... but never inside the scored span
        if c.category == "obf-yaml-folded":
            assert end - start >= 2


def test_case_of_maps_a_reported_path_to_its_case(tmp_path):
    cases_dir = tmp_path / "cases"
    (cases_dir / "plain-a-0" / ".claude").mkdir(parents=True)
    f = cases_dir / "plain-a-0" / ".claude" / "history.jsonl"
    f.write_text("x")
    assert benchmark.case_of(str(f), cases_dir) == "plain-a-0"
    assert benchmark.case_of("/elsewhere/file", cases_dir) is None


@pytest.mark.integration  # runs the installed CLI once per case
def test_run_prints_and_stores_no_key_value(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(benchmark, "installed", lambda tool: tool == "agentprint")
    out = tmp_path / "results.json"
    assert benchmark.main(["--out", out.as_posix()]) == 0
    printed = capsys.readouterr().out
    stored = out.read_text(encoding="utf-8")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    cases = _build(corpus)
    for case in cases:
        if not case.positive:
            continue
        text = (corpus / "cases" / case.id / case.file).read_text(encoding="utf-8")
        for secret in find_secrets(text):
            assert secret.value not in printed and secret.value not in stored
    assert not re.search(r"sk-ant-|sk-proj-|ghp_|AKIA|xoxb-|AIza|sk_live_", printed + stored)
    data = json.loads(stored)
    assert data["tools"]["agentprint"]["overall"]["tp"] > 0
    assert data["not_run"] == ["gitleaks", "trufflehog"]


def test_committed_results_are_well_formed_and_page_names_the_tools():
    results = json.loads((ROOT / "bench" / "results.json").read_text(encoding="utf-8"))
    assert results["schema"] == 1 and "agentprint" in results["tools"]
    page = gen_docs.benchmark_page(results)
    for tool, r in results["tools"].items():
        assert f"{tool} {r['version']}" in page
    for tool in results["not_run"]:
        assert f"Not included in this run: {', '.join(results['not_run'])}" in page
        assert tool in page
    assert "synthetic" in page and "labels are ours" in page


def test_workflow_is_manual_pinned_and_checksummed():
    text = (ROOT / ".github" / "workflows" / "benchmark.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert list(wf[True]) == ["workflow_dispatch"]  # YAML reads `on` as True
    assert wf["permissions"] == {"contents": "read"}
    steps = wf["jobs"]["benchmark"]["steps"]
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
        assert "${{" not in step.get("run", "")
    checkout = next(s for s in steps if s["uses"].startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    assert text.count("sha256sum --check") == 2
    assert re.fullmatch(r"[0-9a-f]{64}", wf["env"]["GITLEAKS_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", wf["env"]["TRUFFLEHOG_SHA256"])
    assert "--no-verification" not in text  # that flag belongs to the runner, not the workflow
