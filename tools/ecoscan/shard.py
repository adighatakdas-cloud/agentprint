"""Splitting one scan round between many volunteers.

A round is a fixed window of registry history plus a shard count:
- npm:  `--window FROM:TO` is a range of replication-feed change numbers (seq), FROM < seq <= TO.
- PyPI: `--window FROM:TO` is a range of changelog serials, FROM < serial <= TO.
Everyone in a round uses the same window and the same N; each volunteer takes one K of
`--shard K/N`. A package belongs to exactly one shard, decided by a SHA-256 hash of its name,
so the result is identical on every machine and Python version.
"""

from __future__ import annotations

import hashlib
import re


def _norm(name: str, eco: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if eco == "pypi" else name  # PEP 503 for PyPI


def shard_of(name: str, total: int, eco: str) -> int:
    """Which shard (1..total) a package belongs to."""
    digest = hashlib.sha256(_norm(name, eco).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % total + 1


def parse_shard(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)/(\d+)", text.strip())
    if not match:
        raise ValueError(f"shard must look like K/N, for example 3/10 (got {text!r})")
    k, n = int(match.group(1)), int(match.group(2))
    if not 1 <= k <= n:
        raise ValueError(f"shard K/N needs 1 <= K <= N (got {text!r})")
    return k, n


def parse_window(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+):(\d+)", text.strip())
    if not match:
        raise ValueError(f"window must look like FROM:TO, for example 100000:250000 (got {text!r})")
    lo, hi = int(match.group(1)), int(match.group(2))
    if lo >= hi:
        raise ValueError(f"window FROM must be smaller than TO (got {text!r})")
    return lo, hi
