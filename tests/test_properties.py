"""Property-based tests (hypothesis) on the riskiest code: in-place masking, redaction, secret
detection, the publish guard and the archive readers. Keys come from tests.fakes only."""

import base64
import contextlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentprint import fix, pack
from agentprint.checks.base import safe_snippet
from agentprint.guard import evaluate
from agentprint.scanner import scan
from agentprint.secrets import find_secrets, find_secrets_deep, redact
from tests import fakes, timing

# Detector functions that must hold the span properties. find_secrets_deep also reports keys
# hidden on purpose; their spans cover the joins or the base64 run they were found in.
DETECTORS = [find_secrets, find_secrets_deep]

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


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# Each way find_secrets_deep un-hides a key, given the key and the two pieces it is cut into.
# The cut is early in the key, so the first piece is never a (shorter) key of its own.
IN_KEY = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
TRICKS = {
    "invisible": lambda key, a, b: f"{a}{IN_KEY[len(a) % len(IN_KEY)]}{b}",
    "invisible everywhere": lambda key, a, b: "\u200b".join(key),
    "plus": lambda key, a, b: f'k = "{a}" + "{b}";',
    "single quotes": lambda key, a, b: f"k = '{a}' + '{b}';",
    "side by side": lambda key, a, b: f'k = "{a}" "{b}"',
    "over lines": lambda key, a, b: f'k = (\n    "{a}"\n    "{b}"\n)',
    "continuation": lambda key, a, b: f"export K={a}\\\n    {b}",
    "yaml block": lambda key, a, b: f"env:\n  data: |\n    {a}\n    {b}\nx: 1",
    "base64": lambda key, a, b: _b64(f"here it is: {key}\n"),
    "json split": lambda key, a, b: json.dumps({"content": f'k = "{a}" + "{b}";'}),
    "json invisible": lambda key, a, b: json.dumps({"display": f"{a}\u200b{b}"}),
    "empty quotes": lambda key, a, b: f'export K={a}""{b} && go',
    "empty single quotes": lambda key, a, b: f"export K={a}''{b} && go",
    "json side by side": lambda key, a, b: json.dumps({"content": f'k = "{a}""{b}";'}),
    "json empty quotes": lambda key, a, b: json.dumps({"display": f'export K={a}""{b}'}),
    "base64 of the key alone": lambda key, a, b: _b64(key),  # under 40 characters for AWS
}
HIDDEN = st.tuples(st.sampled_from(KEYS), st.integers(1, 10), st.sampled_from(sorted(TRICKS))).map(
    lambda t: (t[0], TRICKS[t[2]](t[0], t[0][: t[1]], t[0][t[1] :]))
)
HIDDEN_TEXT = HIDDEN.map(lambda pair: pair[1])


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


# ---- find_secrets and find_secrets_deep ------------------------------------------------------

MIXED = st.lists(
    st.one_of(
        TEXT,
        st.sampled_from(KEYS),
        ALMOST_KEYS,
        FILL,
        HIDDEN_TEXT,
        st.sampled_from(["-", "_", "a", "9"]).map(lambda c: c * 500),
    ),
    max_size=8,
).map("".join)


@settings(max_examples=150, deadline=None)
@given(MIXED)
def test_detectors_return_sorted_in_bounds_spans_plain_ones_disjoint(text):
    for detect in DETECTORS:
        found = detect(text)
        start_before = plain_end_before = 0
        for m in found:
            assert 0 <= m.start < m.end <= len(text)
            assert m.start >= start_before  # sorted
            start_before = m.start
            if m.obfuscation is None:  # a plain key is exactly its own span
                assert text[m.start : m.end] == m.value
                assert m.start >= plain_end_before  # plain keys never overlap each other
                plain_end_before = m.end
            # A hidden key's span covers the joins, or the base64 run or JSON string it was
            # found in, so it may overlap another hidden key's (a base64 run that runs into a
            # key joined by zero-width characters, say).


@settings(max_examples=100, deadline=None)
@given(HIDDEN, FILL, FILL)
def test_deep_detector_finds_a_hidden_key_the_plain_one_misses(hidden, before, after):
    key, hidden_text = hidden
    text = f"{before}\n{hidden_text}\n{after}"
    assert all(m.value != key for m in find_secrets(text))
    assert [m.obfuscation is not None for m in find_secrets_deep(text) if m.value == key] == [True]


def _shown(text: str) -> str:
    """What safe_snippet does to text besides redacting: control characters out, spaces folded."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", text).split())


@settings(max_examples=150, deadline=None)
@given(MIXED)
def test_every_span_the_deep_detector_reports_is_redacted_in_snippets(text):
    shown = safe_snippet(text, limit=10 * len(text) + 100)
    before = _shown(text)
    spans = {(m.start, m.end) for m in find_secrets_deep(text)}  # two keys can share a span
    found = [_shown(text[start:end]) for start, end in spans]
    for piece in found:
        # Every reported copy is gone. A look-alike the detector does not report may stay: the
        # same base64 run with one more character in front decodes to something else.
        assert shown.count(piece) <= before.count(piece) - found.count(piece)


PATHOLOGICAL = {
    "dashes": "-" * 500,
    "underscores": "_" * 500,
    "letters": "a" * 500,
    "digits": "9" * 500,
    "mixed run": "a-9_" * 125,
    "dash blocks": ("-" * 499 + "a") * 2000,
    "endless key": "sk-ant-api03-" + "a" * 100000,
}


@pytest.mark.parametrize("detect", DETECTORS, ids=lambda d: d.__name__)
@pytest.mark.parametrize("name", PATHOLOGICAL)
def test_detectors_are_fast_on_pathological_text(detect, name):
    text = PATHOLOGICAL[name]
    assert timing.best_of(lambda: detect(text)) < timing.bound(1.0)


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
