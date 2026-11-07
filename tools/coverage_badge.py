"""Turn a coverage.py JSON report into a shields.io endpoint file for the README badge.

Usage: python -m tools.coverage_badge coverage.json site/public/coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def badge(percent: float) -> dict[str, object]:
    """The shields.io endpoint payload for a coverage percentage (rounded down)."""
    return {
        "schemaVersion": 1,
        "label": "coverage",
        "message": f"{int(percent)}%",
        "color": "FFB020",
        "labelColor": "050B14",
    }


def write_badge(report: Path, out: Path) -> None:
    percent = json.loads(report.read_text(encoding="utf-8"))["totals"]["percent_covered"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(badge(percent)) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.coverage_badge <coverage.json> <out.json>", file=sys.stderr)
        return 2
    write_badge(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
