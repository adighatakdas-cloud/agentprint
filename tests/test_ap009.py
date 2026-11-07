"""AP009: what in a package lets an AI rebuild your source, judged by the package's licence.

Every package is synthetic and built at test time: listings built directly, or archives
written to tmp_path. No real package, and no key, appears here.
"""

import base64
import io
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import pack
from agentprint.advice import Moment
from agentprint.checks import PACKAGE_WANT, check_package
from agentprint.checks import ap009_source_exposure as ap009
from agentprint.cli import app
from agentprint.gitcheck import check_push
from agentprint.guard import check_package_path, evaluate
from agentprint.models import Severity
from agentprint.pack import PackageListing, read_archive
from agentprint.report import FailOn, exit_code, to_agent
from tests import fakes

runner = CliRunner()
LABEL = "demo-1.0.0.tgz"
# Distinctive text that must never reach any finding field.
SOURCE = "const ultraSecretSauce = 42; // our original comment, never shipped as text"
TOUR = "This codebase keeps the pricing engine in src/zebra-quokka/"


def _package_json(licence: object = None, **extra) -> bytes:
    data = {"name": "demo", "version": "1.0.0", "description": "zebra-quokka demo", **extra}
    if licence is not None:
        data["license"] = licence
    return json.dumps(data).encode()


def _map(sources_content: object = None, **extra) -> bytes:
    data = {"version": 3, "sources": ["../src/index.ts"], "names": [], "mappings": "AAAA"}
    if sources_content is not None:
        data["sourcesContent"] = sources_content
    return json.dumps({**data, **extra}).encode()


def _inline(map_bytes: bytes, code: bytes = b"console.log(1);\n") -> bytes:
    b64 = base64.b64encode(map_bytes)
    return code + b"//# sourceMappingURL=data:application/json;charset=utf-8;base64," + b64 + b"\n"


def _listing(members: dict[str, bytes | None], label: str = LABEL) -> PackageListing:
    """A listing as pack builds it: None = listed, but its content was not read."""
    contents = {name: data for name, data in members.items() if data is not None}
    skipped = [name for name, data in members.items() if data is None]
    return PackageListing(label, list(members), contents, skipped)


def _npm(licence: object = None, **members: bytes | None) -> PackageListing:
    files: dict[str, bytes | None] = {"package/package.json": _package_json(licence)}
    files |= {f"package/{name}": data for name, data in members.items()}
    return _listing(files)


def _tgz(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _severities(findings) -> list[str]:
    return [f.severity.name for f in findings]


def _ap009(listing: PackageListing):
    findings = ap009.check_listing(listing)
    assert all(f.check_id == "AP009" and f.title == ap009.TITLE for f in findings)
    return findings


# ---- the interface ------------------------------------------------------------------------


def test_id_title_fix():
    assert ap009.ID == "AP009"
    assert ap009.TITLE == "Package lets an AI rebuild your source"
    assert ap009.FIX and "\n" not in ap009.FIX


@pytest.mark.parametrize(
    ("name", "wanted"),
    [
        ("package/dist/index.js.map", True),
        ("package/dist/index.js", True),
        ("package/dist/index.mjs", True),
        ("package/dist/index.cjs", True),
        ("package/package.json", True),
        ("package.json", True),  # an npm project folder, listed by npm pack --dry-run
        ("demo-1.0.dist-info/METADATA", True),
        ("demo-1.0/PKG-INFO", True),
        ("package/src/index.ts", False),  # judged by name alone
        ("package/CLAUDE.md", False),
        ("package/lib/deep/package.json", False),  # not the package's own licence
        ("package/node_modules/dep/index.js", False),  # bundled dependency: not your source
        ("package/README.md", False),
    ],
)
def test_wants_only_what_it_reads(name, wanted):
    assert ap009.wants(name) is wanted


# ---- source maps -------------------------------------------------------------------------


@pytest.mark.parametrize("licence", ["UNLICENSED", "SEE LICENSE IN zebra-terms.txt", None])
def test_closed_package_map_with_sources_content_is_high(licence):
    listing = _npm(
        licence, **{"dist/index.js": b"console.log(1);", "dist/index.js.map": _map([SOURCE])}
    )
    [finding] = _ap009(listing)
    assert finding.severity is Severity.HIGH
    assert finding.location == f"{LABEL}!package/dist/index.js.map"
    assert finding.detail.startswith("1 source map holds your original source (")
    assert "closed source" in finding.detail or "closed-source" in finding.detail
    assert finding.detail.endswith("e.g. package/dist/index.js.map")
    assert exit_code([finding], FailOn.high) == 1


def test_open_package_map_with_sources_content_is_low_and_not_block(tmp_path):
    members = {
        "package/package.json": _package_json("MIT"),
        "package/dist/index.js": b"console.log(1);",
        "package/dist/index.js.map": _map([SOURCE]),
        "package/dist/util.js.map": _map([SOURCE]),
        "package/dist/layout.js.map": _map(),  # no sourcesContent: not mentioned for open source
        "package/src/index.ts": b"export const x = 1;",
        "package/CLAUDE.md": TOUR.encode(),
    }
    # A real archive, read the way pack reads it.
    listing = read_archive(_tgz(tmp_path / "open-1.0.0.tgz", members), PACKAGE_WANT)
    findings = _ap009(listing)
    assert _severities(findings) == ["LOW"]  # at most one low print
    assert findings[0].detail.startswith("2 source maps hold your original source (open-source")
    assert exit_code(findings, FailOn.high) == 0
    assert to_agent(check_package(listing), FailOn.high, Moment.publish).startswith(
        "agentprint: OK"
    )
    verdict = evaluate("npm publish open-1.0.0.tgz", tmp_path)
    assert verdict is not None and not verdict.block


@pytest.mark.parametrize(("licence", "expected"), [("UNLICENSED", ["MEDIUM"]), ("MIT", [])])
def test_map_without_sources_content_closed_medium_open_none(licence, expected):
    listing = _npm(licence, **{"dist/index.js": b"1", "dist/index.js.map": _map()})
    findings = _ap009(listing)
    assert _severities(findings) == expected
    if findings:
        assert findings[0].detail.startswith(
            "1 source map gives away your file layout and original names ("
        )
        assert "original source" not in findings[0].detail


@pytest.mark.parametrize("ext", ["js", "mjs", "cjs"])
def test_inline_base64_map_detected(ext):
    full = _npm("UNLICENSED", **{f"dist/index.{ext}": _inline(_map([SOURCE]))})
    [finding] = _ap009(full)
    assert finding.severity is Severity.HIGH
    assert finding.location == f"{LABEL}!package/dist/index.{ext}"

    layout = _npm("UNLICENSED", **{f"dist/index.{ext}": _inline(_map())})
    assert _severities(_ap009(layout)) == ["MEDIUM"]

    open_full = _npm("Apache-2.0", **{f"dist/index.{ext}": _inline(_map([SOURCE]))})
    assert _severities(_ap009(open_full)) == ["LOW"]


def test_inline_map_marker_inside_code_is_not_a_map():
    # A bundler or source-map library mentions the marker in its own code: not an inline map.
    code = (
        b'const PREFIX = "//# sourceMappingURL=data:application/json;base64,";\n'
        b"module.exports = PREFIX;\n"
    )
    bad_b64 = b"x();\n//# sourceMappingURL=data:application/json;base64,!!!notbase64\n"
    not_json = b"x();\n//# sourceMappingURL=data:application/json;base64," + base64.b64encode(
        b"not json"
    )
    listing = _npm("UNLICENSED", **{"a.js": code, "b.js": bad_b64, "c.js": not_json})
    assert _ap009(listing) == []


def test_null_or_invalid_sources_content_is_not_full_source():
    listing = _npm(
        "UNLICENSED",
        **{
            "dist/a.js": b"1",
            "dist/a.js.map": _map([None, None]),
            "dist/b.js.map": _map(["", "   "]),
            "dist/c.js.map": _map("not a list"),
            "dist/d.js.map": _map([1, {"x": 2}]),
            "dist/e.js.map": b"{ this is not json",
            "dist/f.js.map": b"\xff\xfe\x00garbage",
            "dist/g.js.map": json.dumps(["a", "list"]).encode(),  # JSON, but not a source map
        },
    )
    findings = _ap009(listing)
    assert Severity.HIGH not in [f.severity for f in findings]
    assert not any("original source" in f.detail for f in findings)
    # The four real maps without usable content give away the layout; the rest are not maps.
    [layout] = findings
    assert layout.severity is Severity.MEDIUM
    assert layout.detail.startswith("4 source maps give away your file layout")


def test_index_map_sections_with_sources_content_are_full_source():
    index = json.dumps(
        {
            "version": 3,
            "sections": [{"offset": {"line": 0, "column": 0}, "map": json.loads(_map([SOURCE]))}],
        }
    ).encode()
    listing = _npm("UNLICENSED", **{"dist/a.js": b"1", "dist/a.js.map": index})
    assert _severities(_ap009(listing)) == ["HIGH"]


def test_map_with_xssi_prefix_is_read():
    listing = _npm("UNLICENSED", **{"a.js": b"1", "a.js.map": b")]}'\n" + _map([SOURCE])})
    assert _severities(_ap009(listing)) == ["HIGH"]


# ---- original source and instruction files ---------------------------------------------------


def test_original_ts_next_to_js_closed_medium_dts_only_none():
    shipped = {
        "dist/index.js": b"1",
        "src/index.ts": b"export const x: number = 1;",
        "src/App.tsx": b"export default () => null;",
        "src/a.mts": b"",
        "src/b.cts": b"",
        "src/Card.vue": b"<template/>",
        "src/Button.svelte": b"<script/>",
        "dist/index.d.ts": b"export declare const x: number;",
        "dist/index.d.mts": b"",
        "dist/index.d.cts": b"",
        "dist/styles.d.css.ts": b"",
    }
    [finding] = _ap009(_npm("UNLICENSED", **shipped))
    assert finding.severity is Severity.MEDIUM
    assert finding.detail.startswith("6 original source files ship next to the compiled code (")
    assert finding.location == f"{LABEL}!package/src/index.ts"

    assert _ap009(_npm("MIT", **shipped)) == []  # open source: shipping source is normal

    types_only = {"index.d.ts": b"", "index.d.mts": b"", "sub/index.d.cts": b""}
    assert _ap009(_npm("UNLICENSED", **types_only)) == []

    # Source with no compiled output is the package itself (e.g. TypeScript for bundlers).
    source_only = {"src/index.ts": b"export const x = 1;"}
    assert _ap009(_npm("UNLICENSED", **source_only)) == []


def test_instruction_files_closed_medium_open_none():
    files = {
        "CLAUDE.md": TOUR.encode(),
        "AGENTS.md": TOUR.encode(),
        "docs/GEMINI.md": TOUR.encode(),
        ".cursorrules": TOUR.encode(),
        ".windsurfrules": TOUR.encode(),
        ".github/copilot-instructions.md": TOUR.encode(),
        ".cursor/rules/style.mdc": TOUR.encode(),
        "README.md": b"# demo",
        "docs/agents.md": b"about agents",  # a doc page, not an instruction file
        "CLAUDE.local.md": b"personal",  # AP006's, not an instruction file
    }
    [finding] = _ap009(_npm("UNLICENSED", **files))
    assert finding.severity is Severity.MEDIUM
    assert finding.detail.startswith("7 AI instruction files describe your codebase to an AI (")
    assert finding.location == f"{LABEL}!package/CLAUDE.md"
    assert finding.agent == "ai-instructions"

    assert _ap009(_npm("ISC", **files)) == []


def test_bundled_dependencies_are_not_your_source():
    files = {
        "node_modules/dep/index.js": _inline(_map([SOURCE])),
        "node_modules/dep/index.js.map": _map([SOURCE]),
        "node_modules/dep/src/index.ts": b"",
        "node_modules/dep/CLAUDE.md": b"",
        "index.js": b"1",
    }
    assert _ap009(_npm("UNLICENSED", **files)) == []


# ---- the licence ---------------------------------------------------------------------------


def test_missing_licence_counts_as_closed_and_says_so():
    listing = _npm(None, **{"dist/index.js": b"1", "dist/index.js.map": _map([SOURCE])})
    closed, reason = ap009.licence_of(listing)
    assert closed is True
    assert reason == "no licence field, so treated as closed source; add one if it's open source"
    [finding] = _ap009(listing)
    assert finding.severity is Severity.HIGH
    assert "no licence field, so treated as closed source; add one if it's open source" in (
        finding.detail
    )

    for blank in ("", "   "):
        assert ap009.licence_of(_npm(blank))[0] is True

    unreadable = _listing({"package/package.json": None, "package/a.js.map": _map([SOURCE])})
    closed, reason = ap009.licence_of(unreadable)
    assert closed is True
    assert "could not be read" in reason and "treated as closed source" in reason

    broken = _listing({"package/package.json": b"{ nope", "package/a.js.map": _map()})
    assert ap009.licence_of(broken)[0] is True
    assert "could not be read" in ap009.licence_of(broken)[1]

    nothing = _listing({"package/a.js.map": _map()})
    closed, reason = ap009.licence_of(nothing)
    assert closed is True and "treated as closed source" in reason


@pytest.mark.parametrize(
    ("licence", "closed"),
    [
        ("MIT", False),
        ("(MIT OR Apache-2.0)", False),
        ("Unlicense", False),  # the public-domain licence, not UNLICENSED
        ("UNLICENSED", True),
        ("unlicensed", True),
        ("SEE LICENSE IN LICENSE.md", True),
        ({"type": "MIT", "url": "https://example.invalid"}, False),  # old object form
        ({"url": "https://example.invalid"}, True),
        (["MIT"], True),  # not a licence field npm understands
    ],
)
def test_npm_licence_classification(licence, closed):
    assert ap009.licence_of(_npm(licence))[0] is closed


def test_npm_old_licenses_array_is_a_licence():
    listing = _listing(
        {"package/package.json": json.dumps({"licenses": [{"type": "BSD-3-Clause"}]}).encode()}
    )
    assert ap009.licence_of(listing)[0] is False


def test_project_folder_package_json_is_the_licence():
    listing = _listing({"package.json": _package_json("MIT"), "dist/a.js.map": _map([SOURCE])})
    assert ap009.licence_of(listing) == (False, "open-source package")


def _metadata(*headers: str) -> bytes:
    lines = ["Metadata-Version: 2.4", "Name: demo", "Version: 1.0", *headers, "", "Body text."]
    return "\n".join(lines).encode()


@pytest.mark.parametrize(
    ("headers", "closed"),
    [
        (["License-Expression: MIT"], False),
        (["License: BSD"], False),
        (["Classifier: License :: OSI Approved :: MIT License"], False),
        ([], True),
        (["License: UNKNOWN"], True),  # what old setuptools writes when no licence was given
        (["License: "], True),
        (["Classifier: License :: Other/Proprietary License"], True),
        (["License: Proprietary", "Classifier: License :: Other/Proprietary License"], True),
        (["Classifier: Programming Language :: Python :: 3"], True),
    ],
)
def test_pypi_metadata_licence_classification(headers, closed):
    wheel = _listing(
        {"demo/__init__.py": b"", "demo-1.0.dist-info/METADATA": _metadata(*headers)},
        label="demo-1.0-py3-none-any.whl",
    )
    assert ap009.licence_of(wheel)[0] is closed
    sdist = _listing(
        {"demo-1.0/PKG-INFO": _metadata(*headers), "demo-1.0/pyproject.toml": b""},
        label="demo-1.0.tar.gz",
    )
    assert ap009.licence_of(sdist)[0] is closed


def test_pypi_licence_reasons_and_a_wheel_finding(tmp_path):
    none = _listing({"demo-1.0.dist-info/METADATA": _metadata()}, label="demo.whl")
    assert ap009.licence_of(none) == (
        True,
        "no licence metadata, so treated as closed source; add one if it's open source",
    )
    unreadable = _listing({"demo-1.0.dist-info/METADATA": None}, label="demo.whl")
    assert "could not be read" in ap009.licence_of(unreadable)[1]
    proprietary = _listing(
        {
            "demo-1.0.dist-info/METADATA": _metadata(
                "Classifier: License :: Other/Proprietary License"
            )
        },
        label="demo.whl",
    )
    assert "Other/Proprietary" in ap009.licence_of(proprietary)[1]

    # A wheel whose frontend ships a source map with the original source (a closed package).
    whl = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("demo-1.0.dist-info/METADATA", _metadata())
        zf.writestr("demo/static/app.js", "console.log(1);")
        zf.writestr("demo/static/app.js.map", _map([SOURCE]))
    [finding] = _ap009(read_archive(whl, PACKAGE_WANT))
    assert finding.severity is Severity.HIGH
    assert finding.location == f"{whl.name}!demo/static/app.js.map"


def test_package_json_in_a_python_package_is_not_its_licence():
    sdist = _listing(
        {
            "demo-1.0/PKG-INFO": _metadata(),  # no licence: closed
            "demo-1.0/package.json": _package_json("MIT"),  # a bundled frontend's
        },
        label="demo-1.0.tar.gz",
    )
    assert ap009.licence_of(sdist)[0] is True


# ---- one finding per kind, limits, no contents ---------------------------------------------


def test_one_finding_per_kind_with_count_and_example():
    files: dict[str, bytes | None] = {}
    files |= {f"dist/{i}.js": b"1" for i in range(12)}
    files |= {f"dist/{i}.js.map": _map([SOURCE]) for i in range(12)}
    files |= {f"dist/css/{i}.css.map": _map() for i in range(3)}
    files |= {"src/a.ts": b"", "src/b.ts": b""}
    files |= {"CLAUDE.md": TOUR.encode(), "AGENTS.md": TOUR.encode()}
    findings = _ap009(_npm("UNLICENSED", **files))
    assert _severities(findings) == ["HIGH", "MEDIUM", "MEDIUM", "MEDIUM"]
    full, layout, original, instructions = findings
    assert full.detail.startswith("12 source maps hold your original source (")
    assert full.location == f"{LABEL}!package/dist/0.js.map"
    assert full.detail.endswith("e.g. package/dist/0.js.map")
    assert layout.detail.startswith("3 source maps give away your file layout")
    assert layout.location == f"{LABEL}!package/dist/css/0.css.map"
    assert original.detail.startswith("2 original source files ship")
    assert instructions.detail.startswith("2 AI instruction files describe")
    assert all(f.count == 1 and f.line is None for f in findings)
    assert [f.agent for f in findings] == ["build", "build", "build", "ai-instructions"]
    assert len({f.fix for f in findings}) == 3  # each kind says how to fix it; maps share one


def test_oversized_member_counted_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr("agentprint.pack.MAX_CONTENT_BYTES", 4000)
    big_bundle = _inline(_map([SOURCE * 100]))  # an inline map at the end, over the limit
    assert len(big_bundle) > 4000
    tgz = _tgz(
        tmp_path / "big-1.0.0.tgz",
        {
            "package/package.json": _package_json("UNLICENSED"),
            "package/dist/huge.js": big_bundle,
            "package/dist/huge.js.map": _map([SOURCE * 100]),
            "package/dist/small.js.map": _map([SOURCE]),
        },
    )
    listing = read_archive(tgz, PACKAGE_WANT)
    assert set(listing.skipped) == {"package/dist/huge.js", "package/dist/huge.js.map"}
    full, layout = _ap009(listing)
    # Only what was read is judged by content; nothing unread is called full source.
    assert full.severity is Severity.HIGH
    assert full.detail.startswith("1 source map holds your original source (")
    assert "2 files not read" in full.detail
    # An unread .map still ships, so it counts by its name, and says it was not checked.
    assert layout.severity is Severity.MEDIUM
    assert layout.location == "big-1.0.0.tgz!package/dist/huge.js.map"

    # The huge bundle alone: nothing can be claimed about it, and nothing crashes.
    alone = _tgz(
        tmp_path / "alone-1.0.0.tgz",
        {"package/package.json": _package_json("UNLICENSED"), "package/dist/huge.js": big_bundle},
    )
    assert _ap009(read_archive(alone, PACKAGE_WANT)) == []


def test_content_budget_keeps_agent_files_first(tmp_path, monkeypatch):
    # AP009's extra reads must never stop AP006 from reading an agent file for keys.
    leaky = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}).encode()
    monkeypatch.setattr("agentprint.pack.MAX_TOTAL_CONTENT_BYTES", len(leaky) + 100)
    tgz = _tgz(
        tmp_path / "budget-1.0.0.tgz",
        {
            "package/package.json": _package_json("UNLICENSED"),  # read first, fits
            "package/dist/a.js": b"x" * 90,  # read, then gives its room back
            "package/.claude/settings.local.json": leaky,
        },
    )
    listing = read_archive(tgz, PACKAGE_WANT)
    assert listing.contents["package/.claude/settings.local.json"] == leaky
    assert "package/dist/a.js" in listing.skipped
    assert sum(len(v) for v in listing.contents.values()) <= len(leaky) + 100
    ids = [f.check_id for f in check_package(listing)]
    assert ids[:2] == ["AP006", "AP002"]
    assert fakes.ANTHROPIC not in repr(check_package(listing))


def test_project_folder_extra_reads_stay_within_the_limits(tmp_path, monkeypatch):
    # npm pack --dry-run is faked: no npm needed. Extra (AP009) reads keep to both limits.
    root = tmp_path / "proj"
    (root / "dist").mkdir(parents=True)
    (root / "package.json").write_bytes(_package_json("UNLICENSED"))
    (root / "dist" / "a.js.map").write_bytes(_map([SOURCE]))
    (root / "dist" / "big.js").write_bytes(b"x" * 5000)
    files = ["package.json", "dist/a.js.map", "dist/big.js", "dist/missing.js"]
    output = json.dumps([{"filename": "proj-1.0.0.tgz", "files": [{"path": p} for p in files]}])
    monkeypatch.setattr("agentprint.pack.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "agentprint.pack._run",
        lambda cmd, cwd=None: subprocess.CompletedProcess(cmd, 0, output, ""),
    )
    monkeypatch.setattr("agentprint.pack.MAX_CONTENT_BYTES", 4000)
    listing = pack.read_npm_project(root, PACKAGE_WANT)
    assert set(listing.contents) == {"package.json", "dist/a.js.map"}
    assert set(listing.skipped) == {"dist/big.js", "dist/missing.js"}
    [finding] = _ap009(listing)
    assert finding.severity is Severity.HIGH
    assert "2 files not read" in finding.detail


def test_no_contents_in_findings():
    listing = _npm(
        "SEE LICENSE IN zebra-terms.txt",
        **{
            "dist/index.js": _inline(_map([SOURCE])),
            "dist/index.js.map": _map([SOURCE]),
            "dist/other.js.map": _map(),
            "src/index.ts": SOURCE.encode(),
            "CLAUDE.md": TOUR.encode(),
        },
    )
    findings = _ap009(listing)
    assert len(findings) == 4
    for finding in findings:
        text = json.dumps(finding.to_dict()) + finding.fix
        for secret in ("ultraSecretSauce", "original comment", "zebra", "quokka", "AAAA"):
            assert secret not in text
        assert "src/index.ts" not in text or finding.location.endswith("src/index.ts")


# ---- where it runs -------------------------------------------------------------------------


def _closed_tgz(path: Path) -> Path:
    return _tgz(
        path,
        {
            "package/package.json": _package_json(),
            "package/dist/index.js": b"console.log(1);",
            "package/dist/index.js.map": _map([SOURCE]),
        },
    )


def test_pack_cli_reports_ap009(tmp_path):
    tgz = _closed_tgz(tmp_path / "closed-1.0.0.tgz")
    result = runner.invoke(app, ["pack", str(tgz), "--format", "json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    [finding] = [f for f in data["findings"] if f["check_id"] == "AP009"]
    assert finding["severity"] == "high"
    assert finding["location"] == "closed-1.0.0.tgz!package/dist/index.js.map"
    assert SOURCE not in result.output

    sarif = runner.invoke(app, ["pack", str(tgz), "--format", "sarif"])
    rules = json.loads(sarif.output)["runs"][0]["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["AP009"]

    agent = runner.invoke(app, ["pack", str(tgz), "--format", "agent"])
    assert agent.exit_code == 1
    assert agent.output.startswith("agentprint: BLOCK")
    assert "AP009 Package lets an AI rebuild your source" in agent.output

    opened = _tgz(
        tmp_path / "open-1.0.0.tgz",
        {"package/package.json": _package_json("MIT"), "package/a.js.map": _map([SOURCE])},
    )
    assert runner.invoke(app, ["pack", str(opened), "--format", "json"]).exit_code == 0


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_pack_cli_reports_ap009_for_an_npm_project(tmp_path):
    root = tmp_path / "closed-app"
    (root / "dist").mkdir(parents=True)
    (root / "package.json").write_bytes(_package_json())  # no licence: closed source
    (root / "dist" / "index.js").write_text("console.log(1);\n")
    (root / "dist" / "index.js.map").write_bytes(_map([SOURCE]))
    result = runner.invoke(app, ["pack", str(root), "--format", "json", "--fail-on", "high"])
    assert result.exit_code == 1
    [finding] = [f for f in json.loads(result.output)["findings"] if f["check_id"] == "AP009"]
    assert finding["severity"] == "high"
    assert finding["location"].endswith("!dist/index.js.map")


def test_publish_guard_runs_ap009(tmp_path):
    _closed_tgz(tmp_path / "closed-1.0.0.tgz")
    verdict = evaluate("npm publish closed-1.0.0.tgz", tmp_path)
    assert verdict is not None and verdict.block
    assert "AP009" in verdict.reason

    dist = tmp_path / "py" / "dist"
    dist.mkdir(parents=True)
    with zipfile.ZipFile(dist / "demo-1.0-py3-none-any.whl", "w") as zf:
        zf.writestr("demo-1.0.dist-info/METADATA", _metadata())
        zf.writestr("demo/static/app.js.map", _map([SOURCE]))
    verdict = evaluate("twine upload dist/*", tmp_path / "py")
    assert verdict is not None and verdict.block and "AP009" in verdict.reason

    # The MCP server's check_package goes through the same path.
    assert [f.check_id for f in check_package_path(tmp_path / "closed-1.0.0.tgz")] == ["AP009"]


def test_fix_pack_sees_ap009(tmp_path):
    tgz = _closed_tgz(tmp_path / "closed-1.0.0.tgz")
    result = runner.invoke(
        app, ["fix", str(tgz), "--pack", "--dry-run", "--format", "json", "--home", str(tmp_path)]
    )
    assert result.exit_code == 1, result.output
    assert "AP009" in result.output
    only = runner.invoke(
        app,
        ["fix", str(tgz), "--pack", "--only", "AP009", "--dry-run", "--format", "json"]
        + ["--home", str(tmp_path)],
    )
    assert "unknown check" not in only.output


def _source_project(root: Path) -> Path:
    (root / "dist").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "package.json").write_bytes(_package_json())  # no licence: closed source
    (root / "dist" / "index.js").write_text("console.log(1);\n")
    (root / "dist" / "index.js.map").write_bytes(_map([SOURCE]))
    (root / "src" / "index.ts").write_text("export const x = 1;\n")
    (root / "CLAUDE.md").write_text(TOUR)
    return root


def test_scan_and_push_do_not_run_ap009(tmp_path, monkeypatch):
    project = _source_project(tmp_path / "proj")
    result = runner.invoke(app, ["scan", str(project), "--no-home", "--format", "json"])
    assert '"AP009"' not in result.output

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    assert [f for f in check_push(project) if f.check_id == "AP009"] == []
    verdict = evaluate("git push", project)
    assert verdict is None or "AP009" not in (verdict.reason + (verdict.warning or ""))
