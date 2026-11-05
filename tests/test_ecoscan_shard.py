import json
import random
import xmlrpc.client

import pytest

from tests.test_ecoscan_registries import Web, _feed_web, _http
from tests.test_ecoscan_run import Enumerator, _ok_scan, _quiet
from tools.ecoscan import npm, pypi
from tools.ecoscan.__main__ import main
from tools.ecoscan.net import Response, Tally
from tools.ecoscan.run import run
from tools.ecoscan.shard import parse_shard, parse_window, shard_of


def _names(n=3000):
    rng = random.Random(7)
    letters = "abcdefghijklmnopqrstuvwxyz-0123456789"
    return sorted(
        {"".join(rng.choice(letters) for _ in range(rng.randint(3, 20))) for _ in range(n)}
    )


@pytest.mark.parametrize("total", [1, 2, 7, 10])
@pytest.mark.parametrize("eco", ["npm", "pypi"])
def test_shards_are_disjoint_and_cover_everything(total, eco):
    names = _names()
    shards = [{n for n in names if shard_of(n, total, eco) == k} for k in range(1, total + 1)]
    assert set().union(*shards) == set(names)
    assert sum(len(s) for s in shards) == len(names)  # no name in two shards
    if total > 1:
        assert all(len(s) > len(names) / total / 2 for s in shards)  # roughly even


def test_shard_is_the_same_on_every_machine():
    # sha256-based, not Python's per-process hash(): pinned values must never change, or
    # volunteers on different versions would scan overlapping shards.
    got = [shard_of(n, 10, "npm") for n in ("left-pad", "react", "@scope/pkg")]
    got.append(shard_of("requests", 10, "pypi"))
    assert got == PINNED


PINNED = [1, 5, 5, 3]  # from the first release of shard.py; never change


def test_pypi_names_are_normalized_before_sharding():
    assert shard_of("Foo_Bar.baz", 7, "pypi") == shard_of("foo-bar-baz", 7, "pypi")


def test_parse_shard_and_window():
    assert parse_shard("3/10") == (3, 10)
    assert parse_window("100:200") == (100, 200)
    for bad in ("0/10", "11/10", "a/b", "3", "1/0"):
        with pytest.raises(ValueError):
            parse_shard(bad)
    for bad in ("200:100", "1-2", "x:y", "5:5"):
        with pytest.raises(ValueError):
            parse_window(bad)


def test_run_keeps_only_its_own_shard(tmp_path):
    names = [f"p{i}" for i in range(50)]
    run(
        "npm", tmp_path, None, None, **_quiet(recent=Enumerator(names), scan=_ok_scan, shard=(2, 5))
    )
    rows = [
        json.loads(x)
        for x in (tmp_path / "private" / "targets-npm.jsonl").read_text().split("\n")
        if x
    ]
    assert rows and {r["name"] for r in rows} == {n for n in names if shard_of(n, 5, "npm") == 2}
    meta = json.loads((tmp_path / "private" / "meta-npm.json").read_text())
    assert (meta["shard"], meta["window"]) == ([2, 5], None)


def test_resuming_a_folder_with_another_round_is_refused(tmp_path):
    enum = Enumerator([f"p{i}" for i in range(10)])
    run("npm", tmp_path, None, None, **_quiet(recent=enum, scan=_ok_scan, shard=(1, 2)))
    with pytest.raises(ValueError, match="new --out"):
        run("npm", tmp_path, None, None, **_quiet(recent=enum, scan=_ok_scan, shard=(2, 2)))
    with pytest.raises(ValueError, match="new --out"):
        run(
            "npm",
            tmp_path,
            None,
            None,
            **_quiet(recent=enum, scan=_ok_scan, shard=(1, 2), window=(1, 9)),
        )


def test_npm_window_is_an_explicit_sequence_range():
    got = npm.in_range(_http(_feed_web()), 10, 20, Tally())
    assert [t["name"] for t in got] == ["p20", "p19", "p17", "p16", "p14", "p13", "p11"]


def test_pypi_window_is_an_explicit_serial_range():
    events = [
        ("a", "1.0", 0, "new release", 10),
        ("b", "2.0", 0, "new release", 15),
        ("A", "1.1", 0, "new release", 20),
        ("c", "0.1", 0, "new release", 25),
        ("c", "0.1", 0, "remove release", 26),
        ("d", "3.0", 0, "new release", 30),
    ]
    web = Web()

    def rpc(method, url, headers, body):
        params, name = xmlrpc.client.loads(body)
        result = [list(e) for e in events if e[4] > params[0]]
        reply = xmlrpc.client.dumps((result,), methodresponse=True).encode()
        return Response(200, {}, reply, len(reply))

    web.routes["https://pypi.org/pypi"] = rpc
    got = pypi.in_range(_http(web), 12, 26, Tally())
    assert [(t["name"], t["version"]) for t in got] == [("A", "1.1"), ("b", "2.0")]


def test_run_command_passes_window_and_shard(tmp_path, monkeypatch):
    seen = {}

    def fake_run(eco, out, n, http, **kw):
        seen.update(n=n, window=kw["window"], shard=kw["shard"])
        return {"interrupted": False}

    monkeypatch.setattr("tools.ecoscan.__main__.run", fake_run)
    args = ["run", "npm", "--window", "100:200", "--shard", "2/5", "--out", str(tmp_path)]
    assert main(args) == 0
    assert seen == {"n": None, "window": (100, 200), "shard": (2, 5)}
    with pytest.raises(SystemExit):  # a window or a limit is required
        main(["run", "npm", "--out", str(tmp_path)])


def test_window_command_prints_shared_bounds(monkeypatch, capsys):
    monkeypatch.setattr(npm, "head", lambda http, tally: 1_000_000)
    assert main(["window", "npm", "--packages", "50000"]) == 0
    out = capsys.readouterr().out
    # newest hour left out (15,000), then about 3.5 change numbers per package
    assert "--window 810000:985000" in out
