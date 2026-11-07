import json
import subprocess
from pathlib import Path

import pytest

from tests.fakes import ANTHROPIC
from tools import prepublic_audit

REAL_LOOKING = "sk-ant-" + "api03-" + "aB3dE5gH7jK9mN1pQ3sT5vX7zC2fR4uW6yA8bD0eG" * 2
BRAND = "Fe" + "asty"
COMMUNITY = ("LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "T")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.org")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit(repo: Path, files: dict[str, str], msg: str = "c") -> None:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg)


@pytest.fixture
def repo(tmp_path, env):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    commit(r, {n: "x\n" for n in COMMUNITY} | {"fixture.txt": f"key = {ANTHROPIC}\n"})
    return r


def audit(repo, capsys, *extra):
    code = prepublic_audit.main(["--repo", str(repo), "--no-network", *extra])
    return code, capsys.readouterr()


def test_clean_repo_with_fake_fixtures_passes(repo, capsys):
    code, out = audit(repo, capsys)
    assert code == 0
    assert "PASS" in out.out


def test_real_looking_key_in_old_commit_fails_without_printing_it(repo, capsys):
    commit(repo, {"old.txt": f"k={REAL_LOOKING}\n"}, "add")
    git(repo, "rm", "-q", "old.txt")
    git(repo, "commit", "-m", "remove")
    code, out = audit(repo, capsys)
    assert code == 1
    text = out.out + out.err
    assert "old.txt" in text
    assert REAL_LOOKING not in text
    assert REAL_LOOKING[8:] not in text
    assert "sk-a" in text


def test_key_on_other_branch_is_found(repo, capsys):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"s.txt": REAL_LOOKING})
    git(repo, "checkout", "-q", "-")
    assert audit(repo, capsys)[0] == 1


def test_tracked_private_file_fails(repo, capsys):
    commit(repo, {"docs/private/x.md": "notes\n"})
    code, out = audit(repo, capsys)
    assert code == 1
    assert "docs/private/x.md" in out.out


def test_tracked_env_fails(repo, capsys):
    commit(repo, {".env": "A=1\n"})
    assert audit(repo, capsys)[0] == 1


def test_brand_in_tracked_file_and_message_fail(repo, capsys):
    commit(repo, {"a.txt": f"support@{BRAND.lower()}.co\n"}, f"about {BRAND}")
    code, out = audit(repo, capsys)
    assert code == 1
    assert "a.txt" in out.out
    assert "commit message" in out.out


def test_brand_in_author_email_fails(repo, capsys, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", f"me@{BRAND.lower()}.co")
    commit(repo, {"b.txt": "b\n"})
    assert audit(repo, capsys)[0] == 1


def test_missing_community_file_fails(repo, capsys):
    git(repo, "rm", "-q", "SECURITY.md")
    git(repo, "commit", "-m", "rm")
    code, out = audit(repo, capsys)
    assert code == 1
    assert "SECURITY.md" in out.out


def test_large_and_binary_blobs_are_skipped_and_counted(repo, capsys):
    (repo / "big.txt").write_text("a" * (prepublic_audit.MAX_BLOB_BYTES + 1))
    (repo / "b.bin").write_bytes(b"\x00\x01" + REAL_LOOKING.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "blobs")
    code, out = audit(repo, capsys)
    assert code == 0
    assert "2 skipped" in out.out


def test_json_format(repo, capsys):
    code, out = audit(repo, capsys, "--format", "json")
    data = json.loads(out.out)
    assert code == 0 and data["result"] == "PASS" and data["problems"] == []


def test_not_a_repo_is_usage_error(tmp_path, capsys, env):
    assert prepublic_audit.main(["--repo", str(tmp_path), "--no-network"]) == 2


def test_bad_flag_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        prepublic_audit.main(["--nope"])
    assert e.value.code == 2


def test_gh_unavailable_is_a_note(repo, capsys, monkeypatch):
    monkeypatch.setattr(
        prepublic_audit.subprocess,
        "run",
        _no_gh(prepublic_audit.subprocess.run),
    )
    r = prepublic_audit.run_audit(repo, network=True)
    assert r.ok
    assert any("gh" in n for n in r.notes)


def _no_gh(real):
    def run(cmd, *a, **k):
        if cmd[0] == "gh":
            raise FileNotFoundError
        return real(cmd, *a, **k)

    return run
