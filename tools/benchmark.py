"""Score agentprint (and gitleaks, trufflehog when installed) on the labelled bench corpus.

    uv run python -m tools.benchmark                 # writes bench/results.json
    uv run python -m tools.benchmark --out other.json

Every tool runs offline in its default configuration through its command line. The corpus is
built fresh in a temporary folder by bench/build_corpus.py. Only counts, case ids and tool
versions are printed or stored: never a key, never a finding's matched text.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from bench import build_corpus
from bench.build_corpus import Case

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "bench" / "results.json"
TOOLS = ("agentprint", "gitleaks", "trufflehog")
TIMEOUT = 300


Hits = dict[str, list[tuple[int, int]]]  # case id -> line ranges the tool reported there

# agentprint checks that report a secret. Permission, hook and file-mode checks (AP003, AP004,
# AP007, AP008) are not key detection and are never counted.
AGENTPRINT_SECRET_CHECKS = ("AP001", "AP002", "AP005")


def rate(num: int, den: int) -> float | None:
    """num/den rounded to 4 places; None when there is nothing to divide by."""
    return round(num / den, 4) if den else None


def _overlaps(span: tuple[int, int], reported: Sequence[tuple[int, int]]) -> bool:
    return any(start <= span[1] and end >= span[0] for start, end in reported)


def key_matched(case: Case, hits: Hits) -> bool:
    """One rule for every tool: a finding counts for a positive case only if its reported
    line(s) overlap the line span of the injected key. A finding with no line never counts."""
    return case.lines is not None and _overlaps(case.lines, hits.get(case.id, []))


def _summary(cases: Sequence[Case], flagged: set[str]) -> dict:
    tp = sum(c.id in flagged for c in cases if c.positive)
    fn = sum(c.positive for c in cases) - tp
    fp = sum(c.id in flagged for c in cases if not c.positive)
    tn = sum(not c.positive for c in cases) - fp
    return {
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "recall": rate(tp, tp + fn),
        "precision": rate(tp, tp + fp),
        "false_positive_rate": rate(fp, fp + tn),
    }


KEY_GROUPS = {"plain_keys": "plain-", "hidden_keys": "obf-"}  # overall field -> category prefix


def key_recall(categories: dict, prefix: str) -> dict:
    """Recall over the positive categories whose name starts with `prefix` (plain- or obf-)."""
    group = [c for n, c in categories.items() if n.startswith(prefix) and c["kind"] == "positive"]
    found = sum(c["found"] for c in group)
    cases = sum(c["cases"] for c in group)
    return {"found": found, "cases": cases, "recall": rate(found, cases)}


def score(cases: Sequence[Case], hits: Hits) -> dict:
    """Per-category and overall numbers for one tool, given what it reported where.

    Headline (key-matched): a positive case is found when a finding overlaps the injected
    key's lines, missed otherwise; a negative case is a false positive when the tool reports
    anything in its file. recall = TP/(TP+FN); precision = TP/(TP+FP), file-level.
    `any_hit` is the looser reading: any finding anywhere in a positive case's file counts.
    """
    matched = {c.id for c in cases if c.positive and key_matched(c, hits)}
    anywhere = {c.id for c in cases if c.positive and hits.get(c.id)}
    negatives = {c.id for c in cases if not c.positive and hits.get(c.id)}
    cats: dict[str, dict] = {}
    by_cat: dict[str, list[Case]] = defaultdict(list)
    for c in cases:
        by_cat[c.category].append(c)
    for name, group in by_cat.items():
        if group[0].positive:
            found = sum(c.id in matched for c in group)
            loose = sum(c.id in anywhere for c in group)
            cats[name] = {
                "kind": "positive",
                "cases": len(group),
                "found": found,
                "recall": rate(found, len(group)),
                "any_hit": loose,
            }
        else:
            fp = sum(c.id in negatives for c in group)
            cats[name] = {
                "kind": "negative",
                "cases": len(group),
                "false_positives": fp,
                "false_positive_rate": rate(fp, len(group)),
            }
    overall = _summary(cases, matched | negatives)
    overall["any_hit"] = {
        k: v
        for k, v in _summary(cases, anywhere | negatives).items()
        if k in ("tp", "recall", "precision")
    }
    for field, prefix in KEY_GROUPS.items():  # keys written plainly vs hidden on purpose
        overall[field] = key_recall(cats, prefix)
    return {"overall": overall, "categories": dict(sorted(cats.items()))}


def case_of(path: str, cases_dir: Path) -> str | None:
    """The case id a reported file path belongs to (its first folder under cases/)."""
    try:
        rel = Path(path).resolve().relative_to(cases_dir.resolve())
    except ValueError:
        try:
            rel = Path(path).relative_to(cases_dir)
        except ValueError:
            return None
    return rel.parts[0] if rel.parts else None


def _add(hits: Hits, case: str | None, start: object, end: object) -> None:
    """Record one finding; a missing or non-numeric line is stored as (0, 0), which never
    overlaps a real span but still marks the file as flagged."""
    if case is None:
        return
    if isinstance(start, int) and isinstance(end, int):
        hits.setdefault(case, []).append((start, end))
    else:
        hits.setdefault(case, []).append((0, 0))


def parse_agentprint(report: dict, case: str) -> Hits:
    """From `agentprint scan --format json`: secret checks only, using each finding's line."""
    hits: Hits = {}
    for f in report["findings"]:
        if f.get("check_id") in AGENTPRINT_SECRET_CHECKS:
            _add(hits, case, f.get("line"), f.get("line"))
    return hits


def parse_gitleaks(report: list | None, cases_dir: Path) -> Hits:
    """From a gitleaks JSON report: File, StartLine, EndLine. The matched text is never read."""
    hits: Hits = {}
    for item in report or []:
        _add(
            hits,
            case_of(item.get("File", ""), cases_dir),
            item.get("StartLine"),
            item.get("EndLine"),
        )
    return hits


def parse_trufflehog(lines: Sequence[str], cases_dir: Path) -> Hits:
    """From `trufflehog --json` output: SourceMetadata.Data.Filesystem.{file,line}. Raw is
    never read."""
    hits: Hits = {}
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        fs = (((item.get("SourceMetadata") or {}).get("Data") or {}).get("Filesystem")) or {}
        if fs.get("file"):
            _add(hits, case_of(fs["file"], cases_dir), fs.get("line"), fs.get("line"))
    return hits


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd, check=False
    )


def _agentprint_cmd() -> list[str]:
    found = shutil.which("agentprint")
    if found:
        return [found]
    return [sys.executable, "-c", "from agentprint.cli import app; app()"]


def version_agentprint() -> str:
    out = _run([*_agentprint_cmd(), "--version"])
    return out.stdout.strip().removeprefix("agentprint").strip() or "unknown"


def hits_agentprint(cases: Sequence[Case], cases_dir: Path) -> Hits:
    cmd = _agentprint_cmd()

    def one(case: Case) -> Hits:
        folder = cases_dir / case.id
        # --home so history files (which live in a home folder) are read too.
        out = _run(
            [*cmd, "scan", "--home", str(folder), "--format", "json", "--fail-on", "none",
             "--plain", str(folder)]
        )  # fmt: skip
        try:
            return parse_agentprint(json.loads(out.stdout), case.id)
        except (ValueError, KeyError):
            raise RuntimeError(f"agentprint gave no JSON for case {case.id}") from None

    merged: Hits = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for part in pool.map(one, cases):
            merged |= part
    return merged


def version_gitleaks() -> str:
    return _run(["gitleaks", "version"]).stdout.strip().removeprefix("v")


def hits_gitleaks(cases: Sequence[Case], cases_dir: Path) -> Hits:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        out = _run(
            ["gitleaks", "dir", str(cases_dir), "--no-banner", "--redact", "--exit-code", "0",
             "--report-format", "json", "--report-path", str(report)]
        )  # fmt: skip
        if out.returncode != 0:
            raise RuntimeError(f"gitleaks dir exited {out.returncode}; need gitleaks 8.19 or newer")
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    return parse_gitleaks(data, cases_dir)


def version_trufflehog() -> str:
    out = _run(["trufflehog", "--version"])
    return (out.stdout + out.stderr).strip().removeprefix("trufflehog").strip()


def hits_trufflehog(cases: Sequence[Case], cases_dir: Path) -> Hits:
    out = _run(
        ["trufflehog", "filesystem", str(cases_dir), "--json", "--no-verification", "--no-update"]
    )
    if out.returncode != 0:
        raise RuntimeError(f"trufflehog exited {out.returncode}")
    return parse_trufflehog(out.stdout.splitlines(), cases_dir)


RUNNERS: dict[str, tuple[Callable[[], str], Callable[[Sequence[Case], Path], Hits]]] = {
    "agentprint": (version_agentprint, hits_agentprint),
    "gitleaks": (version_gitleaks, hits_gitleaks),
    "trufflehog": (version_trufflehog, hits_trufflehog),
}


HIDDEN_FOLDERS = ("/.claude/", "/.codex/", "/.cursor/", "/.hermes/")


def sanity_check(tool: str, cases: Sequence[Case], hits: Hits) -> None:
    """Fail loudly if a tool plainly did not run as intended: it must find at least one plain
    key on its line, and one of those must live in a hidden agent folder (.claude, .codex, ...),
    which shows the tool really read hidden directories."""
    plain = [c for c in cases if c.category.startswith("plain-") and key_matched(c, hits)]
    if not plain:
        raise RuntimeError(f"{tool} found none of the plain keys; the run is not valid")
    if not any(any(h in f"/{c.file}" for h in HIDDEN_FOLDERS) for c in plain):
        raise RuntimeError(f"{tool} reported nothing inside hidden agent folders; run is not valid")


def installed(tool: str) -> bool:
    return tool == "agentprint" or shutil.which(tool) is not None


def run_all(tools: Sequence[str] = TOOLS) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "corpus"
        root.mkdir()
        cases = build_corpus.build(root)
        results: dict[str, dict] = {}
        skipped = []
        for tool in tools:
            if not installed(tool):
                skipped.append(tool)
                continue
            version, flag = RUNNERS[tool]
            entry = {"version": version()}
            hits = flag(cases, root / "cases")
            sanity_check(tool, cases, hits)
            entry |= score(cases, hits)
            results[tool] = entry
    positives = sum(c.positive for c in cases)
    return {
        "schema": 1,
        "generated": datetime.now(UTC).strftime("%Y-%m-%d"),
        "seed": build_corpus.SEED,
        "corpus": {
            "cases": len(cases),
            "positives": positives,
            "negatives": len(cases) - positives,
        },
        "tools": results,
        "not_run": skipped,
    }


def summary(results: dict) -> str:
    """Counts only, one line per tool."""
    lines = []
    for name, r in results["tools"].items():
        o = r["overall"]
        plain, hidden = (key_recall(r["categories"], prefix) for prefix in KEY_GROUPS.values())
        lines.append(
            f"{name} {r['version']}: recall {o['recall']} precision {o['precision']} "
            f"(key found {o['tp']}/{o['tp'] + o['fn']}: plain {plain['found']}/{plain['cases']}, "
            f"hidden {hidden['found']}/{hidden['cases']}; any hit in file recall "
            f"{o['any_hit']['recall']}, false positives {o['fp']}/{o['fp'] + o['tn']})"
        )
    if results["not_run"]:
        lines.append("not installed, skipped: " + ", ".join(results["not_run"]))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)
    results = run_all()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(summary(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
