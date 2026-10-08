import os
import sys

import pytest

from agentprint.files import iter_lines, walk_files


def test_reads_lines_with_numbers(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("one\ntwo\n")
    assert list(iter_lines(f)) == [(1, "one\n"), (2, "two\n")]


def test_binary_file_is_skipped(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\x00\x00 api_key = abc")
    assert list(iter_lines(f)) == []


def test_invalid_utf8_is_replaced_not_fatal(tmp_path):
    f = tmp_path / "latin1.txt"
    f.write_bytes(b"caf\xe9 ok\n")
    assert list(iter_lines(f)) == [(1, "caf� ok\n")]


def test_missing_file_yields_nothing(tmp_path):
    assert list(iter_lines(tmp_path / "nope.json")) == []


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX non-root")
def test_unreadable_file_is_skipped(tmp_path):
    f = tmp_path / "locked.json"
    f.write_text("secret\n")
    f.chmod(0)
    try:
        assert list(iter_lines(f)) == []
    finally:
        f.chmod(0o600)


def test_walk_lists_files_recursively_and_accepts_a_file(tmp_path):
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "x.txt").write_text("x")
    (tmp_path / "d" / "y.txt").write_text("y")
    assert sorted(p.name for p in walk_files(tmp_path / "d")) == ["x.txt", "y.txt"]
    assert list(walk_files(tmp_path / "d" / "y.txt")) == [tmp_path / "d" / "y.txt"]
    assert list(walk_files(tmp_path / "missing")) == []


def test_symlink_loop_does_not_hang(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "real.txt").write_text("r")
    (tmp_path / "d" / "loop").symlink_to(tmp_path / "d")
    assert [p.name for p in walk_files(tmp_path / "d")] == ["real.txt"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX non-root")
def test_unenterable_directory_is_skipped(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "settings.json").write_text("x")
    locked.chmod(0o600)  # readable listing, but cannot be entered
    try:
        assert list(walk_files(locked)) == []
        assert list(walk_files(locked / "settings.json")) == []
        assert list(iter_lines(locked / "settings.json")) == []
    finally:
        locked.chmod(0o700)


def test_symlinked_root_file_is_followed(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("x")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert list(walk_files(link)) == [link]
