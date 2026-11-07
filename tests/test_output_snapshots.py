"""Lock the AI-facing outputs (agent, json, sarif) byte for byte.

The human terminal output may change freely; these files must not.
Normalisation (only run-specific text): the pytest tmp folder -> <TMP>, the version -> <VERSION>,
SARIF partialFingerprints (sha256 of a tmp-path-dependent string) -> <FINGERPRINT>.
Set AGENTPRINT_UPDATE_SNAPSHOTS=1 to rewrite the snapshot files instead of comparing.
"""

import io
import os
import re
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentprint import __version__
from agentprint.cli import app

runner = CliRunner()
SNAPSHOTS = Path(__file__).parent / "data" / "snapshots"


def _normalise(text: str, tmp_path: Path) -> str:
    text = text.replace(str(tmp_path), "<TMP>").replace(__version__, "<VERSION>")
    return re.sub(r'("agentprint/v1": ")[0-9a-f]{64}"', r'\1<FINGERPRINT>"', text)


def _tarball(tmp_path: Path) -> Path:
    data = b'{"x": 1}'
    tgz = tmp_path / "p.tgz"
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/.claude/settings.local.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return tgz


CASES = {
    "scan-agent": ("scan", "agent", "scan.agent.txt"),
    "scan-json": ("scan", "json", "scan.json"),
    "scan-sarif": ("scan", "sarif", "scan.sarif"),
    "pack-agent": ("pack", "agent", "pack.agent.txt"),
    "pack-json": ("pack", "json", "pack.json"),
}


# The scan snapshots hold POSIX tmp paths and no "not checked on Windows" note; Windows output
# legitimately differs (drive paths, the AP008 note), and the pack snapshots below run everywhere.
CASE_PARAMS = [
    pytest.param(name, marks=pytest.mark.posix_only) if name.startswith("scan") else name
    for name in CASES
]


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_snapshot(case, tmp_path, fake_home, fake_project, monkeypatch):
    # SARIF's originalUriBaseIds is built from the process cwd; pin it so it becomes <TMP>/.
    monkeypatch.chdir(tmp_path)
    command, fmt, filename = CASES[case]
    if command == "scan":
        args = ["scan", str(fake_project), "--home", str(fake_home), "--format", fmt]
    else:
        args = ["pack", str(_tarball(tmp_path)), "--format", fmt]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    output = _normalise(result.output, tmp_path)
    path = SNAPSHOTS / filename
    if os.environ.get("AGENTPRINT_UPDATE_SNAPSHOTS") == "1":
        path.write_text(output, encoding="utf-8")
        pytest.skip(f"wrote {filename}")
    assert output == path.read_text(encoding="utf-8")
