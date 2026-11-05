"""Resumable scan loop. Everything it writes is names, paths, counts and redacted values.

Working files live in <out>/private/ (they name packages, so they are never posted), per
ecosystem (`npm`, `pypi`):
- targets-<eco>.jsonl   the package list, fetched once; a resumed run reuses it
- meta-<eco>.json       the round (window, shard) and what fetching the list cost
- results-<eco>.jsonl   one line per finished package (the resume state)
- sessions-<eco>.jsonl  one line per run: start, end, packages done, interrupted or not
"""

from __future__ import annotations

import json
import os
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import npm, pypi
from .detect import new_record
from .net import Http, Tally, TooLarge
from .shard import shard_of

REGISTRIES = {"npm": npm, "pypi": pypi}
PRIVATE_NOTE = """\
Do not post anything in this folder.

These files name packages and versions, and findings.jsonl holds redacted key-shaped strings.
Only ../public/summary.json is safe to share (it holds counts only).

If findings.jsonl is not empty, send it privately so maintainers can be told first:
  - GitHub private vulnerability reporting:
    https://github.com/adighatakdas-cloud/agentprint/security/advisories/new
  - or email: adi65das@gmail.com
Never use or test a key you find.
"""


def private_dir(out: Path) -> Path:
    return out / "private"


def key(target: dict) -> str:
    return f"{target['name']}@{target.get('version') or ''}"


def read_jsonl(path: Path) -> list[dict]:
    """Every complete line; a half-written last line (killed mid-write) is skipped."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _targets(eco, out: Path, n, http, recent, log, window, shard) -> list[dict]:
    path = private_dir(out) / f"targets-{eco}.jsonl"
    meta_path = private_dir(out) / f"meta-{eco}.json"
    wanted = {"window": list(window) if window else None, "shard": list(shard)}
    if path.exists():
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        for field, value in wanted.items():
            if meta.get(field) != value:
                raise ValueError(
                    f"{out} was started with {field} {meta.get(field)}, not {value}; "
                    "use a new --out folder for another round or shard"
                )
        return read_jsonl(path)
    where = f"window {window[0]}:{window[1]}" if window else f"the {n} most recent"
    log(f"{eco}: listing packages in {where}")
    tally, start = Tally(), time.monotonic()
    listed = recent(http, n, tally)
    k, total = shard
    targets = [t for t in listed if shard_of(t["name"], total, eco) == k]
    log(f"{eco}: shard {k}/{total} holds {len(targets)} of {len(listed)} packages")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(t) + "\n" for t in targets), encoding="utf-8")
    os.replace(tmp, path)  # all or nothing
    meta = {
        "eco": eco,
        **wanted,
        "requested": n,
        "listed": len(listed),
        "targets": len(targets),
        "created": time.time(),
        "enumeration_bytes": tally.bytes,
        "enumeration_requests": tally.requests,
        "enumeration_seconds": round(time.monotonic() - start, 3),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return targets


def _one(eco, http, target, scan, validate: bool) -> dict:
    tally, start = Tally(), time.monotonic()
    try:
        rec = scan(http, target, tally, validate=validate)
    except TooLarge as exc:
        rec = new_record(eco, target["name"], target.get("version"))
        rec.update(status="skipped", reason=f"too large: {str(exc)[:200]}")
    except Exception as exc:  # one bad package must not stop the run
        rec = new_record(eco, target["name"], target.get("version"))
        rec.update(status="error", reason=f"{type(exc).__name__}: {str(exc)[:200]}")
    rec.update(
        key=key(target),
        bytes=tally.bytes,
        requests=tally.requests,
        seconds=round(time.monotonic() - start, 3),
    )
    return rec


def run(
    eco: str,
    out: Path,
    n: int | None,
    http: Http | None,
    *,
    window: tuple[int, int] | None = None,
    shard: tuple[int, int] = (1, 1),
    workers: int = 6,
    validate_pct: int = 0,
    recent: Callable | None = None,
    scan: Callable | None = None,
    redo: set[str] = frozenset(),
    log: Callable[[str], None] = print,
) -> dict:
    """Scan one shard of a round. Safe to stop with Ctrl-C and run again to continue.

    The round is either a fixed `window` of registry history (everything in it; `n`, if set,
    caps it) or the `n` most recent packages. `shard` (K, N) keeps only the packages whose
    name hashes to K, so N volunteers can split a round without overlap.
    `redo`: scan again the packages whose newest result has one of these statuses (for
    example after a fix). The new line is appended; readers keep the newest line per package.
    """
    private_dir(out).mkdir(parents=True, exist_ok=True)
    (private_dir(out) / "README.txt").write_text(PRIVATE_NOTE, encoding="utf-8")
    if recent is None and window is not None:
        lo, hi = window

        def recent(http, n, tally):
            return REGISTRIES[eco].in_range(http, lo, hi, tally)[:n]

    recent = recent or REGISTRIES[eco].recent
    scan = scan or REGISTRIES[eco].scan
    targets = _targets(eco, out, n, http, recent, log, window, shard)
    results_path = private_dir(out) / f"results-{eco}.jsonl"
    newest = {row.get("key"): row.get("status") for row in read_jsonl(results_path)}
    done = {k for k, status in newest.items() if status not in redo}
    todo = [t for t in targets if key(t) not in done]
    log(f"{eco}: {len(targets)} targets, {len(done & {key(t) for t in targets})} already done")

    session = {"eco": eco, "start": time.time(), "processed": 0, "interrupted": False}
    counts = {"candidates": 0, "secrets": 0, "errors": 0}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        with results_path.open("a", encoding="utf-8") as results:
            futures = [
                pool.submit(
                    _one, eco, http, t, scan, zlib.crc32(key(t).encode()) % 100 < validate_pct
                )
                for t in todo
            ]
            for future in as_completed(futures):
                rec = future.result()
                results.write(json.dumps(rec, ensure_ascii=False) + "\n")
                results.flush()
                session["processed"] += 1
                counts["candidates"] += rec["candidate_count"] > 0
                counts["secrets"] += bool(rec["secrets"])
                counts["errors"] += rec["status"] == "error"
                if session["processed"] % 100 == 0 or session["processed"] == len(todo):
                    log(
                        f"{eco}: {session['processed']}/{len(todo)} done; "
                        f"{counts['candidates']} with agent files, "
                        f"{counts['secrets']} with key-shaped strings, {counts['errors']} errors"
                    )
    except KeyboardInterrupt:
        session["interrupted"] = True
        log(f"{eco}: interrupted; run the same command again to continue")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        session["end"] = time.time()
        with (private_dir(out) / f"sessions-{eco}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(session) + "\n")
    return session
