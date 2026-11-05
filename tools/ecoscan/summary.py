"""Turn a run folder into counts. The summary never contains package names, paths or values."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .run import read_jsonl

FULL_RUN = 50_000


def _per_package(rows: list[dict], field: str) -> dict[str, int]:
    """How many packages have at least one candidate with each value of `field`."""
    seen: Counter[str] = Counter()
    for rec in rows:
        seen.update({str(c[field]) for c in rec["candidates"]})
    return dict(seen.most_common())


def _eco(out: Path, eco: str, full: int) -> dict | None:
    rows = list({r["key"]: r for r in read_jsonl(out / f"results-{eco}.jsonl")}.values())
    if not rows:
        return None
    targets = len(read_jsonl(out / f"targets-{eco}.jsonl")) or len(rows)
    meta_path = out / f"meta-{eco}.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    sessions = read_jsonl(out / f"sessions-{eco}.jsonl")
    ok = [r for r in rows if r["status"] in ("ok", "partial")]  # partial: some archive unread
    status = Counter(r["status"] for r in rows)

    by_agent_kind: Counter[str] = Counter()
    for rec in ok:
        by_agent_kind.update({f"{c['agent']}/{c['kind']}" for c in rec["candidates"]})
    rule_packages: Counter[str] = Counter()
    rule_findings: Counter[str] = Counter()
    secret_kinds: Counter[str] = Counter()
    for rec in ok:
        rule_packages.update({s["rule"] for s in rec["secrets"]})
        rule_findings.update(s["rule"] for s in rec["secrets"])
        secret_kinds.update({f"{s['agent']}/{s['kind']}" for s in rec["secrets"]})
    ap006 = [r for r in ok if any(c["ap006"] for c in r["candidates"])]

    package_bytes = sum(r.get("bytes", 0) for r in rows)
    enum_bytes = meta.get("enumeration_bytes", 0)
    wall = sum(s.get("end", s["start"]) - s["start"] for s in sessions)
    done_in_sessions = sum(s.get("processed", 0) for s in sessions) or len(rows)
    per_pkg_seconds = wall / done_in_sessions
    validated = [r for r in rows if r.get("validated") is not None]
    # A tarball fetched only to check jsDelivr's list is research overhead, not scan cost. The
    # sample is random, so the other packages stand for the whole run.
    check_only = [r for r in validated if not r["candidate_count"]]
    basis = [r for r in rows if not (r.get("validated") is not None and not r["candidate_count"])]
    per_pkg_bytes = sum(r.get("bytes", 0) for r in basis) / max(1, len(basis))
    tarball_mean = sum(r.get("bytes", 0) for r in validated) / max(1, len(validated))
    return {
        "targets": targets,
        "processed": len(rows),
        "status": dict(status),
        "error_rate": round(status["error"] / len(rows), 4),
        "files_listed": sum(r["files"] for r in rows),
        "methods": dict(Counter(str(r.get("method")) for r in ok)),
        "with_candidates": sum(1 for r in ok if r["candidate_count"]),
        "with_ap006": len(ap006),
        "ap006_by_agent": dict(
            Counter(a for r in ap006 for a in {c["agent"] for c in r["candidates"] if c["ap006"]})
        ),
        "by_agent": _per_package(ok, "agent"),
        "by_kind": _per_package(ok, "kind"),
        "by_agent_kind": dict(by_agent_kind.most_common()),
        "with_secrets": sum(1 for r in ok if r["secrets"]),
        "with_secrets_in_ap006_files": sum(1 for r in ok if any(s["ap006"] for s in r["secrets"])),
        "secrets_by_rule": {
            rule: {"packages": count, "findings": rule_findings[rule]}
            for rule, count in rule_packages.most_common()
        },
        "secrets_by_agent_kind": dict(secret_kinds.most_common()),
        "with_unscanned_candidates": sum(1 for r in ok if r["not_scanned"]),
        "too_large": dict(Counter(a for r in rows for a in r.get("too_large", []))),
        "validation": {
            "sampled": len(validated),
            "identical": sum(1 for r in validated if r["validated"]),
        },
        "requests": sum(r.get("requests", 0) for r in rows) + meta.get("enumeration_requests", 0),
        "bytes": {
            "enumeration": enum_bytes,
            "packages": package_bytes,
            "total": enum_bytes + package_bytes,
            "validation_only": sum(r.get("bytes", 0) for r in check_only),
            "per_package_mean": round(per_pkg_bytes),
        },
        "wall_seconds": round(wall, 1),
        "seconds_per_package": round(per_pkg_seconds, 4),
        "projection": {
            "packages": full,
            "bytes": round(full * per_pkg_bytes + enum_bytes * full / targets),
            "bytes_if_every_tarball_downloaded": round(full * tarball_mean) if validated else None,
            "hours": round(
                (per_pkg_seconds * full + meta.get("enumeration_seconds", 0) * full / targets)
                / 3600,
                2,
            ),
        },
    }


def summarize(out: Path, full: int = FULL_RUN) -> dict:
    found = {eco: _eco(out, eco, full) for eco in ("npm", "pypi")}
    return {eco: data for eco, data in found.items() if data is not None}


def export_findings(out: Path) -> None:
    """findings-<eco>.jsonl: one redacted line per key-shaped string (for triage, never git)."""
    for eco in ("npm", "pypi"):
        rows = list({r["key"]: r for r in read_jsonl(out / f"results-{eco}.jsonl")}.values())
        lines = [
            json.dumps(
                {"eco": eco, "package": r["name"], "version": r["version"], **s}, ensure_ascii=False
            )
            for r in rows
            for s in r["secrets"]
        ]
        if rows:
            path = out / f"findings-{eco}.jsonl"
            path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _mb(n: float) -> str:
    return f"{n / 1e6:,.1f} MB" if n < 1e9 else f"{n / 1e9:,.2f} GB"


def render(summary: dict) -> str:
    lines = []
    for eco, s in summary.items():
        lines += [
            f"== {eco} ==",
            f"packages scanned     {s['processed']} of {s['targets']}  status {s['status']}",
            f"error rate           {s['error_rate']:.2%}",
            f"with agent files     {s['with_candidates']}  (agentprint AP006 rules: "
            f"{s['with_ap006']})",
            f"  by agent           {s['by_agent']}",
            f"  by kind            {s['by_kind']}",
            f"with key-shaped strings in agent files  {s['with_secrets']}  "
            f"(in AP006 files: {s['with_secrets_in_ap006_files']})",
            f"  by rule            {s['secrets_by_rule']}",
            f"  by file type       {s['secrets_by_agent_kind']}",
            f"validation sample    {s['validation']['identical']}/{s['validation']['sampled']} "
            f"jsDelivr lists identical to the tarball",
            f"downloaded           {_mb(s['bytes']['total'])} in {s['requests']} requests "
            f"({_mb(s['bytes']['validation_only'])} only for the validation sample; "
            f"scan cost {_mb(s['bytes']['per_package_mean'])} per package)",
            f"wall time            {s['wall_seconds'] / 60:,.1f} min "
            f"({s['seconds_per_package']:.2f} s per package)",
            f"projected {s['projection']['packages']:,} packages: "
            f"{_mb(s['projection']['bytes'])}, {s['projection']['hours']} h",
        ]
        if s["projection"]["bytes_if_every_tarball_downloaded"] is not None:
            every = s["projection"]["bytes_if_every_tarball_downloaded"]
            lines.append(f"  (downloading every tarball instead: {_mb(every)})")
        lines.append("")
    return "\n".join(lines)
