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


def rate(num: int, den: int) -> float | None:
    """num/den rounded to 4 places; None when there is nothing to divide by."""
    return round(num / den, 4) if den else None


def score(cases: Sequence[Case], flagged: set[str]) -> dict:
    """Per-category and overall numbers for one tool, given the ids of cases it flagged.

    Positive case: flagged = found (TP), else missed (FN). Negative case: flagged = false
    positive (FP), else clean (TN). recall = TP/(TP+FN). precision = TP/(TP+FP).
    """
    cats: dict[str, dict] = {}
    by_cat: dict[str, list[Case]] = defaultdict(list)
    for c in cases:
        by_cat[c.category].append(c)
    for name, group in by_cat.items():
        hit = sum(c.id in flagged for c in group)
        if group[0].positive:
            cats[name] = {
                "kind": "positive",
                "cases": len(group),
                "found": hit,
                "recall": rate(hit, len(group)),
            }
        else:
            cats[name] = {
                "kind": "negative",
                "cases": len(group),
                "false_positives": hit,
                "false_positive_rate": rate(hit, len(group)),
            }
    tp = sum(c.id in flagged for c in cases if c.positive)
    fn = sum(c.positive for c in cases) - tp
    fp = sum(c.id in flagged for c in cases if not c.positive)
    tn = sum(not c.positive for c in cases) - fp
    overall = {
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "recall": rate(tp, tp + fn),
        "precision": rate(tp, tp + fp),
        "false_positive_rate": rate(fp, fp + tn),
    }
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


def flagged_agentprint(cases: Sequence[Case], cases_dir: Path) -> set[str]:
    cmd = _agentprint_cmd()

    def one(case: Case) -> str | None:
        folder = cases_dir / case.id
        # --home so history files (which live in a home folder) are read too.
        out = _run(
            [*cmd, "scan", "--home", str(folder), "--format", "json", "--fail-on", "none",
             "--plain", str(folder)]
        )  # fmt: skip
        try:
            findings = json.loads(out.stdout)["findings"]
        except (ValueError, KeyError):
            raise RuntimeError(f"agentprint gave no JSON for case {case.id}") from None
        return case.id if findings else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return {c for c in pool.map(one, cases) if c}


def version_gitleaks() -> str:
    return _run(["gitleaks", "version"]).stdout.strip().removeprefix("v")


def flagged_gitleaks(cases: Sequence[Case], cases_dir: Path) -> set[str]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        base = ["--redact", "--report-format", "json", "--report-path", str(report)]
        out = _run(["gitleaks", "dir", str(cases_dir), "--no-banner", "--exit-code", "0", *base])
        if out.returncode != 0:  # gitleaks before 8.19 has no `dir`
            out = _run(
                ["gitleaks", "detect", "--no-git", "--source", str(cases_dir), "--no-banner",
                 "--exit-code", "0", *base]
            )  # fmt: skip
        if out.returncode != 0:
            raise RuntimeError(f"gitleaks exited {out.returncode}")
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
        # Read only the file path of each finding; the matched text is never touched.
        files = [item.get("File", "") for item in data or []]
    return {c for c in (case_of(f, cases_dir) for f in files) if c}


def version_trufflehog() -> str:
    out = _run(["trufflehog", "--version"])
    return (out.stdout + out.stderr).strip().removeprefix("trufflehog").strip()


def flagged_trufflehog(cases: Sequence[Case], cases_dir: Path) -> set[str]:
    out = _run(
        ["trufflehog", "filesystem", str(cases_dir), "--json", "--no-verification", "--no-update"]
    )
    if out.returncode != 0:
        raise RuntimeError(f"trufflehog exited {out.returncode}")
    found: set[str] = set()
    for line in out.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        file = (((item.get("SourceMetadata") or {}).get("Data") or {}).get("Filesystem") or {}).get(
            "file", ""
        )
        case = case_of(file, cases_dir) if file else None
        if case:
            found.add(case)
    return found


RUNNERS: dict[str, tuple[Callable[[], str], Callable[[Sequence[Case], Path], set[str]]]] = {
    "agentprint": (version_agentprint, flagged_agentprint),
    "gitleaks": (version_gitleaks, flagged_gitleaks),
    "trufflehog": (version_trufflehog, flagged_trufflehog),
}


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
            entry |= score(cases, flag(cases, root / "cases"))
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
        lines.append(
            f"{name} {r['version']}: recall {o['recall']} precision {o['precision']} "
            f"(found {o['tp']}/{o['tp'] + o['fn']}, false positives {o['fp']}/{o['fp'] + o['tn']})"
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
