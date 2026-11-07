"""Project config: where it is read from, what it holds, and that it never stops a run."""

import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentprint.config import DEFAULT_BASELINE, Sources, glob_pattern, load, read_regular
from agentprint.report import FailOn

FULL = """
fail_on = "medium"
strict = true
baseline = "ci/accepted.json"

[[ignore]]
check = "ap003"
path = ".devcontainer/**"
reason = "throwaway dev container"

[[ignore]]
path = "docs/examples/*.json"
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_config_means_the_defaults(tmp_path):
    config = load(tmp_path)
    assert config.source is None and config.warnings == ()
    assert config.fail_on is None and config.strict is False and config.ignore == ()
    assert config.baseline == tmp_path / DEFAULT_BASELINE


def test_agentprint_toml_is_read(tmp_path):
    source = _write(tmp_path / ".agentprint.toml", FULL)
    config = load(tmp_path)
    assert config.source == source and config.warnings == ()
    assert config.fail_on is FailOn.medium and config.strict is True
    assert config.baseline == tmp_path / "ci" / "accepted.json"
    first, second = config.ignore
    assert (first.check, first.path, first.reason) == (
        "AP003",
        ".devcontainer/**",
        "throwaway dev container",
    )
    assert first.matches("AP003", ".devcontainer/.claude/settings.json")
    assert not first.matches("AP004", ".devcontainer/.claude/settings.json")
    assert not first.matches("AP003", ".claude/settings.json")
    assert second.check is None and second.reason == "no reason given"
    assert second.matches("AP002", "docs/examples/mcp.json")


def test_pyproject_tool_table_is_read(tmp_path):
    _write(
        tmp_path / "pyproject.toml", '[project]\nname = "x"\n\n[tool.agentprint]\nstrict = true\n'
    )
    config = load(tmp_path)
    assert config.source == tmp_path / "pyproject.toml" and config.strict is True


def test_agentprint_toml_wins_over_pyproject(tmp_path):
    _write(tmp_path / "pyproject.toml", '[tool.agentprint]\nfail_on = "low"\n')
    _write(tmp_path / ".agentprint.toml", 'fail_on = "critical"\n')
    assert load(tmp_path).fail_on is FailOn.critical


def test_a_pyproject_without_our_table_is_not_a_config(tmp_path):
    _write(tmp_path / "pyproject.toml", '[project]\nname = "agentprint-user"\n')
    assert load(tmp_path).source is None
    _write(tmp_path / "pyproject.toml", "this is [not toml\n")  # never names us: not ours to judge
    assert load(tmp_path).warnings == ()


def test_config_option_wins_and_no_config_reads_nothing(tmp_path):
    _write(tmp_path / ".agentprint.toml", 'fail_on = "low"\n')
    other = _write(tmp_path / "ci" / "strict.toml", 'fail_on = "critical"\n')
    assert load(tmp_path, Sources(config=other)).fail_on is FailOn.critical
    off = load(tmp_path, Sources(off=True))
    assert off.fail_on is None and off.source is None and off.baseline is None  # no baseline
    chosen = tmp_path / "b.json"
    assert load(tmp_path, Sources(off=True, baseline=chosen)).baseline == chosen
    assert load(tmp_path, Sources(baseline=chosen)).baseline == chosen  # wins over the config


def test_config_option_may_name_a_pyproject(tmp_path):
    other = _write(tmp_path / "sub" / "pyproject.toml", '[tool.agentprint]\nfail_on = "low"\n')
    assert load(tmp_path, Sources(config=other)).fail_on is FailOn.low
    bare = _write(tmp_path / "bare" / "pyproject.toml", '[project]\nname = "x"\n')
    config = load(tmp_path, Sources(config=bare))
    assert config.fail_on is None and "no [tool.agentprint] table" in config.warnings[0]


def test_unknown_keys_are_one_warning_and_the_rest_still_applies(tmp_path):
    text = 'fail_on = "low"\ncolour = "amber"\n[[ignore]]\ncheck = "AP007"\npth = "x"\n'
    _write(tmp_path / ".agentprint.toml", text)
    config = load(tmp_path)
    assert len(config.warnings) == 1
    assert "unknown keys" in config.warnings[0]
    assert "colour" in config.warnings[0] and "pth (ignore entry 1)" in config.warnings[0]
    assert config.fail_on is FailOn.low and config.ignore[0].check == "AP007"


def test_invalid_toml_warns_and_uses_the_defaults(tmp_path):
    _write(tmp_path / ".agentprint.toml", 'fail_on = "low"\nstrict = \n')
    config = load(tmp_path)
    assert config.warnings[0].startswith(f"could not read config {tmp_path / '.agentprint.toml'}: ")
    assert config.warnings[0].endswith("; using the defaults")
    assert config.fail_on is None and config.ignore == ()
    assert config.baseline == tmp_path / DEFAULT_BASELINE  # the baseline still applies


def test_missing_config_option_file_warns(tmp_path):
    config = load(tmp_path, Sources(config=tmp_path / "nope.toml"))
    assert config.warnings[0].startswith("could not read config")
    assert config.fail_on is None


def test_not_utf8_and_huge_configs_warn(tmp_path):
    _write(tmp_path / "a" / ".agentprint.toml", "")
    (tmp_path / "a" / ".agentprint.toml").write_bytes(b'fail_on = "\xff"\n')
    assert load(tmp_path / "a").warnings[0].startswith("could not read config")
    _write(tmp_path / "b" / ".agentprint.toml", "#" * (1024 * 1024 + 1))
    assert "larger than 1 MiB" in load(tmp_path / "b").warnings[0]


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ('fail_on = "sometimes"', "fail_on"),
        ("fail_on = 3", "fail_on"),
        ('strict = "yes"', "strict"),
        ("baseline = 1", "baseline"),
        ('baseline = "/etc/x.json"', "inside the project"),
        ('baseline = "../other/x.json"', "inside the project"),
        ('baseline = "a/../../x.json"', "inside the project"),
        ('ignore = "AP003"', "must be a list"),
        ('ignore = ["AP003"]', "is not a table"),
        ('[[ignore]]\nreason = "everything"', "neither check nor path"),
        ('[[ignore]]\ncheck = "AP999"', "unknown check"),
        ('[[ignore]]\ncheck = "rm -rf"', "unknown check"),
        ("[[ignore]]\ncheck = 3", "must be text"),
        ('[[ignore]]\npath = "  "', "neither check nor path"),
    ],
)
def test_bad_values_warn_and_change_nothing(tmp_path, text, words):
    _write(tmp_path / ".agentprint.toml", text + "\n")
    config = load(tmp_path)
    assert len(config.warnings) == 1 and words in config.warnings[0], config.warnings
    assert config.fail_on is None and config.strict is False and config.ignore == ()
    assert config.baseline == tmp_path / DEFAULT_BASELINE


def test_warnings_hold_no_control_characters(tmp_path):
    _write(tmp_path / ".agentprint.toml", '"\\u001b[2Jx" = 1\n')
    (warning,) = load(tmp_path).warnings
    assert "\x1b" not in warning


@pytest.mark.parametrize(
    ("glob", "path", "hit"),
    [
        ("*.json", "a.json", True),
        ("*.json", "x/a.json", False),
        ("**/*.json", "a.json", True),
        ("**/*.json", "x/y/a.json", True),
        (".claude/", ".claude/settings.json", True),
        (".claude/", ".claude", False),
        (".claude/**", ".claude/x/y.json", True),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("/.mcp.json", ".mcp.json", True),
        (".mcp.json", "sub/.mcp.json", False),
        ("docs/[x].md", "docs/[x].md", True),  # brackets are literal
        ("docs/[x].md", "docs/x.md", False),
        ("a.b", "aXb", False),  # dots are literal
        ("src\\*.ts", "src/a.ts", True),  # a Windows-style slash means a folder too
    ],
)
def test_ignore_paths_are_globs_from_the_project_root(glob, path, hit):
    assert (glob_pattern(glob).fullmatch(path) is not None) is hit


@contextmanager
def _within_seconds(limit: int):
    """Fail instead of hanging forever if a read ever blocks again."""

    def stop(*_):
        raise AssertionError(f"still reading after {limit} s: a read blocked")

    old = signal.signal(signal.SIGALRM, stop)
    signal.alarm(limit)
    started = time.perf_counter()
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert time.perf_counter() - started < limit


@pytest.mark.posix_only  # FIFOs and /dev/null
def test_a_fifo_or_a_device_is_never_read_as_a_config(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with _within_seconds(5):
        config = load(tmp_path, Sources(config=tmp_path / "pipe"))
        assert "not a regular file" in config.warnings[0]
        assert "not a regular file" in load(tmp_path, Sources(config=Path(os.devnull))).warnings[0]
        (tmp_path / ".agentprint.toml").symlink_to(tmp_path / "pipe")
        (tmp_path / "pyproject.toml").symlink_to(os.devnull)
        assert load(tmp_path).warnings == ()  # neither is a file: nothing to read, no hang


def test_read_regular_is_capped_and_can_refuse_links(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"x" * 11)
    assert read_regular(big, 11) == b"x" * 11
    with pytest.raises(ValueError, match="larger than 10 bytes"):
        read_regular(big, 10)
    with pytest.raises(FileNotFoundError):
        read_regular(tmp_path / "missing", 10)
    if os.name != "nt":
        (tmp_path / "link").symlink_to(big)
        assert read_regular(tmp_path / "link", 11) == b"x" * 11
        with pytest.raises(ValueError, match="symbolic link"):
            read_regular(tmp_path / "link", 11, links=False)
