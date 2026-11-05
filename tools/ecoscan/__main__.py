"""Volunteer research scan. See docs/VOLUNTEER_SCAN.md.

python -m tools.ecoscan window npm|pypi [--packages N]             print a round's bounds
python -m tools.ecoscan run npm|pypi --window FROM:TO --shard K/N --out DIR
python -m tools.ecoscan summary --out DIR                          rebuild the two outputs
"""

from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path

from . import npm, pypi
from .net import Http, RateLimiter, Tally
from .run import REGISTRIES, run
from .shard import parse_shard, parse_window
from .summary import FULL_RUN, finish

# Fixed on purpose: volunteers cannot make the scan faster or ruder. Requests per second, per
# host. jsDelivr asks heavy users to stay under 100 requests a minute or ask first.
DEFAULT_RATE = 4.0
HOST_RATES = {"data.jsdelivr.com": 1.6, "replicate.npmjs.com": 1.0}
# About one hour of changes (measured Nov 2026): lets uploads, CDNs and spam removal settle.
SKIP_NEWEST = {"npm": 15_000, "pypi": 2_000}
# Feed entries per scanned package in the Nov 2026 pilot (npm change numbers, PyPI serials).
ENTRIES_PER_PACKAGE = {"npm": 3.5, "pypi": 6.0}
# Size caps can be raised, but never past this: everything is read in memory.
MAX_SIZE_CAP_MB = 256


def _arg(parse):
    def convert(text: str):
        try:
            return parse(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return convert


def _size_mb(text: str) -> float:
    value = float(text)
    if not 1 <= value <= MAX_SIZE_CAP_MB:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_SIZE_CAP_MB} (MB)")
    return value


def _gb(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be more than 0")
    return value


def _http() -> Http:
    return Http(RateLimiter(default=DEFAULT_RATE, rates=HOST_RATES))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ecoscan",
        description="Research scan: agent files and key-shaped strings in recent npm/PyPI "
        "packages. Keys are never used and always redacted. Only public/summary.json is "
        "safe to post.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    win = sub.add_parser("window", help="print current bounds for a new scan round")
    win.add_argument("eco", choices=["npm", "pypi"])
    win.add_argument("--packages", type=int, default=FULL_RUN, help="about how many packages")

    go = sub.add_parser("run", help="scan one shard of a round")
    go.add_argument("eco", choices=["npm", "pypi"])
    go.add_argument(
        "--window",
        type=_arg(parse_window),
        help="the round's range: npm change numbers or PyPI changelog serials, FROM:TO",
    )
    go.add_argument("--shard", type=_arg(parse_shard), default=(1, 1), help="K/N, e.g. 3/10")
    go.add_argument("--limit", type=int, help="without --window: this many newest packages")
    go.add_argument("--out", type=Path, required=True, help="output folder (keep it out of git)")
    go.add_argument("--workers", type=int, default=6, choices=range(1, 9), metavar="1-8")
    go.add_argument("--max-gb", type=_gb, help="stop after downloading this much (all runs)")
    go.add_argument(
        "--npm-max-mb",
        type=_size_mb,
        default=npm.DEFAULT_MAX_MB,
        help=f"skip npm packages bigger than this, unpacked (default {npm.DEFAULT_MAX_MB})",
    )
    go.add_argument(
        "--sdist-max-mb",
        type=_size_mb,
        default=pypi.DEFAULT_SDIST_MAX_MB,
        help=f"don't download PyPI sdists bigger than this (default {pypi.DEFAULT_SDIST_MAX_MB})",
    )
    go.add_argument(
        "--skip-newest",
        type=int,
        default=None,
        help="with --limit: leave out this many newest feed entries (default: about an hour)",
    )
    go.add_argument(
        "--redo",
        action="append",
        default=[],
        choices=["error", "skipped", "gone", "partial"],
        help="scan again packages whose last result had this status (repeatable)",
    )
    go.add_argument(
        "--validate-pct",
        type=int,
        default=0,
        help="npm: also download this %% of tarballs to check jsDelivr's file lists",
    )

    report = sub.add_parser("summary", help="rebuild public/summary.json and private findings")
    report.add_argument("--out", type=Path, required=True)
    report.add_argument("--full", type=int, default=FULL_RUN, help="packages to project for")
    args = parser.parse_args(argv)

    if args.command == "window":
        hi = REGISTRIES[args.eco].head(_http(), Tally()) - SKIP_NEWEST[args.eco]
        lo = max(0, hi - round(args.packages * ENTRIES_PER_PACKAGE[args.eco]))
        note = f"about {args.packages:,} packages; the newest hour is left out"
        print(f"{args.eco} --window {lo}:{hi}   ({note})")
        return 0

    if args.command == "run":
        if args.window is None and args.limit is None:
            parser.error("run needs --window FROM:TO (a shared round) or --limit N")
        recent = None
        if args.window is None:
            skip = SKIP_NEWEST[args.eco] if args.skip_newest is None else args.skip_newest
            recent = partial(REGISTRIES[args.eco].recent, skip_newest=skip)
        if args.eco == "npm":
            scan = partial(npm.scan, max_mb=args.npm_max_mb)
        else:
            scan = partial(pypi.scan, sdist_max_mb=args.sdist_max_mb)
        try:
            session = run(
                args.eco,
                args.out,
                args.limit,
                _http(),
                window=args.window,
                shard=args.shard,
                workers=args.workers,
                validate_pct=args.validate_pct,
                redo=set(args.redo),
                recent=recent,
                scan=scan,
                max_bytes=None if args.max_gb is None else round(args.max_gb * 1e9),
            )
        except ValueError as exc:
            parser.error(str(exc))
        if session["interrupted"]:
            return 130
        print(finish(args.out))
        return 0

    print(finish(args.out, full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
