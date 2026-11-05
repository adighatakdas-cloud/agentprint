"""Turn a run folder into two outputs, kept apart on purpose:

- PUBLIC  <out>/public/summary.json   counts only: no package names, versions, paths or values.
                                      Safe to post. `merge` adds many of these together.
- PRIVATE <out>/private/findings.jsonl one line per key-shaped string: package, version, path,
                                      line, rule and the redacted value. Never posted.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .run import private_dir, read_jsonl

FULL_RUN = 50_000
ECOSYSTEMS = ("npm", "pypi")
SCANNED = ("ok", "partial")  # partial: one of a release's archives could not be read
REPORT_URL = "https://github.com/adighatakdas-cloud/agentprint/security/advisories/new"
EMAIL = "adi65das@gmail.com"


def _per_package(rows: list[dict], field: str) -> dict[str, int]:
    """How many packages have at least one candidate with each value of `field`."""
    seen: Counter[str] = Counter()
    for rec in rows:
        seen.update({str(c[field]) for c in rec["candidates"]})
    return dict(seen.most_common())


def newest_rows(out: Path, eco: str) -> list[dict]:
    """One result per package: the newest line wins (see `run --redo`)."""
    return list(
        {r["key"]: r for r in read_jsonl(private_dir(out) / f"results-{eco}.jsonl")}.values()
    )


def counts(rows: list[dict], meta: dict, sessions: list[dict]) -> dict:
    """Numbers that can be added up across volunteers' runs. No names, paths or values."""
    ok = [r for r in rows if r["status"] in SCANNED]
    by_agent_kind: Counter[str] = Counter()
    rule_packages: Counter[str] = Counter()
    rule_findings: Counter[str] = Counter()
    secret_kinds: Counter[str] = Counter()
    for rec in ok:
        by_agent_kind.update({f"{c['agent']}/{c['kind']}" for c in rec["candidates"]})
        rule_packages.update({s["rule"] for s in rec["secrets"]})
        rule_findings.update(s["rule"] for s in rec["secrets"])
        secret_kinds.update({f"{s['agent']}/{s['kind']}" for s in rec["secrets"]})
    ap006 = [r for r in ok if any(c["ap006"] for c in r["candidates"])]
    validated = [r for r in rows if r.get("validated") is not None]
    # A tarball fetched only to check jsDelivr's list is research overhead, not scan cost.
    check_only = [r for r in validated if not r["candidate_count"]]
    basis = [r for r in rows if not (r.get("validated") is not None and not r["candidate_count"])]
    package_bytes = sum(r.get("bytes", 0) for r in rows)
    enum_bytes = meta.get("enumeration_bytes", 0)
    return {
        "targets": meta.get("targets", len(rows)),
        "processed": len(rows),
        "status": dict(Counter(r["status"] for r in rows)),
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
            rule: {"packages": n, "findings": rule_findings[rule]}
            for rule, n in rule_packages.most_common()
        },
        "secrets_by_agent_kind": dict(secret_kinds.most_common()),
        "with_unscanned_candidates": sum(1 for r in ok if r["not_scanned"]),
        "too_large": dict(Counter(a for r in rows for a in r.get("too_large", []))),
        "validation": {
            "sampled": len(validated),
            "identical": sum(1 for r in validated if r["validated"]),
            "tarball_bytes": sum(r.get("bytes", 0) for r in validated),
        },
        "requests": sum(r.get("requests", 0) for r in rows) + meta.get("enumeration_requests", 0),
        "bytes": {
            "enumeration": enum_bytes,
            "packages": package_bytes,
            "total": enum_bytes + package_bytes,
            "validation_only": sum(r.get("bytes", 0) for r in check_only),
            "scan_basis": sum(r.get("bytes", 0) for r in basis),
            "scan_basis_packages": len(basis),
        },
        "seconds": {
            "wall": round(sum(s.get("end", s["start"]) - s["start"] for s in sessions), 1),
            "timed_packages": sum(s.get("processed", 0) for s in sessions),
            "enumeration": meta.get("enumeration_seconds", 0),
        },
    }


def derive(c: dict, full: int | None = None) -> dict:
    """Rates and per-package figures, recomputed from counts (also after a merge)."""
    out = dict(c)
    out["error_rate"] = round(c["status"].get("error", 0) / max(1, c["processed"]), 4)
    per_bytes = c["bytes"]["scan_basis"] / max(1, c["bytes"]["scan_basis_packages"])
    timed = c["seconds"]["timed_packages"] or c["processed"]
    per_seconds = c["seconds"]["wall"] / max(1, timed)
    out["bytes_per_package"] = round(per_bytes)
    out["seconds_per_package"] = round(per_seconds, 4)
    if full:
        sampled = c["validation"]["sampled"]
        targets = max(1, c["targets"])
        out["projection"] = {
            "packages": full,
            "bytes": round(full * per_bytes + c["bytes"]["enumeration"] * full / targets),
            "bytes_if_every_tarball_downloaded": (
                round(full * c["validation"]["tarball_bytes"] / sampled) if sampled else None
            ),
            "hours": round(
                (per_seconds * full + c["seconds"]["enumeration"] * full / targets) / 3600, 2
            ),
        }
    return out


def _round(meta: dict) -> dict:
    return {
        "window": meta.get("window"),
        "limit": None if meta.get("window") else meta.get("requested"),
        "shard": meta.get("shard"),
    }


def summarize(out: Path, full: int | None = FULL_RUN) -> dict:
    found = {}
    for eco in ECOSYSTEMS:
        rows = newest_rows(out, eco)
        if not rows:
            continue
        meta_path = private_dir(out) / f"meta-{eco}.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        sessions = read_jsonl(private_dir(out) / f"sessions-{eco}.jsonl")
        found[eco] = {"round": _round(meta), **derive(counts(rows, meta, sessions), full)}
    return found


def public_summary(out: Path, full: int | None = FULL_RUN) -> dict:
    return {
        "kind": "ecoscan-summary",
        "schema": 1,
        "note": "Counts only: no package names, versions, paths or key values. Safe to post.",
        "ecosystems": summarize(out, full),
    }


def export_findings(out: Path) -> int:
    """private/findings.jsonl: one redacted line per key-shaped string. Returns the count."""
    lines = [
        json.dumps(
            {"eco": eco, "package": r["name"], "version": r["version"], **s}, ensure_ascii=False
        )
        for eco in ECOSYSTEMS
        for r in newest_rows(out, eco)
        for s in r["secrets"]
    ]
    private_dir(out).mkdir(parents=True, exist_ok=True)
    path = private_dir(out) / "findings.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return len(lines)


def finish(out: Path, full: int | None = FULL_RUN) -> str:
    """Write both outputs and return what to tell the volunteer (counts and paths only)."""
    summary = public_summary(out, full)
    public = out / "public" / "summary.json"
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    found = export_findings(out)
    lines = [
        render(summary["ecosystems"]),
        f"PUBLIC  (counts only, safe to post on the scan-round issue): {public}",
        f"PRIVATE (never post, not even redacted):                     {private_dir(out)}",
    ]
    if found:
        lines += [
            "",
            f"{found} key-shaped string(s) were found in agent files. They are redacted and saved",
            f"in {private_dir(out) / 'findings.jsonl'}.",
            "Do not post them anywhere public, and do not use or test any key.",
            "Send the file privately so the maintainers can be told first:",
            f"  - GitHub private vulnerability reporting: {REPORT_URL}",
            f"  - or email: {EMAIL}",
        ]
    return "\n".join(lines)


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
            f"scan cost {_mb(s['bytes_per_package'])} per package)",
            f"wall time            {s['seconds']['wall'] / 60:,.1f} min "
            f"({s['seconds_per_package']:.2f} s per package)",
        ]
        if "projection" in s:
            p = s["projection"]
            lines.append(f"projected {p['packages']:,} packages: {_mb(p['bytes'])}, {p['hours']} h")
            if p["bytes_if_every_tarball_downloaded"] is not None:
                every = p["bytes_if_every_tarball_downloaded"]
                lines.append(f"  (downloading every tarball instead: {_mb(every)})")
        lines.append("")
    return "\n".join(lines)
