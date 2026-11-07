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
    for needed in ("zero-width", "split-string", "yaml-folded", "line-continuation", "base64"):
        assert f"obf-{needed}" in by_cat
    for needed in ("git-sha", "uuid", "sha256", "base64-image", "minified-js", "lorem-ipsum"):
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


def test_score_math():
    cases = [
        Case("p-0", "plain-x", True, "f"),
        Case("p-1", "plain-x", True, "f"),
        Case("o-0", "obf-y", True, "f"),
        Case("n-0", "neg-z", False, "f"),
        Case("n-1", "neg-z", False, "f"),
        Case("n-2", "neg-z", False, "f"),
        Case("n-3", "neg-z", False, "f"),
    ]
    result = benchmark.score(cases, {"p-0", "o-0", "n-1"})
    assert result["overall"] == {
        "tp": 2,
        "fn": 1,
        "fp": 1,
        "tn": 3,
        "recall": 0.6667,
        "precision": 0.6667,
        "false_positive_rate": 0.25,
    }
    cats = result["categories"]
    assert cats["plain-x"] == {"kind": "positive", "cases": 2, "found": 1, "recall": 0.5}
    assert cats["obf-y"]["recall"] == 1.0
    assert cats["neg-z"] == {
        "kind": "negative",
        "cases": 4,
        "false_positives": 1,
        "false_positive_rate": 0.25,
    }


def test_score_with_nothing_flagged_or_nothing_to_find():
    cases = [Case("n-0", "neg-z", False, "f")]
    o = benchmark.score(cases, set())["overall"]
    assert o["recall"] is None and o["precision"] is None and o["false_positive_rate"] == 0.0


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
