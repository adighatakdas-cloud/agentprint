"""PyPI: find recent releases, then list the files of each release's sdist and one wheel.

- Recent releases come from PyPI's changelog (XML-RPC `changelog_since_serial`, the mirroring
  interface PyPI still supports): one request returns tens of thousands of events.
- Zips (wheels, old .zip sdists) are read with HTTP Range requests: the directory at the end
  of the file first, then only the agent-file members. The rest is never downloaded.
- A .tar.gz sdist has no directory, so it is downloaded whole and read in memory.
"""

from __future__ import annotations

import re
import xmlrpc.client
from urllib.parse import quote

from agentprint.pack import PackError, read_archive_bytes, read_archive_fileobj

from .detect import examine, is_candidate, new_record
from .net import Http, HttpError, RangeFile, Tally, TooLarge, json_of

PYPI = "https://pypi.org"
MAX_JSON_BYTES = 32 * 1024 * 1024
# Default cap for whole-file downloads (.tar.gz sdists). In the pilot, sdists over 10 MB were
# ~4% of releases but half the bandwidth. Volunteers can raise it with --sdist-max-mb.
DEFAULT_SDIST_MAX_MB = 10
MAX_DOWNLOAD_BYTES = DEFAULT_SDIST_MAX_MB * 1_000_000
MAX_RANGE_BYTES = 64 * 1024 * 1024  # bytes read from one zip with Range requests


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503


def _rpc(http: Http, method: str, *params, tally: Tally):
    body = xmlrpc.client.dumps(params, method).encode()
    resp = http.post(
        f"{PYPI}/pypi",
        body,
        headers={"Content-Type": "text/xml"},
        max_bytes=MAX_JSON_BYTES,
        tally=tally,
    )
    if resp.status != 200:
        raise HttpError(f"HTTP {resp.status} for PyPI {method}")
    (result,), _ = xmlrpc.client.loads(resp.body)
    return result


def recent(
    http: Http, n: int, tally: Tally, *, step: int = 20_000, skip_newest: int = 0
) -> list[dict]:
    """The newest release of each of the `n` most recently released projects, newest first.

    Releases that were deleted again inside the window are left out. `skip_newest` leaves out
    the newest changelog serials (about 1,900 an hour in Nov 2026), so files have finished
    uploading.
    """
    hi = head(http, tally) - skip_newest
    found: dict[str, dict] = {}
    removed: set[tuple[str, str | None]] = set()
    while len(found) < n and hi > 0:
        lo = max(0, hi - step)
        _pick(_events(http, lo, hi, tally), found, removed)
        hi = lo
    return sorted(found.values(), key=lambda t: -t["serial"])[:n]


def head(http: Http, tally: Tally) -> int:
    """The newest changelog serial."""
    return int(_rpc(http, "changelog_last_serial", tally=tally))


def in_range(http: Http, lo: int, hi: int, tally: Tally) -> list[dict]:
    """The newest release of every project released in serials (lo, hi], newest first.

    This is a scan round's window. The changelog never changes, so every volunteer gets the
    same list (releases deleted later show up as "gone" when scanned).
    """
    found: dict[str, dict] = {}
    _pick(_events(http, lo, hi, tally), found, set())
    return sorted(found.values(), key=lambda t: -t["serial"])


def _events(http: Http, lo: int, hi: int, tally: Tally) -> list:
    rows, since = [], lo
    while since < hi:  # page forward in case the server caps rows per call
        batch = _rpc(http, "changelog_since_serial", since, tally=tally)
        if not batch:
            break
        rows += [r for r in batch if r[4] <= hi]
        top = max(r[4] for r in batch)
        if top >= hi or top <= since:
            break
        since = top
    return rows


def _pick(rows: list, found: dict, removed: set) -> None:
    """Newest first: keep each project's newest release unless it was removed afterwards."""
    for name, version, _when, action, serial in sorted(rows, key=lambda r: -r[4]):
        key = _norm(name)
        if action == "remove project":
            removed.add((key, None))
        elif action == "remove release":
            removed.add((key, version))
        elif (
            action == "new release"
            and key not in found
            and (key, version) not in removed
            and (key, None) not in removed
        ):
            found[key] = {"name": name, "version": version, "serial": serial}


def _listing(http: Http, archive: str, info: dict, tally: Tally, rec: dict, cap: int):
    filename, url, size = info["filename"], info["url"], int(info.get("size") or 0)
    if filename.endswith((".zip", ".whl")):
        rec["method"].append(f"{archive}:range")
        remote = RangeFile(http, url, size, max_bytes=MAX_RANGE_BYTES, tally=tally)
        return read_archive_fileobj(remote, filename, is_candidate)
    if size > cap:
        raise TooLarge(f"{size} bytes")
    rec["method"].append(f"{archive}:full")
    resp = http.get(url, headers={"Accept-Encoding": "identity"}, max_bytes=cap, tally=tally)
    if resp.status != 200:
        raise HttpError(f"HTTP {resp.status} for {archive}")
    return read_archive_bytes(resp.body, filename, is_candidate)


def scan(
    http: Http,
    target: dict,
    tally: Tally,
    *,
    validate: bool = False,
    sdist_max_mb: float | None = None,
) -> dict:
    cap = MAX_DOWNLOAD_BYTES if sdist_max_mb is None else int(sdist_max_mb * 1_000_000)
    name, version = target["name"], target["version"]
    rec = new_record("pypi", name, version)
    url = f"{PYPI}/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    resp = http.get(url, tally=tally, max_bytes=MAX_JSON_BYTES)
    if resp.status == 404:
        rec.update(status="gone", reason="release or project removed")
        return rec
    files = json_of(resp, "PyPI release").get("urls") or []
    sdists = [f for f in files if f.get("packagetype") == "sdist"]
    wheels = [f for f in files if f.get("packagetype") == "bdist_wheel"]
    picks = [("sdist", sdists[0])] if sdists else []
    if wheels:  # wheels of one release come from the same tree; the smallest stands for all
        picks.append(("wheel", min(wheels, key=lambda f: int(f.get("size") or 0))))
    if not picks:
        rec.update(status="skipped", reason="no sdist or wheel")
        return rec
    rec["method"], rec["archive_errors"] = [], []
    read = 0
    for archive, info in picks:
        try:
            listing = _listing(http, archive, info, tally, rec, cap)
        except TooLarge:
            rec["too_large"].append(archive)
            continue
        except (PackError, HttpError) as exc:  # one bad archive must not sink the other
            rec["archive_errors"].append(f"{archive}: {str(exc)[:200]}")
            continue
        examine(rec, listing, archive, strip_root=archive == "sdist")
        read += 1
    rec["method"] = "+".join(rec["method"]) or None
    if rec["archive_errors"]:
        if read:
            rec.update(status="partial", reason="an archive could not be read")
        else:
            rec.update(status="error", reason="no archive could be read")
    return rec
