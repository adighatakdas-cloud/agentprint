"""python -m tools.ecoscan run npm|pypi --limit N --out DIR   |   summary --out DIR"""

from __future__ import annotations

import argparse
import json
from functools import partial
from pathlib import Path

from .net import Http, RateLimiter
from .run import REGISTRIES, run
from .summary import FULL_RUN, export_findings, render, summarize

# Fixed on purpose: volunteers cannot make the scan faster or ruder. Requests per second, per
# host. jsDelivr asks heavy users to stay under 100 requests a minute or ask first.
DEFAULT_RATE = 4.0
HOST_RATES = {"data.jsdelivr.com": 1.6, "replicate.npmjs.com": 1.0}
# About one hour of changes (measured Nov 2026): lets uploads, CDNs and spam removal settle.
SKIP_NEWEST = {"npm": 15_000, "pypi": 2_000}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ecoscan",
        description="Research scan: agent files and key-shaped strings in recent npm/PyPI "
        "packages. Output is redacted and must stay out of git (use a scans/ folder).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="scan the most recent packages of one registry")
    go.add_argument("eco", choices=["npm", "pypi"])
    go.add_argument("--limit", type=int, required=True, help="how many recent packages")
    go.add_argument("--out", type=Path, required=True, help="output folder (gitignored)")
    go.add_argument("--workers", type=int, default=6, choices=range(1, 9), metavar="1-8")
    go.add_argument(
        "--skip-newest",
        type=int,
        default=None,
        help="leave out this many newest feed entries (default: about one hour's worth)",
    )
    go.add_argument(
        "--redo",
        action="append",
        default=[],
        choices=["error", "skipped", "gone"],
        help="scan again packages whose last result had this status (repeatable)",
    )
    go.add_argument(
        "--validate-pct",
        type=int,
        default=0,
        help="npm: also download this %% of tarballs to check jsDelivr's file lists",
    )
    report = sub.add_parser("summary", help="counts only: no names, paths or values")
    report.add_argument("--out", type=Path, required=True)
    report.add_argument("--full", type=int, default=FULL_RUN, help="packages to project for")
    args = parser.parse_args(argv)

    if args.command == "run":
        limiter = RateLimiter(default=DEFAULT_RATE, rates=HOST_RATES)
        skip = SKIP_NEWEST[args.eco] if args.skip_newest is None else args.skip_newest
        session = run(
            args.eco,
            args.out,
            args.limit,
            Http(limiter),
            workers=args.workers,
            validate_pct=args.validate_pct,
            redo=set(args.redo),
            recent=partial(REGISTRIES[args.eco].recent, skip_newest=skip),
        )
        return 130 if session["interrupted"] else 0

    summary = summarize(args.out, full=args.full)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    export_findings(args.out)
    print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
