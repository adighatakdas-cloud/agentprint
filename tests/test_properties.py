"""Property-based tests (hypothesis) on the riskiest code: in-place masking, redaction, secret
detection, the publish guard and the archive readers. Keys come from tests.fakes only."""

import contextlib
import io
import json
import tarfile
import time
import zipfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentprint import fix, pack
from agentprint.guard import evaluate
from agentprint.scanner import scan
from agentprint.secrets import find_secrets, redact
from tests import fakes

# Detector functions that must hold the span properties. A later detector (for example a deeper
# one for obfuscated keys) joins this list and is covered by the same tests.
DETECTORS = [find_secrets]

KEYS = sorted(fakes.ALL.values())
INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff\u202e\u00ad\x00\x1b\r\t"
TEXT = st.text(
    alphabet=st.one_of(st.characters(blacklist_categories=("Cs",)), st.sampled_from(INVISIBLE)),
    max_size=200,
)
ALMOST_KEYS = st.sampled_from(KEYS).flatmap(
    lambda k: st.integers(0, len(k)).map(lambda n: k[:n])  # truncated keys
)
FILL = st.text(alphabet="abcXYZ019_-= \u00e9\u6f22\U0001f600", max_size=40)


# ---- redact ----------------------------------------------------------------------------------


@given(st.text(max_size=300))
def test_redact_never_shows_more_than_four_characters(value):
    out = redact(value)
    assert out == f"{value[:4]}\u2026(len {len(value)})"
    shown = out[: out.rindex("\u2026")]
    assert len(shown) <= 4 and value.startswith(shown)


@given(st.sampled_from(KEYS))
def test_redact_of_a_key_leaks_no_tail(key):
    assert key[4:] not in redact(key) and key[5:] not in redact(key)


# ---- find_secrets ----------------------------------------------------------------------------


@settings(max_examples=150, deadline=2000)
@given(
    st.lists(
        st.one_of(
            TEXT,
            st.sampled_from(KEYS),
            ALMOST_KEYS,
            FILL,
            st.sampled_from(["-", "_", "a", "9"]).map(lambda c: c * 500),
        ),
        max_size=8,
    )
)
def test_detectors_return_sorted_disjoint_in_bounds_spans(parts):
    text = "".join(parts)
    for detect in DETECTORS:
        found = detect(text)
        end_before = 0
        for m in found:
            assert 0 <= m.start < m.end <= len(text)
            assert text[m.start : m.end] == m.value
            assert m.start >= end_before  # sorted and not overlapping
            end_before = m.end


PATHOLOGICAL = [c * 500 for c in "-_a9"] + [
    "a-9_" * 125,
    ("-" * 499 + "a") * 2000,
    "sk-ant-api03-" + "a" * 100000,
]


@pytest.mark.parametrize("detect", DETECTORS)
@pytest.mark.parametrize("text", PATHOLOGICAL)
def test_detectors_are_fast_on_pathological_text(detect, text):
    start = time.perf_counter()
    detect(text)
    assert time.perf_counter() - start < 1.0


@given(st.sampled_from(KEYS), FILL, FILL)
def test_detectors_find_a_key_between_plain_text(key, before, after):
    text = f"{before} {key} {after}"
    for detect in DETECTORS:
        assert any(m.value == key for m in detect(text))


# ---- fix: masking in place -------------------------------------------------------------------

LINE = st.tuples(FILL, st.lists(st.tuples(st.sampled_from(KEYS), FILL), max_size=3))


def _history(lines) -> bytes:
    out = []
    for lead, keys in lines:
        text = lead + "".join(f" {key} {fill}" for key, fill in keys)
        out.append(json.dumps({"display": text}) + "\n")
    return "".join(out).encode()


def _expected(data: bytes) -> bytes:
    for key in KEYS:
        data = data.replace(key.encode(), fix.mask_text(key).encode())
    return data


def _setup(tmp_path: Path, data: bytes) -> tuple[Path, Path]:
    home = tmp_path / "home"
    path = home / ".claude" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return home, path


@settings(max_examples=40, deadline=None)
@given(st.lists(LINE, min_size=1, max_size=5), TEXT)
def test_fix_changes_only_key_bytes_and_survives_appends(tmp_path_factory, lines, appended):
    tmp = tmp_path_factory.mktemp("fx")
    original = _history(lines)
    home, path = _setup(tmp, original)
    p = fix.plan(scan(home, None))
    extra = (json.dumps({"display": appended}) + "\n").encode()
    with path.open("ab") as fh:
        fh.write(extra)
    fix.apply(p, home)
    after = path.read_bytes()
    assert after == _expected(original) + extra  # only key bytes changed; appends survived
    assert len(after) == len(original) + len(extra)
    for key in KEYS:
        assert key.encode() not in after
        if key.encode() in original:
            assert key[:4].encode() + b"*" * (len(key) - 4) in after  # first 4 kept, same length
    # running fix again is a no-op
    p2 = fix.plan(scan(home, None))
    out2 = fix.apply(p2, home)
    assert out2.applied == [] and path.read_bytes() == after


# ---- guard -----------------------------------------------------------------------------------

ANY_COMMAND = st.one_of(
    st.text(max_size=120),
    st.builds(
        " ".join,
        st.lists(
            st.sampled_from(
                [
                    "npm",
                    "publish",
                    "git",
                    "push",
                    "twine",
                    "upload",
                    "&&",
                    ";",
                    "|",
                    "-w",
                    "dist/*",
                    "cd",
                    "..",
                    "'",
                    '"',
                    "$(",
                    "`",
                    "\\",
                    "uv",
                    "pnpm",
                    "-r",
                    "--tag",
                    "x.tgz",
                    "\n",
                ]
            ),
            max_size=12,
        ),
    ),
)


@settings(max_examples=150, deadline=None)
@given(ANY_COMMAND, st.booleans())
def test_guard_never_raises_on_any_command(tmp_path_factory, command, strict):
    cwd = tmp_path_factory.mktemp("g")
    evaluate(command, cwd, strict=strict)


def _leaky_dir(root: Path) -> None:
    (root / "dist").mkdir(exist_ok=True)
    members = {
        "package/.claude/settings.local.json": json.dumps(
            {"permissions": {"allow": [f"Bash(export K={fakes.ANTHROPIC})"]}}
        ).encode()
    }
    with tarfile.open(root / "demo-1.0.0.tgz", "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    with zipfile.ZipFile(root / "dist" / "demo-1.0-py3-none-any.whl", "w") as zf:
        zf.writestr("demo/.codex/auth.json", "{}")


PUBLISHES = st.sampled_from(
    [
        "npm publish demo-1.0.0.tgz",
        "pnpm publish demo-1.0.0.tgz",
        "twine upload dist/*",
        "uv publish",
    ]
)
PREFIX = st.sampled_from(["", "cd . && ", "FOO=1 ", "  ", "echo hi; ", "true && "])
SUFFIX = st.sampled_from(["", " 2>&1", " | cat"])
ODD_SUFFIX = st.sampled_from([" --access public", " --tag next", " nothing.tgz"])


@settings(max_examples=60, deadline=None)
@given(PREFIX, PUBLISHES, SUFFIX, st.booleans())
def test_a_recognised_publish_of_a_leaky_package_is_always_blocked(
    tmp_path_factory, pre, cmd, post, strict
):
    root = tmp_path_factory.mktemp("leaky")
    _leaky_dir(root)
    verdict = evaluate(pre + cmd + post, root, strict=strict)
    assert verdict is not None and verdict.block
    assert fakes.ANTHROPIC not in verdict.reason


@settings(max_examples=60, deadline=None)
@given(PREFIX, PUBLISHES, ODD_SUFFIX, st.booleans())
def test_a_publish_is_never_allowed_silently_even_with_odd_arguments(
    tmp_path_factory, pre, cmd, post, strict
):
    """Arguments agentprint cannot map to a package may fail open, but never without saying so;
    in strict mode they block."""
    root = tmp_path_factory.mktemp("odd")
    _leaky_dir(root)
    verdict = evaluate(pre + cmd + post, root, strict=strict)
    assert verdict is not None
    assert verdict.block or (not strict and verdict.warning)


# ---- pack ------------------------------------------------------------------------------------


def _valid_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("package/a.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
    return buf.getvalue()


def _valid_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b.txt", "hi")
    return buf.getvalue()


def _mutated(seed: bytes) -> st.SearchStrategy[bytes]:
    return st.tuples(
        st.integers(0, len(seed) - 1), st.binary(max_size=8), st.integers(0, len(seed))
    ).map(lambda t: seed[: t[0]] + t[1] + seed[t[0] + 1 : t[2] or None])


@settings(max_examples=200, deadline=None)
@given(
    st.one_of(st.binary(max_size=600), _mutated(_valid_tgz()), _mutated(_valid_zip())),
    st.sampled_from(["x.tgz", "x.tar.gz", "x.zip", "x.whl", "noext"]),
)
def test_reading_arbitrary_bytes_raises_only_documented_errors(data, label):
    with contextlib.suppress(pack.PackError):  # the documented error
        pack.read_archive_bytes(data, label, lambda name: True)
