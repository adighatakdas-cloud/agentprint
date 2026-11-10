"""Files the command line writes and then runs (reader round 4: N1, N2, I4, I5).

A text written to a file counts as run when a later command can run that file: a shell, an
interpreter, `source`, `./x`, make/just/npm for their own files, an unknown program, or a
program named through a variable. Then a publish in it is followed, or "could not check",
never silent. Written and only committed, listed or printed, it is data: never a warning.

Every publish here is of a leaky package (fake key from tests.fakes via tests.test_guard).
"""

import subprocess

import pytest

from agentprint import commands, shell
from agentprint.commands import Action, detect
from agentprint.guard import evaluate
from tests.test_guard import LEAKY, _tgz

P = "npm publish demo-1.0.0.tgz"
CLEAN = {"package/package.json": b'{"name": "demo", "version": "1.0.0"}', "package/index.js": b"1"}


@pytest.fixture
def leaky(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {**CLEAN, "package/.claude/settings.local.json": LEAKY})
    return tmp_path


def _hd(target: str, body: str = P) -> str:
    return f"cat > {target} <<'EOF'\n{body}\nEOF\n"


def _verdicts(command, root):
    return evaluate(command, root), evaluate(command, root, strict=True)


def _backstop_only(text, cwd):
    nodes = shell.parse(shell.lex(text).tokens)
    return [(r.action, r.tool) for r in commands._backstop(commands._Text(text, nodes, cwd))]


# ---- N1: written to a file named through a variable or a substitution, then run -------------


@pytest.mark.parametrize(
    "command",
    [
        "F=r.sh; " + _hd('"$F"') + 'bash "$F"',
        "T=$(mktemp); " + _hd('"$T"') + 'bash "$T"',
        _hd('"$TMPDIR/r.sh"') + 'bash "$TMPDIR/r.sh"',
        "D=scripts; mkdir -p $D; " + _hd('"$D/r.sh"') + 'bash "$D/r.sh"',
        f'F=r.sh; echo \'{P}\' > "$F"; sh "$F"',
        f"F=r.sh; printf '%s\\n' '{P}' > \"$F\" && sh \"$F\"",
        f"{{ echo '{P}'; }} > r.sh; sh r.sh",
        _hd("/tmp/agentprint-r.sh") + "bash /tmp/agentprint-r.sh",
        f"echo '{P}' > r.sh; sh \"$PWD/r.sh\"",
    ],
)
def test_a_written_file_the_reader_can_follow_is_checked_for_real(leaky, command):
    default, strict = _verdicts(command, leaky)
    assert default.block and strict.block


@pytest.mark.parametrize(
    "command",
    [
        _hd("step.sh") + 'for f in step*; do bash "$f"; done',
        f"echo '{P}' > r.sh; for f in *.sh; do sh \"$f\"; done",
        f"echo '{P}' > r.sh; sh ./r*.sh",
        "mkdir -p bin && printf '#!/bin/sh\\n" + P + "\\n' > bin/rel && chmod +x bin/rel && "
        'PATH="$PWD/bin:$PATH" rel',
        f"echo '{P}' > r.sh; find . -name r.sh -exec sh {{}} \\;",
        f"echo '{P}' > r.sh; ls *.sh | xargs -n1 sh",
        f"echo '{P}' > r.sh; sh $(ls *.sh)",
        _hd("r.sh") + 'cat r.sh >/dev/null; bash "$X"',  # cat is no run: it hides nothing
        'run() { for f in *.sh; do sh "$f"; done; }; ' + _hd("r.sh") + "run",
    ],
)
def test_a_written_file_run_in_a_way_the_reader_cannot_follow_is_could_not_check(leaky, command):
    default, strict = _verdicts(command, leaky)
    assert strict.block and (default.block or default.warning)


# ---- N2: a file a tool reads by itself ----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        _hd("Makefile", f"release:\n\t{P}") + "make release",
        f"printf 'release:\\n\\t{P}\\n' > Makefile && make release",
        _hd("justfile", f"release:\n    {P}") + "just release",
        _hd("package.json", '{"name": "demo", "scripts": {"release": "' + P + '"}}')
        + "npm run release",
        _hd("package.json", '{"name": "demo", "scripts": {"release": "' + P + '"}}')
        + "pnpm release",
        f'echo \'{{"scripts": {{"release": "{P}"}}}}\' > package.json && npm run release',
        _hd("package.json", '{"scripts": {"postversion": "' + P + '"}}') + "npm version patch",
    ],
)
def test_a_makefile_justfile_or_package_json_the_command_wrote_is_read(leaky, command):
    default, strict = _verdicts(command, leaky)
    assert default.block and strict.block


def test_install_scripts_in_a_written_package_json_are_could_not_check(leaky):
    command = _hd("package.json", '{"scripts": {"prepare": "' + P + '"}}') + "npm install"
    assert _verdicts(command, leaky)[1].block


# ---- I4: sinks that write what they read into a file named in their words ---------------------


@pytest.mark.parametrize(
    "command",
    [
        f"cp /dev/stdin run.sh <<'EOF'\n{P}\nEOF\nsh run.sh",
        f"sort -o run.sh <<'EOF'\n{P}\nEOF\nsh run.sh",
        f"uniq - run.sh <<'EOF'\n{P}\nEOF\nsh run.sh",
        f"dd of=run.sh <<'EOF'\n{P}\nEOF\nsh run.sh",
    ],
)
def test_a_sink_that_writes_a_file_from_its_input_is_followed(leaky, command):
    default, strict = _verdicts(command, leaky)
    assert default.block and strict.block


@pytest.mark.parametrize(
    "command",
    [
        f"base64 -i - -o x.b64 <<'EOF'\n{P}\nEOF\nbase64 -d -i x.b64 | sh",
        f"pbcopy <<'EOF'\n{P}\nEOF\npbpaste | sh",
        f"echo '{P}' | xargs -d '\\n' sh -c",
        f"echo '{P}' | xargs -0 bash -c",
    ],
)
def test_text_stored_or_split_and_then_run_is_never_silent(leaky, command):
    assert _verdicts(command, leaky)[1].block


# ---- I5: written and only committed, listed or printed: data -------------------------------

WORKFLOW = (
    "name: release\non: push\njobs:\n  publish:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: npm ci\n      - run: npm publish --provenance\n"
)


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p .github/workflows && " + _hd(".github/workflows/release.yml", WORKFLOW)
        + "git add .github/workflows/release.yml && git commit -m 'ci: add release workflow'",
        _hd("README.md", "# demo\n\n    npm publish --access public\n") + "git add README.md",
        _hd("RELEASING.md", "1. npm version patch\n2. git push --follow-tags\n3. npm publish")
        + "git add RELEASING.md && git commit -m 'docs: releasing'",
        _hd("scripts/release.sh", "#!/bin/sh\nnpm publish")
        + "chmod +x scripts/release.sh && git add scripts/release.sh",
        "F=docs/publish.md; " + _hd('"$F"', "Run npm publish after tagging.") + 'git add "$F"',
        "printf 'npm publish\\n' > NOTES.txt && git add NOTES.txt && cat NOTES.txt",
        _hd("package.json", '{"scripts": {"release": "npm publish", "test": "node t.js"}}')
        + "npm test",
        _hd("Makefile", "build:\n\tnpm run build\nrelease:\n\tnpm publish") + "make build",
        "T=$(mktemp); " + _hd('"$T"', "npm test") + 'bash "$T"; rm -f "$T"',
        _hd("notes.md", "npm publish") + "prettier --write notes.md",
    ],
)  # fmt: skip
def test_a_file_written_and_only_committed_or_read_stays_quiet(tmp_path, command):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert _verdicts(command, tmp_path) == (None, None)


# ---- the backstop on its own -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "runs"),
    [
        ('F=r.sh; cat > "$F" <<\'EOF\'\nnpm publish x\nEOF\nbash "$G"', True),
        ("cat > r.sh <<'EOF'\nnpm publish x\nEOF\n$RUNNER r.sh", True),
        ("cat > r.sh <<'EOF'\nnpm publish x\nEOF\nsource r.sh", True),
        ("cat > r.sh <<'EOF'\nnpm publish x\nEOF\neval \"$(cat r.sh)\"", True),
        ("echo 'npm publish x' > Makefile\nmake", True),
        ("cat > r.sh <<'EOF'\nnpm publish x\nEOF\ngit add r.sh && cat r.sh && ls", False),
        ("cat > r.sh <<'EOF'\nnpm publish x\nEOF\nbash other.sh", False),
        ("echo 'npm publish x' > notes.md; prettier notes.md", False),
    ],
)
def test_the_backstop_counts_a_written_file_only_when_a_later_command_may_run_it(
    tmp_path, text, runs
):
    assert bool(_backstop_only(text, tmp_path)) == runs


def test_a_written_file_run_before_it_is_written_is_still_counted_in_a_loop(tmp_path):
    text = "for i in 1 2; do sh r.sh; echo 'npm publish x' > r.sh; done"
    assert _backstop_only(text, tmp_path) == [(Action.publish, "npm")]
    assert detect(text, tmp_path)


def test_many_writes_and_runs_stay_fast(tmp_path):
    from tests import timing

    command = "".join(f"echo 'npm publish x' > f{i}.txt; sh g{i}.sh; " for i in range(1300))
    assert len(command) < 64 * 1024
    assert timing.best_of(lambda: detect(command, tmp_path), times=2) < timing.bound(1.0)
