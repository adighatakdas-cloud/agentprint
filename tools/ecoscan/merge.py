"""Add up many volunteers' public summaries (one per shard) into round totals.

Only public summaries are accepted. Anything that looks like a private file (redacted values,
package names, paths) is refused, so a mistake cannot leak into the round total.
"""

from __future__ import annotations

import json
from pathlib import Path

from .summary import ECOSYSTEMS, derive

DERIVED = {"round", "error_rate", "bytes_per_package", "seconds_per_package", "projection"}
PRIVATE_KEYS = {"package", "name", "version", "path", "redacted", "candidates", "secrets", "key"}


class MergeError(ValueError):
    """A file cannot be merged; the message says why in plain words."""


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def load(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if "…(len" in text or "\\u2026(len" in text:
        raise MergeError(
            f"{path}: holds redacted values, so it looks like a private findings file. "
            "Never post or merge it; send it privately instead "
            "(see https://adighatakdas-cloud.github.io/agentprint/census/)."
        )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MergeError(f"{path}: not one JSON document (a private .jsonl file?)") from exc
    if not isinstance(data, dict) or data.get("kind") != "ecoscan-summary":
        raise MergeError(f"{path}: not a public summary (expected public/summary.json)")
    if _keys(data) & PRIVATE_KEYS:
        raise MergeError(f"{path}: not a public summary: it has names or paths in it")
    return data


def _add(total: dict, part: dict) -> None:
    for name, value in part.items():
        if isinstance(value, dict):
            _add(total.setdefault(name, {}), value)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            total[name] = total.get(name, 0) + value


def merge(paths: list[Path]) -> dict:
    summaries = [(Path(p), load(p)) for p in paths]
    result = {}
    for eco in ECOSYSTEMS:
        parts = [(p, s["ecosystems"][eco]) for p, s in summaries if eco in s["ecosystems"]]
        if not parts:
            continue
        rounds = {json.dumps(part["round"]["window"]) for _p, part in parts}
        totals = {part["round"]["shard"][1] for _p, part in parts if part["round"]["shard"]}
        if None in [part["round"]["window"] for _p, part in parts]:
            raise MergeError(f"{eco}: only summaries from a shared --window round can be merged")
        if len(rounds) > 1:
            raise MergeError(f"{eco}: summaries come from different windows: {sorted(rounds)}")
        if len(totals) != 1:
            raise MergeError(f"{eco}: summaries use different shard counts: {sorted(totals)}")
        seen: dict[int, Path] = {}
        total: dict = {}
        incomplete = []
        for path, part in parts:
            k = part["round"]["shard"][0]
            if k in seen:
                raise MergeError(f"{eco}: shard {k} appears twice ({seen[k]} and {path})")
            seen[k] = path
            if part["processed"] < part["targets"]:
                incomplete.append(k)
            _add(total, {name: v for name, v in part.items() if name not in DERIVED})
        n = totals.pop()
        result[eco] = {
            "round": {
                "window": parts[0][1]["round"]["window"],
                "shards": n,
                "merged": sorted(seen),
                "missing": [k for k in range(1, n + 1) if k not in seen],
                "incomplete": sorted(incomplete),
            },
            **derive(total),
        }
    return {
        "kind": "ecoscan-merged-summary",
        "schema": 1,
        "note": "Counts only, added up from volunteers' public summaries. Safe to post.",
        "inputs": len(summaries),
        "ecosystems": result,
    }
