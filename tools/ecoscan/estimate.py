"""Up-front cost estimate for a shard, from the November 2026 pilot.

Per package, measured on 2,000 npm packages and 2,000 PyPI releases, then recomputed as if the
default size caps had applied (npm: skip packages over 150 MB; PyPI: sdists over 10 MB).
Time is wall time at the fixed request rates, so it does not depend on your connection much.
"""

from __future__ import annotations

PER_PACKAGE = {
    "npm": (180_000, 0.63),  # bytes, seconds (jsDelivr's 1.6 requests/s sets the pace)
    "pypi": (730_000, 0.52),  # mostly whole .tar.gz sdists
}


def estimate(eco: str, packages: int) -> tuple[int, float]:
    per_bytes, per_seconds = PER_PACKAGE[eco]
    return per_bytes * packages, round(per_seconds * packages, 2)


def describe(eco: str, packages: int) -> str:
    size, seconds = estimate(eco, packages)
    took = f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{max(1, round(seconds / 60))} min"
    return (
        f"{eco}: {packages} packages to scan; estimate about {size / 1e9:.2f} GB and "
        f"{took} (Nov 2026 pilot, default size caps)"
    )
