"""Wall-clock checks for the "stays fast" tests that hold on a busy or traced machine.

A single timing on a loaded CI runner, or under coverage (which slows Python loops 2-3x), can
miss a bound the code meets easily. So each check takes the best of a few runs, and the bound
is three times looser while coverage or another tracer is active.
"""

import sys
import time
from collections.abc import Callable


def tracing() -> bool:
    """True under coverage or a debugger/profiler, which slow every Python line."""
    return sys.gettrace() is not None or "coverage" in sys.modules


def bound(seconds: float = 1.0) -> float:
    """The time limit to assert: `seconds` normally, three times that while tracing."""
    return seconds * 3 if tracing() else seconds


def best_of(run: Callable[[], object], times: int = 3) -> float:
    """The fastest of `times` runs of `run`, in seconds."""
    best = float("inf")
    for _ in range(times):
        start = time.perf_counter()
        run()
        best = min(best, time.perf_counter() - start)
    return best
