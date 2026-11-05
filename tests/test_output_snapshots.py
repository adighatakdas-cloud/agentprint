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


@pytest.mark.parametrize("case", CASES)
def test_snapshot(case, tmp_path, fake_home, fake_project):
    command, fmt, filename = CASES[case]
    if command == "scan":
        args = ["scan", str(fake_project), "--home", str(fake_home), "--format", fmt]
    else:
        args = ["pack", str(_tarball(tmp_path)), "--format", fmt]
    result = runner.invoke(app, args)
    output = _normalise(result.output, tmp_path)
    path = SNAPSHOTS / filename
    if os.environ.get("AGENTPRINT_UPDATE_SNAPSHOTS") == "1":
        path.write_text(output)
        pytest.skip(f"wrote {filename}")
    assert output == path.read_text()
