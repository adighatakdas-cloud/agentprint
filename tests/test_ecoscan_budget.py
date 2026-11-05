import json

import pytest

from tests.test_ecoscan_registries import (
    JSON_URL,
    SDIST,
    TARBALL,
    _http,
    _npm_web,
    _pypi_web,
)
from tests.test_ecoscan_run import Enumerator, _ok_scan
from tools.ecoscan import npm, pypi
from tools.ecoscan.__main__ import main
from tools.ecoscan.estimate import estimate
from tools.ecoscan.net import Tally
from tools.ecoscan.run import run


def _heavy(http, target, tally, *, validate=False):
    tally.add(400_000_000)  # 0.4 GB per package
    return _ok_scan(http, target, tally)


def _results(out):
    return (out / "private" / "results-npm.jsonl").read_text().splitlines()


def test_max_gb_stops_cleanly_and_a_rerun_continues(tmp_path):
    enum = Enumerator([f"p{i}" for i in range(10)])
    logs = []
    first = run(
        "npm",
        tmp_path,
        None,
        None,
        workers=1,
        recent=enum,
        scan=_heavy,
        max_bytes=10**9,
        log=logs.append,
    )
    assert first["stopped"] == "max-gb"
    assert len(_results(tmp_path)) == 3  # 0.4 + 0.4 + 0.4 GB passes the 1 GB budget
    assert any("--max-gb" in line for line in logs)
    second = run(
        "npm",
        tmp_path,
        None,
        None,
        workers=1,
        recent=enum,
        scan=_heavy,
        max_bytes=2 * 10**9,
        log=logs.append,
    )
    assert second["stopped"] == "max-gb" and len(_results(tmp_path)) == 5
    run("npm", tmp_path, None, None, workers=1, recent=enum, scan=_heavy, log=logs.append)
    assert len(_results(tmp_path)) == 10


def test_estimate_comes_from_the_pilot_with_default_caps():
    assert estimate("npm", 1000) == (180_000_000, 630.0)
    assert estimate("pypi", 1000) == (730_000_000, 520.0)


def test_estimate_is_shown_before_scanning(tmp_path):
    logs = []
    run("npm", tmp_path, None, None, recent=Enumerator(["a", "b"]), scan=_ok_scan, log=logs.append)
    first_scan_line = next(i for i, line in enumerate(logs) if "done;" in line)
    assert any("estimate" in line and "GB" in line for line in logs[:first_scan_line])


def test_npm_skips_packages_over_the_size_cap_without_downloading():
    files = {"index.js": b"1", ".claude/settings.local.json": b"{}"}
    web = _npm_web(files, files)
    manifest = json.loads(web.routes["https://registry.npmjs.org/demo/latest"][1])
    manifest["dist"]["unpackedSize"] = 200_000_000
    web.json("https://registry.npmjs.org/demo/latest", manifest)
    rec = npm.scan(_http(web), {"name": "demo"}, Tally())
    assert (rec["status"], rec["too_large"]) == ("skipped", ["package"])
    assert web.urls() == ["https://registry.npmjs.org/demo/latest"]  # nothing else fetched
    lifted = npm.scan(_http(web), {"name": "demo"}, Tally(), max_mb=256)
    assert lifted["status"] == "ok" and TARBALL in web.urls()


def test_pypi_sdist_cap_is_10_mb_unless_lifted():
    web = _pypi_web({"setup.py": b""}, {"demo/__init__.py": b""}, sdist_size=11_000_000)
    rec = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally())
    assert rec["too_large"] == ["sdist"] and SDIST not in web.urls()
    lifted = pypi.scan(_http(web), {"name": "demo", "version": "1.0"}, Tally(), sdist_max_mb=20)
    assert lifted["too_large"] == [] and SDIST in web.urls()
    assert JSON_URL in web.urls()


def test_cli_passes_caps_and_budget(tmp_path, monkeypatch):
    seen = {}

    def fake_run(eco, out, n, http, **kw):
        seen.update(max_bytes=kw["max_bytes"], scan=kw["scan"])
        return {"interrupted": False}

    monkeypatch.setattr("tools.ecoscan.__main__.run", fake_run)
    base = ["run", "npm", "--limit", "5", "--out", str(tmp_path)]
    main(base)
    assert seen["max_bytes"] is None
    assert seen["scan"].keywords == {"max_mb": 150}
    main([*base, "--max-gb", "2.5", "--npm-max-mb", "256"])
    assert seen["max_bytes"] == 2_500_000_000
    assert seen["scan"].keywords == {"max_mb": 256}
    main(["run", "pypi", "--limit", "5", "--out", str(tmp_path), "--sdist-max-mb", "40"])
    assert seen["scan"].keywords == {"sdist_max_mb": 40}


@pytest.mark.parametrize("flag", [["--npm-max-mb", "300"], ["--sdist-max-mb", "0"]])
def test_size_caps_have_a_hard_ceiling(tmp_path, flag):
    with pytest.raises(SystemExit):
        main(["run", "npm", "--limit", "5", "--out", str(tmp_path), *flag])
