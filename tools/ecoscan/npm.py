"""npm: find recently changed packages, then list each one's files as cheaply as possible.

Listing order of preference (see README.md, "Methodology"):
1. jsDelivr's file list for the exact version: a few hundred bytes, no archive download.
2. The registry tarball, downloaded only when (1) shows an agent-file path, when (1) fails,
   or for the validation sample that checks (1) against the tarball.
"""

from __future__ import annotations

from urllib.parse import quote

from agentprint.pack import read_archive_bytes

from .detect import examine, is_candidate, new_record
from .net import Http, HttpError, Tally, json_of

REPLICATE = "https://replicate.npmjs.com/registry"
REGISTRY = "https://registry.npmjs.org"
JSDELIVR = "https://data.jsdelivr.com/v1/packages/npm"
PAGE = 10_000  # the most the replication feed returns per request
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_TARBALL_BYTES = 64 * 1024 * 1024


def _url_name(name: str) -> str:
    return quote(name, safe="@/")  # scoped names keep their readable "@scope/name" form


def recent(
    http: Http, n: int, tally: Tally, *, window: int | None = None, skip_newest: int = 0
) -> list[dict]:
    """The `n` most recently changed packages, newest first, from the replication feed.

    The feed lists each package once, at its latest change. It cannot page backwards, so this
    reads forward from `update_seq - window`, widening the window until `n` live packages show.
    `skip_newest` leaves out the newest changes (about 15,000 an hour in Nov 2026): minutes-old
    versions are often not on jsDelivr yet, and spam is often removed within the hour.
    """
    root = json_of(http.get(f"{REPLICATE}/", tally=tally, max_bytes=MAX_JSON_BYTES), "npm root")
    hi = int(root["update_seq"]) - skip_newest
    window = window or max(4 * n, 1000)
    found: dict[str, int] = {}
    while len(found) < n and hi > 0:
        lo = max(0, hi - window)
        since = lo
        while since < hi:
            url = f"{REPLICATE}/_changes?since={since}&limit={PAGE}"
            page = json_of(http.get(url, tally=tally, max_bytes=MAX_JSON_BYTES), "npm changes")
            rows = page.get("results") or []
            for row in rows:
                seq = int(row["seq"])
                if seq > hi:
                    break
                if not row.get("deleted") and not str(row["id"]).startswith("_design/"):
                    found.setdefault(str(row["id"]), seq)
            last = int(page.get("last_seq") or since)
            if not rows or last <= since:
                break
            since = last
        hi, window = lo, window * 2
    newest = sorted(found.items(), key=lambda item: -item[1])[:n]
    return [{"name": name, "seq": seq} for name, seq in newest]


def scan(http: Http, target: dict, tally: Tally, *, validate: bool = False) -> dict:
    name = target["name"]
    rec = new_record("npm", name)
    resp = http.get(f"{REGISTRY}/{_url_name(name)}/latest", tally=tally, max_bytes=MAX_JSON_BYTES)
    if resp.status == 404:
        rec.update(status="gone", reason="no latest version (unpublished or no latest tag)")
        return rec
    manifest = json_of(resp, "npm manifest")
    version = str(manifest["version"])
    tarball = manifest["dist"]["tarball"]
    rec["version"] = version

    listed = None
    js_url = f"{JSDELIVR}/{_url_name(name)}@{quote(version, safe='')}?structure=flat"
    js = http.get(js_url, tally=tally, max_bytes=MAX_JSON_BYTES)
    if js.status == 200:
        listed = [str(f["name"]).lstrip("/") for f in json_of(js, "jsDelivr list")["files"]]
        rec["method"] = "jsdelivr"
    else:
        rec.update(method="tarball", jsdelivr_status=js.status)

    if listed is not None and not validate and not any(is_candidate(p) for p in listed):
        rec["files"] = len(listed)
        return rec

    if listed is not None:
        rec["method"] = "jsdelivr+tarball"
    resp = http.get(
        tarball,
        headers={"Accept-Encoding": "identity"},
        max_bytes=MAX_TARBALL_BYTES,
        tally=tally,
    )
    if resp.status == 404:  # npm removes spam and malware tarballs; the manifest lags behind
        rec.update(status="gone", reason="tarball removed")
        return rec
    if resp.status != 200:
        raise HttpError(f"HTTP {resp.status} for npm tarball")
    listing = read_archive_bytes(resp.body, "package.tgz", is_candidate)
    if validate and listed is not None:
        in_tarball = {f.split("/", 1)[1] if "/" in f else f for f in listing.files}
        rec["validated"] = in_tarball == set(listed)
        rec["validate_diff"] = len(in_tarball ^ set(listed))
    examine(rec, listing, "tarball", strip_root=True)
    return rec
