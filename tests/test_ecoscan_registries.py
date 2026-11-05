import io
import json
import tarfile
import xmlrpc.client
import zipfile

import pytest

from agentprint.secrets import redact
from tests import fakes
from tools.ecoscan import npm, pypi
from tools.ecoscan.net import Http, RateLimiter, Response, Tally, TooLarge

LEAKY = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}).encode()


class Web:
    """An in-memory internet: canned replies by URL, Range support, size limits, a call log."""

    def __init__(self):
        self.routes: dict = {}
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, url, body=b"", status=200, headers=None):
        self.routes[url] = (status, body, headers or {})

    def json(self, url, obj, status=200):
        self.add(url, json.dumps(obj).encode(), status)

    def urls(self):
        return [url for _method, url, _headers in self.calls]

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers)))
        route = self.routes.get(url)
        if callable(route):
            return route(method, url, headers, body)
        if route is None:
            return Response(404, {}, b"not found", 9)
        status, data, hdrs = route
        rng = headers.get("Range")
        if rng and status == 200:
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            full = len(data)
            data = data[start : end + 1]
            status = 206
            hdrs = {**hdrs, "content-range": f"bytes {start}-{start + len(data) - 1}/{full}"}
        if max_bytes is not None and len(data) > max_bytes:
            raise TooLarge("too big")
        return Response(status, hdrs, data, len(data))


def _http(web):
    return Http(RateLimiter(clock=lambda: 0.0, sleep=lambda s: None), transport=web)


def _tgz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- npm ------------------------------------------------------------------------------------

TARBALL = "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz"


def _npm_web(listing, tarball_members, jsdelivr_status=200):
    web = Web()
    web.json(
        "https://registry.npmjs.org/demo/latest",
        {"name": "demo", "version": "1.0.0", "dist": {"tarball": TARBALL, "unpackedSize": 9}},
    )
    web.json(
        "https://data.jsdelivr.com/v1/packages/npm/demo@1.0.0?structure=flat",
        {"files": [{"name": "/" + n} for n in listing]},
        status=jsdelivr_status,
    )
    web.add(TARBALL, _tgz({"package/" + n: d for n, d in tarball_members.items()}))
    return web


def test_npm_leaky_package_records_candidates_and_redacted_keys():
    files = {"index.js": b"1", ".claude/settings.local.json": LEAKY}
    web = _npm_web(files, files)
    rec = npm.scan(_http(web), {"name": "demo"}, Tally())
    assert (rec["status"], rec["version"], rec["method"]) == ("ok", "1.0.0", "jsdelivr+tarball")
    assert rec["files"] == 2
    assert rec["candidates"] == [
        {
            "archive": "tarball",
            "path": ".claude/settings.local.json",
            "agent": "claude-code",
            "kind": "settings",
            "ap006": True,
        }
    ]
    [secret] = rec["secrets"]
    assert secret["rule"] == "anthropic"
    assert secret["redacted"] == redact(fakes.ANTHROPIC)
    assert (secret["path"], secret["line"]) == (".claude/settings.local.json", 1)
    assert fakes.ANTHROPIC not in json.dumps(rec)


def test_npm_clean_package_never_downloads_the_tarball():
    files = {"index.js": b"1", "README.md": b"hi"}
    web = _npm_web(files, files)
    rec = npm.scan(_http(web), {"name": "demo"}, Tally())
    assert (rec["status"], rec["files"], rec["candidates"]) == ("ok", 2, [])
    assert TARBALL not in web.urls()


def test_npm_falls_back_to_tarball_when_jsdelivr_fails():
    files = {"index.js": b"1", "AGENTS.md": b"be nice"}
    web = _npm_web(files, files, jsdelivr_status=404)
    rec = npm.scan(_http(web), {"name": "demo"}, Tally())
    assert rec["method"] == "tarball"
    assert [c["path"] for c in rec["candidates"]] == ["AGENTS.md"]


def test_npm_validation_compares_jsdelivr_with_the_tarball():
    web = _npm_web({"index.js": b"1"}, {"index.js": b"1", "extra.js": b"2"})
    rec = npm.scan(_http(web), {"name": "demo"}, Tally(), validate=True)
    assert (rec["validated"], rec["validate_diff"]) == (False, 1)
    assert rec["files"] == 2  # the tarball is the ground truth


def test_npm_unpublished_package_is_gone():
    web = Web()
    rec = npm.scan(_http(web), {"name": "demo"}, Tally())
    assert rec["status"] == "gone"


def test_npm_scoped_names_stay_readable_in_urls():
    web = Web()
    npm.scan(_http(web), {"name": "@scope/demo"}, Tally())
    assert web.urls() == ["https://registry.npmjs.org/@scope/demo/latest"]


def _feed_web():
    changes = [{"seq": s, "id": f"p{s}"} for s in range(1, 31)]
    for row in changes:
        if row["seq"] % 3 == 0:
            row["deleted"] = True
    web = Web()
    web.json("https://replicate.npmjs.com/registry/", {"update_seq": 30})

    def feed(method, url, headers, body):
        since = int(url.split("since=")[1].split("&")[0])
        rows = [r for r in changes if r["seq"] > since][:4]  # tiny pages
        last = rows[-1]["seq"] if rows else since
        return Response(200, {}, json.dumps({"results": rows, "last_seq": last}).encode(), 1)

    for since in range(31):
        web.routes[f"https://replicate.npmjs.com/registry/_changes?since={since}&limit=10000"] = (
            feed
        )
    return web


def test_npm_recent_lists_newest_unique_live_packages():
    got = npm.recent(_http(_feed_web()), 5, Tally(), window=4)
    assert [t["name"] for t in got] == ["p29", "p28", "p26", "p25", "p23"]


def test_npm_recent_can_skip_the_newest_changes():
    # The very newest versions are often not on jsDelivr yet, or get removed within minutes.
    got = npm.recent(_http(_feed_web()), 3, Tally(), window=4, skip_newest=10)
    assert [t["name"] for t in got] == ["p20", "p19", "p17"]


def test_npm_oversized_tarball_keeps_the_jsdelivr_names(monkeypatch):
    monkeypatch.setattr(npm, "MAX_TARBALL_BYTES", 10)
    files = {"index.js": b"1", ".claude/settings.local.json": LEAKY}
    rec = npm.scan(_http(_npm_web(files, files)), {"name": "demo"}, Tally())
    assert (rec["status"], rec["too_large"]) == ("ok", ["tarball"])
    assert [c["path"] for c in rec["candidates"]] == [".claude/settings.local.json"]
    assert rec["not_scanned"] == [".claude/settings.local.json"]
    assert rec["files"] == 2


def test_npm_oversized_tarball_without_a_list_is_skipped(monkeypatch):
    monkeypatch.setattr(npm, "MAX_TARBALL_BYTES", 10)
    files = {"index.js": b"1"}
    with pytest.raises(TooLarge):
        npm.scan(_http(_npm_web(files, files, jsdelivr_status=403)), {"name": "demo"}, Tally())


def test_npm_removed_tarball_is_gone_not_an_error():
    files = {"index.js": b"1", "AGENTS.md": b"x"}
    web = _npm_web(files, files)
    web.add(TARBALL, b"", status=404)
    rec = npm.scan(_http(web), {"name": "demo"}, Tally())
    assert (rec["status"], rec["reason"]) == ("gone", "tarball removed")


# --- PyPI -----------------------------------------------------------------------------------

JSON_URL = "https://pypi.org/pypi/demo/1.0/json"
SDIST = "https://files.pythonhosted.org/packages/demo-1.0.tar.gz"
WHEEL = "https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl"


def _pypi_web(sdist_members, wheel_members, sdist_size=None):
    web = Web()
    sdist = _tgz({"demo-1.0/" + n: d for n, d in sdist_members.items()})
    wheel = _zip(wheel_members)
    urls = [
        {
            "packagetype": "sdist",
            "filename": "demo-1.0.tar.gz",
            "url": SDIST,
            "size": sdist_size or len(sdist),
        },
        {
            "packagetype": "bdist_wheel",
            "filename": "demo-1.0-py3-none-any.whl",
            "url": WHEEL,
            "size": len(wheel),
        },
    ]
    web.json(JSON_URL, {"info": {"name": "demo"}, "urls": urls})
    web.add(SDIST, sdist)
    web.add(WHEEL, wheel)
    return web


def test_pypi_scans_sdist_and_wheel():
    web = _pypi_web(
        {"setup.py": b"", ".claude/settings.local.json": LEAKY},
        {"demo/__init__.py": b"", "demo/.cursor/mcp.json": b"{}"},
    )
    rec = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally())
    assert rec["status"] == "ok"
    assert [(c["archive"], c["path"]) for c in rec["candidates"]] == [
        ("sdist", ".claude/settings.local.json"),
        ("wheel", "demo/.cursor/mcp.json"),
    ]
    [secret] = rec["secrets"]
    assert (secret["archive"], secret["rule"]) == ("sdist", "anthropic")
    assert fakes.ANTHROPIC not in json.dumps(rec)
    assert rec["files"] == 4


def test_pypi_oversized_sdist_is_noted_and_wheel_still_scanned(monkeypatch):
    monkeypatch.setattr(pypi, "MAX_DOWNLOAD_BYTES", 10)
    web = _pypi_web({"setup.py": b""}, {"demo/__init__.py": b""}, sdist_size=10_000)
    rec = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally())
    assert rec["status"] == "ok"
    assert rec["too_large"] == ["sdist"]
    assert SDIST not in web.urls()


def test_pypi_failed_wheel_read_keeps_the_sdist_results():
    web = _pypi_web({"setup.py": b"", ".claude/settings.local.json": LEAKY}, {"demo/x.py": b""})
    web.add(WHEEL, b"", status=503)  # never recovers
    rec = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally())
    assert rec["status"] == "partial"
    assert [c["path"] for c in rec["candidates"]] == [".claude/settings.local.json"]
    assert len(rec["secrets"]) == 1
    [problem] = rec["archive_errors"]
    assert problem.startswith("wheel: ")


def test_pypi_release_with_no_readable_archive_is_an_error():
    web = _pypi_web({"setup.py": b""}, {"demo/x.py": b""})
    web.add(SDIST, b"", status=503)
    web.add(WHEEL, b"", status=503)
    rec = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally())
    assert (rec["status"], rec["reason"]) == ("error", "no archive could be read")


def test_pypi_removed_release_is_gone():
    rec = pypi.scan(_http(Web()), {"name": "demo", "version": "1.0"}, Tally())
    assert rec["status"] == "gone"


def test_pypi_release_without_sdist_or_wheel_is_skipped():
    web = Web()
    web.json(JSON_URL, {"info": {}, "urls": [{"packagetype": "bdist_egg", "filename": "x.egg"}]})
    rec = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally())
    assert (rec["status"], rec["reason"]) == ("skipped", "no sdist or wheel")


def test_pypi_recent_lists_newest_release_per_project():
    events = [
        ("a", "1.0", 0, "new release", 10),
        ("b", "2.0", 0, "new release", 15),
        ("a", "1.0", 0, "add source file a-1.0.tar.gz", 16),
        ("A", "1.1", 0, "new release", 20),
        ("c", "0.1", 0, "new release", 25),
        ("c", "0.1", 0, "remove release", 26),
        ("d", "3.0", 0, "new release", 30),
    ]
    web = Web()

    def rpc(method, url, headers, body):
        params, name = xmlrpc.client.loads(body)
        if name == "changelog_last_serial":
            result = 30
        else:
            result = [list(e) for e in events if e[4] > params[0]]
        reply = xmlrpc.client.dumps((result,), methodresponse=True).encode()
        return Response(200, {}, reply, len(reply))

    web.routes["https://pypi.org/pypi"] = rpc
    got = pypi.recent(_http(web), 10, Tally(), step=12)
    assert [(t["name"], t["version"]) for t in got] == [("d", "3.0"), ("A", "1.1"), ("b", "2.0")]
    older = pypi.recent(_http(web), 10, Tally(), step=12, skip_newest=8)
    assert [(t["name"], t["version"]) for t in older] == [("A", "1.1"), ("b", "2.0")]
