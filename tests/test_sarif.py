import io
import json
import re
import tarfile
import unicodedata
from pathlib import Path

import pytest
from jsonschema import Draft4Validator
from typer.testing import CliRunner

from agentprint.cli import app
from agentprint.models import Finding, Severity
from agentprint.sarif import to_sarif
from tests import fakes

runner = CliRunner()
SCHEMA = json.loads((Path(__file__).parent / "data" / "sarif-schema-2.1.0.json").read_text())
# Characters RFC 3986 allows in a URI reference (percent-encoding included).
URI_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def _valid(text: str) -> dict:
    data = json.loads(text)
    errors = sorted(Draft4Validator(SCHEMA).iter_errors(data), key=str)
    assert not errors, [e.message for e in errors[:5]]
    return data


def _uris(node) -> list[str]:
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "uri" and isinstance(v, str)]
        return found + [u for v in node.values() for u in _uris(v)]
    if isinstance(node, list):
        return [u for v in node for u in _uris(v)]
    return []


def _finding(
    check_id="AP001",
    severity=Severity.HIGH,
    location="/x/h.jsonl",
    line=3,
    title="Secret in agent history",
    detail="GitHub token: ghp_…(len 40)",
    fix="Rotate it.",
):
    return Finding(check_id, severity, title, "claude-code", location, line, detail, fix)


def _tgz(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_scan_sarif_is_valid_and_never_contains_a_full_key(fake_home, fake_project):
    result = runner.invoke(
        app, ["scan", str(fake_project), "--home", str(fake_home), "--format", "sarif"]
    )
    assert result.exit_code == 1
    data = _valid(result.output)
    assert data["version"] == "2.1.0"
    run = data["runs"][0]
    assert run["tool"]["driver"]["name"] == "agentprint"
    assert len(run["results"]) == 8
    for key in (*fakes.ALL.values(), fakes.GENERIC_VALUE):
        assert key not in result.output


def test_every_result_points_at_its_rule():
    findings = [
        _finding(),
        _finding("AP002", Severity.CRITICAL, title="Secret in agent settings"),
        _finding("AP001", line=9),
    ]
    run = _valid(to_sarif(findings))["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["AP001", "AP002"]
    for res in run["results"]:
        assert rules[res["ruleIndex"]]["id"] == res["ruleId"]


def test_rule_is_built_from_check_metadata():
    [rule] = json.loads(to_sarif([_finding()]))["runs"][0]["tool"]["driver"]["rules"]
    assert rule["shortDescription"]["text"] == "Secret in agent history"
    assert rule["help"]["text"] == "Rotate it."
    assert rule["defaultConfiguration"]["level"] == "error"
    assert rule["properties"]["security-severity"] == "8.0"
    assert "security" in rule["properties"]["tags"]


@pytest.mark.parametrize(
    ("severity", "level", "score"),
    [
        (Severity.CRITICAL, "error", "9.5"),
        (Severity.HIGH, "error", "8.0"),
        (Severity.MEDIUM, "warning", "5.5"),
        (Severity.LOW, "note", "2.0"),
    ],
)
def test_severity_maps_to_level_and_security_severity(severity, level, score):
    run = json.loads(to_sarif([_finding(severity=severity)]))["runs"][0]
    assert run["results"][0]["level"] == level
    assert run["tool"]["driver"]["rules"][0]["properties"]["security-severity"] == score


def test_rule_takes_its_highest_severity_but_results_keep_their_own():
    findings = [_finding("AP006", Severity.MEDIUM), _finding("AP006", Severity.HIGH)]
    run = json.loads(to_sarif(findings))["runs"][0]
    [rule] = run["tool"]["driver"]["rules"]
    assert rule["properties"]["security-severity"] == "8.0"
    assert [r["level"] for r in run["results"]] == ["warning", "error"]


def test_message_carries_the_redacted_detail_and_count():
    finding = _finding()
    finding.count = 4
    [res] = json.loads(to_sarif([finding]))["runs"][0]["results"]
    assert "ghp_…(len 40)" in res["message"]["text"]
    assert "4" in res["message"]["text"]


def test_file_inside_root_is_relative_to_srcroot_with_line(tmp_path):
    file = tmp_path / ".claude" / "settings.local.json"
    run = _valid(to_sarif([_finding(location=str(file), line=7)], root=tmp_path))["runs"][0]
    physical = run["results"][0]["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"] == {
        "uri": ".claude/settings.local.json",
        "uriBaseId": "%SRCROOT%",
    }
    assert physical["region"] == {"startLine": 7}
    assert run["originalUriBaseIds"]["%SRCROOT%"]["uri"].endswith("/")


def test_file_outside_root_is_an_absolute_file_uri(tmp_path):
    outside = tmp_path / "home" / ".claude" / "history.jsonl"
    data = to_sarif([_finding(location=str(outside))], root=tmp_path / "repo")
    location = json.loads(data)["runs"][0]["results"][0]["locations"][0]
    uri = location["physicalLocation"]["artifactLocation"]["uri"]
    assert uri.startswith("file:///") and uri.endswith("/home/.claude/history.jsonl")


def test_finding_without_line_has_no_region(tmp_path):
    data = to_sarif([_finding(location=str(tmp_path / "f"), line=None)], root=tmp_path)
    physical = json.loads(data)["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert "region" not in physical


def test_member_of_an_archive_is_a_nested_artifact(tmp_path):
    (tmp_path / "dist").mkdir()
    archive = _tgz(tmp_path / "dist" / "demo-1.0.0.tgz", {})
    finding = _finding("AP006", location="demo-1.0.0.tgz!package/.claude/settings.local.json")
    data = to_sarif([finding], root=tmp_path, package=archive, labels=["demo-1.0.0.tgz"])
    run = _valid(data)["runs"][0]
    location = run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    member = run["artifacts"][location["index"]]
    parent = run["artifacts"][member["parentIndex"]]
    assert member["location"]["uri"] == location["uri"] == "/package/.claude/settings.local.json"
    assert parent["location"] == {"uri": "dist/demo-1.0.0.tgz", "uriBaseId": "%SRCROOT%"}
    assert "parentIndex" not in parent
    assert (
        "demo-1.0.0.tgz!package/.claude/settings.local.json" in run["results"][0]["message"]["text"]
    )


def test_members_of_one_archive_share_its_artifact(tmp_path):
    archive = tmp_path / "p.tgz"
    findings = [_finding("AP006", location=f"p.tgz!package/.claude/{n}.jsonl") for n in "ab"]
    run = _valid(to_sarif(findings, root=tmp_path, package=archive, labels=["p.tgz"]))["runs"][0]
    assert len(run["artifacts"]) == 3
    assert {a.get("parentIndex") for a in run["artifacts"]} == {None, 0}


@pytest.mark.parametrize("member", [".claude/settings.local.json", "demo-1.0/.claude/x.jsonl"])
def test_project_pack_points_at_the_source_file(tmp_path, member):
    project = tmp_path / "proj"
    source = project / ".claude" / member.rsplit("/", 1)[1]
    source.parent.mkdir(parents=True)
    source.write_text("{}")
    finding = _finding("AP006", location=f"demo-1.0.tgz!{member}", line=None)
    run = _valid(to_sarif([finding], root=tmp_path, package=project, labels=["demo-1.0.tgz"]))
    artifact_location = run["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"
    ]
    assert artifact_location == {
        "uri": f"proj/.claude/{source.name}",
        "uriBaseId": "%SRCROOT%",
    }


def test_project_pack_never_maps_outside_the_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    finding = _finding("AP006", location="d.tgz!../secret.txt", line=None)
    run = json.loads(to_sarif([finding], root=tmp_path, package=project, labels=["d.tgz"]))
    artifact_location = run["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"
    ]
    assert "index" in artifact_location  # stays a member of the (virtual) archive


def test_hostile_member_names_give_valid_uris_and_clean_messages(tmp_path):
    member = "/package/.claude/we ird#?%\x1b[31m‮\n名.jsonl"
    finding = _finding("AP006", location=f"evil.tgz!{member}")
    data = to_sarif([finding], root=tmp_path, package=tmp_path / "evil.tgz", labels=["evil.tgz"])
    run = _valid(data)["runs"][0]
    for uri in _uris(run):
        assert URI_CHARS.fullmatch(uri), uri
        assert not uri.startswith("//")
    text = run["results"][0]["message"]["text"]
    assert not any(unicodedata.category(ch).startswith("C") for ch in text)


def test_label_containing_bang_is_split_on_the_known_label(tmp_path):
    finding = _finding("AP006", location="we!rd-1.0.tgz!package/.claude/a.jsonl")
    data = to_sarif([finding], root=tmp_path, package=tmp_path / "x", labels=["we!rd-1.0.tgz"])
    run = json.loads(data)["runs"][0]
    member = run["artifacts"][
        run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["index"]
    ]
    assert member["location"]["uri"] == "/package/.claude/a.jsonl"


def test_fingerprints_are_stable_and_never_hash_the_key(tmp_path):
    finding = _finding(location=str(tmp_path / "h.jsonl"))
    first = json.loads(to_sarif([finding], root=tmp_path))["runs"][0]["results"][0]
    again = json.loads(to_sarif([finding], root=tmp_path))["runs"][0]["results"][0]
    assert first["partialFingerprints"] == again["partialFingerprints"]
    other = _finding(location=str(tmp_path / "h.jsonl"), line=4)
    third = json.loads(to_sarif([other], root=tmp_path))["runs"][0]["results"][0]
    assert first["partialFingerprints"] != third["partialFingerprints"]


def test_no_findings_is_a_valid_empty_run():
    run = _valid(to_sarif([]))["runs"][0]
    assert run["results"] == []
    assert run["tool"]["driver"]["rules"] == []


def test_pack_sarif_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    leaky = json.dumps({"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}})
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": leaky.encode()})
    result = runner.invoke(app, ["pack", "demo-1.0.0.tgz", "--format", "sarif"])
    assert result.exit_code == 1
    run = _valid(result.output)["runs"][0]
    assert sorted(r["ruleId"] for r in run["results"]) == ["AP002", "AP006"]
    assert run["artifacts"][0]["location"] == {"uri": "demo-1.0.0.tgz", "uriBaseId": "%SRCROOT%"}
    assert fakes.ANTHROPIC not in result.output


def test_scan_sarif_clean_exits_zero(tmp_path):
    result = runner.invoke(app, ["scan", str(tmp_path), "--no-home", "--format", "sarif"])
    assert result.exit_code == 0
    assert _valid(result.output)["runs"][0]["results"] == []
