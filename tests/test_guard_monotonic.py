"""The reader can only add risk (Ruling X18): every way of writing a publish that the reader
misses is "could not check" (a strict block), never a silent pass; and ordinary work stays quiet.

Each case is a publish of a leaky package (fake key from tests.fakes) written in a way the reader
round 3 review found silent. Strict mode must block it.
"""

import subprocess

import pytest

from agentprint.commands import Action, detect
from agentprint.guard import evaluate
from tests.test_guard import LEAKY, _tgz

P1 = "npm publish demo-1.0.0.tgz"
CLEAN = {"package/package.json": b'{"name": "demo", "version": "1.0.0"}', "package/index.js": b"1"}


@pytest.fixture
def leaky(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {**CLEAN, "package/.claude/settings.local.json": LEAKY})
    return tmp_path


def _files(root, files):
    for rel, text in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)


def _blocks(command, root, files=None):
    _files(root, files)
    verdict = evaluate(command, root, strict=True)
    return verdict is not None and verdict.block


# ---- 1. $'...' is a literal with C escapes, never an empty string ------------------------------


@pytest.mark.parametrize(
    ("command", "files"),
    [
        ("npm $'publish' demo-1.0.0.tgz", None),
        ("$'npm' publish demo-1.0.0.tgz", None),
        ("bash -c $'npm publish demo-1.0.0.tgz'", None),
        ("eval $'npm publish demo-1.0.0.tgz'", None),
        ("sh -c $'npm\\x20publish demo-1.0.0.tgz'", None),
        ("CMD=$'npm\\x20publish'; $CMD demo-1.0.0.tgz", None),
        ("./r.sh", {"r.sh": "#!/bin/bash\nnpm $'publish' demo-1.0.0.tgz\n"}),
        ("make", {"Makefile": "all:\n\tnpm $$'publish' demo-1.0.0.tgz\n"}),
    ],
)
def test_ansi_c_strings_are_decoded(leaky, command, files):
    assert _blocks(command, leaky, files)


def test_ansi_c_push_is_read_as_a_push(tmp_path):
    assert [(r.action, r.tool) for r in detect("git $'push' origin main", tmp_path)] == [
        (Action.push, "git")
    ]


# ---- 2. text inside "..." is never cut out because of an apostrophe ----------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo \"it's $(npm publish demo-1.0.0.tgz) that's\"",
        'git commit -m "don\'t" && echo "$(npm publish demo-1.0.0.tgz)" && echo "that\'s it"',
        "echo \"it's `npm publish demo-1.0.0.tgz` that's\"",
    ],
)
def test_apostrophes_never_hide_a_substitution(leaky, command):
    assert evaluate(command, leaky).block


# ---- 3. here-documents: data, or code, by where they go ----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"cat > release.sh <<'EOF'\n#!/bin/sh\n{P1}\nEOF\nchmod +x release.sh && ./release.sh",
        f"cat > build.sh <<'EOF'\nnpm run build\n{P1}\nEOF\nbash build.sh",
        f"tee step.sh > /dev/null <<'EOF'\n{P1}\nEOF\nsh step.sh",
        f"cat > go.sh <<'EOF' && sh go.sh\n{P1}\nEOF",
        f"CMD=$(cat <<'EOF'\n{P1}\nEOF\n); $CMD",
        f"source /dev/stdin <<'EOF'\n{P1}\nEOF",
        f". /dev/stdin <<'EOF'\n{P1}\nEOF",
        f"make -f - <<'EOF'\nall:\n\t{P1}\nEOF",
        f"ash <<'EOF'\n{P1}\nEOF",
        f"tcsh <<'EOF'\n{P1}\nEOF",
        f"cat <<'EOF' \\\n  | bash\n{P1}\nEOF",
        f"while read -r c; do $c; done <<'EOF'\n{P1}\nEOF",
        "xargs npm <<'EOF'\npublish demo-1.0.0.tgz\nEOF",
        f"osascript <<'EOF'\ndo shell script \"{P1}\"\nEOF",
    ],
    ids=["run-written", "bash-written", "tee-written", "same-line", "cmd-subst", "source-stdin",
         "dot-stdin", "make-stdin", "ash", "tcsh", "continued-pipe", "while-read", "xargs",
         "unknown-sink"],
)  # fmt: skip
def test_a_here_document_that_runs_is_never_silent(leaky, command):
    assert _blocks(command, leaky)


def test_a_here_document_written_as_python_and_run_is_could_not_check(leaky):
    command = (
        "cat > x.py <<'EOF'\nimport subprocess\nsubprocess.run(['npm', 'publish'])\nEOF\n"
        "python3 x.py"
    )
    assert _blocks(command, leaky)


@pytest.mark.parametrize(
    "command",
    [
        f'cat <<E"O"F\nhello\nEOF\n{P1}',  # a partly quoted end word is EOF
        f"cat <<E\\OF\nhello\nEOF\n{P1}",
        f"echo x # \x0bcat <<EOF\n{P1}\nEOF",  # only a newline ends a comment
        f"echo x # \rcat <<EOF\n{P1}\nEOF",
        f"echo x # \u2028cat <<EOF\n{P1}\nEOF",
        f"echo $[1<<2]\n{P1}",  # arithmetic, not a here-document
    ],
    ids=["quoted-end-word", "escaped-end-word", "vt", "cr", "u2028", "old-arithmetic"],
)
def test_here_document_end_words_and_line_ends_are_read_exactly(leaky, command):
    assert evaluate(command, leaky).block


# ---- 4, 13. a wrapper's arguments: "$@" is filled in, never pasted into the text ----------------


@pytest.mark.parametrize(
    "script",
    [
        '#!/bin/sh\n# Wrapper: don\'t call it directly\nexport FOO=1\nexec "$@"\n',
        "#!/bin/bash\necho $'it\\'s'\n\"$@\"\n",
        "#!/bin/bash\nusage() {\n  cat <<EOF\nDon't run this in CI.\nEOF\n}\n"
        '[ $# -eq 0 ] && usage\nexec "$@"\n',
        '#!/bin/sh\n# usage: w.sh "cmd\nexec "$@"\n',
    ],
    ids=["apostrophe-comment", "ansi-c", "usage-heredoc", "lone-quote-comment"],
)
def test_a_wrapper_runs_what_it_is_given(leaky, script):
    _files(leaky, {"w.sh": script})
    assert evaluate(f"./w.sh {P1}", leaky).block


@pytest.mark.parametrize("arg", ["'; cd pkg2; echo '", '"; cd pkg2; echo "'])
def test_an_argument_can_not_inject_a_cd(leaky, arg):
    (leaky / "pkg2").mkdir()
    _tgz(leaky / "pkg2" / "demo-1.0.0.tgz", CLEAN)
    script = f"#!/bin/sh\n# don't run twice\necho 'Releasing $1 now'\n{P1}\n"
    _files(leaky, {"r.sh": script})
    assert evaluate(f"./r.sh {arg}", leaky).block


# ---- 5. a quoted "$CMD" handed to a shell is one argument -------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"CMD='{P1}'; bash -c \"$CMD\"",
        f'CMD="{P1}"; sh -c "$CMD"',
        f"CMD='{P1}'; sudo sh -c \"$CMD\"",
        f"CMD='{P1}'; bash -c \"${{CMD}}\"",
        'CODE=\'import os; os.system("npm publish")\'; python3 -c "$CODE"',
    ],
)
def test_a_quoted_variable_given_to_a_shell_is_read_whole(leaky, command):
    assert _blocks(command, leaky)


# ---- 6. a value set only sometimes is only known at run time -----------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"CMD='{P1}'; [ -n \"$CI\" ] || CMD='echo skip'; $CMD",
        f"CMD='{P1}'; test -z \"$CI\" && CMD='npm test'; $CMD",
        f"CMD='{P1}'; if [ -z \"$CI\" ]; then CMD='npm test'; fi; $CMD",
        f"CMD='{P1}'; case $X in a) CMD='npm test';; esac; $CMD",
        "for c in 'npm test' 'npm publish'; do $c demo-1.0.0.tgz; done",
    ],
    ids=["or", "and", "if", "case", "for"],
)
def test_a_conditional_value_is_could_not_check(leaky, command):
    assert _blocks(command, leaky)


def test_an_assignment_in_a_function_nobody_calls_does_not_count(leaky):
    assert evaluate(f"CMD='{P1}'; f() {{ CMD='npm test'; }}; $CMD", leaky).block


def test_a_cd_that_may_not_happen_checks_both_folders(tmp_path):
    (tmp_path / "pkg").mkdir()
    _tgz(tmp_path / "demo-1.0.0.tgz", {**CLEAN, "package/.claude/settings.local.json": LEAKY})
    _tgz(tmp_path / "pkg" / "demo-1.0.0.tgz", CLEAN)
    assert evaluate(f'[ -n "$CI" ] && cd pkg; {P1}', tmp_path).block  # root when $CI is empty


# ---- 7. what a child shell or script inherits ---------------------------------------------------


@pytest.mark.parametrize(
    ("command", "files"),
    [
        (f"CMD='{P1}' bash -c '$CMD'", None),
        (f"export CMD='{P1}'; bash -c '$CMD'", None),
        (f"export CMD='{P1}'; ./run.sh", {"run.sh": "#!/bin/sh\n$CMD\n"}),
        (f"export CMD='{P1}'; ./run.sh", {"run.sh": '#!/bin/sh\neval "$CMD"\n'}),
    ],
)
def test_exported_variables_reach_child_shells_and_scripts(leaky, command, files):
    assert evaluate(command, leaky, strict=False).block or _blocks(command, leaky, files)


def test_a_variable_not_exported_does_not_reach_a_child(tmp_path):
    assert detect("CMD='npm publish'; bash -c 'echo $CMD'", tmp_path) == []


# ---- 8, 12. values built at run time, and other ways to set a variable --------------------------


@pytest.mark.parametrize(
    "command",
    [
        'CMD="npm ${P:-publish}"; $CMD demo-1.0.0.tgz',
        'CMD="npm $(echo publish)"; $CMD demo-1.0.0.tgz',
        "CMD='npm test'; CMD=${CMD/test/publish}; $CMD demo-1.0.0.tgz",
        f'set -- {P1}; "$@"',
        f"printf -v CMD '{P1}'; $CMD",
        f"read -r CMD <<< '{P1}'; $CMD",
        f"read -ra CMD <<< '{P1}'; \"${{CMD[@]}}\"",
        f"X='{P1}'; N=X; ${{!N}}",
        f"CMD='{P1}'; declare -n r=CMD; $r",
        "IFS=,; CMD='npm,publish,demo-1.0.0.tgz'; $CMD",
        f"export X CMD='{P1}'; $CMD",
        f"f() {{ local a CMD='{P1}'; $CMD; }}; f",
        f"alias p='{P1}'\np",
        f"alias p='{P1}'; p",
        "".join(f"v{i}=1; " for i in range(70)) + f"CMD='{P1}'; $CMD",  # past MAX_VALUES
        f"CMD='{P1}" + " " * 4100 + "'; $CMD",  # past MAX_VALUE
        "$(echo npm publish demo-1.0.0.tgz)",
        "`echo npm publish demo-1.0.0.tgz`",
        'sh -c "$(echo npm publish demo-1.0.0.tgz)"',
        "echo publish | xargs -I{} npm {} demo-1.0.0.tgz",
        f"echo '{P1}' | sh",
        f"printf '{P1}\\n' | bash",
        "echo bnBtIHB1Ymxpc2ggZGVtby0xLjAuMC50Z3o= | base64 -d | sh",
        f"env -S '{P1}'",
        "eval \"$(printf 'npm pub%s demo-1.0.0.tgz' lish)\"",
        'eval "$(echo zgt.0.0.1-omed hsilbup mpn | rev)"',
    ],
)
def test_values_built_or_set_other_ways_are_never_silent(leaky, command):
    assert _blocks(command, leaky)


def test_xargs_with_a_publishing_tool_and_unknown_input_is_could_not_check(tmp_path):
    [risky] = detect("some-list | xargs npm", tmp_path)
    assert risky.action == Action.publish and risky.unsure


# ---- 9, 11. code: aliases, built strings, interpolation ---------------------------------------

Q = "'"


@pytest.mark.parametrize(
    "command",
    [
        f'python3 -c {Q}from subprocess import run; import sys; run(["npm", sys.argv[1]]){Q} publish',  # noqa: E501
        f'python3 -c {Q}import subprocess as sp, sys; sp.run(["npm", sys.argv[1]]){Q} publish',
        f'python3 -c {Q}import subprocess, sys; subprocess.run(["npm"] + sys.argv[1:]){Q} publish',
        f'python3 -c {Q}import subprocess; subprocess.run(["npm", "PUBLISH".lower()]){Q}',
        f'python3 -c {Q}import subprocess; subprocess.run(["npm", "hsilbup"[::-1]]){Q}',
        f'python3 -c {Q}import subprocess; subprocess.run(["npm", "pubXlish".replace("X", "")]){Q}',
        f'python3 -c {Q}import subprocess; subprocess.run(["npm", "%slish" % "pub"]){Q}',
        f'python3 -c {Q}import subprocess; subprocess.run(["npm", "pub{{}}".format("lish")]){Q}',
        f'python3 -c {Q}import subprocess; v="pub"+"lish"; t="npm"; subprocess.run([t, v]){Q}',
        f'python3 -c {Q}import asyncio, sys; asyncio.run(asyncio.create_subprocess_exec("npm", sys.argv[1])){Q} publish',  # noqa: E501
        f'python3 -c {Q}import pty, sys; pty.spawn(["npm", sys.argv[1]]){Q} publish',
        f'python3 -c {Q}import os, base64; os.system(base64.b64decode("bnBtIHB1Ymxpc2g=").decode()){Q}',  # noqa: E501
        f'python3 -c {Q}import os; os.system(bytes.fromhex("6e706d207075626c697368").decode()){Q}',
        f'python3 -c {Q}import os; os.system("npm " + "".join(map(chr, [112,117,98,108,105,115,104]))){Q}',  # noqa: E501
        f'node -e {Q}const cp=require("child_process"); cp["exec"+"Sync"]("npm " + process.argv[1]){Q} publish',  # noqa: E501
        f'node -e {Q}const {{execSync: x}}=require("child_process"); x("npm " + process.argv[1]){Q} publish',  # noqa: E501
        f'node -e {Q}require("child_process").execSync("npm " + "PUBLISH".toLowerCase()){Q}',
        f'node -e {Q}require("child_process").execSync("npm PUBLISH".toLowerCase()){Q}',
        f'ruby -e {Q}v=ARGV[0]; system("npm #{{v}}"){Q} publish',
        f"ruby -e {Q}v=ARGV[0]; `npm #{{v}}`{Q} publish",
        f'perl -e {Q}$v=shift; system("npm $v"){Q} publish',
        f'php -r {Q}$v=$argv[1]; shell_exec("npm $v");{Q} publish',
        f'python3 -c {Q}import subprocess; subprocess.run(["npm", "publish", "--dry-run=false"]){Q}',  # noqa: E501
    ],
)
def test_code_that_may_publish_is_never_silent(tmp_path, command):
    assert _blocks(command, tmp_path)


def test_a_zx_script_given_the_verb_is_could_not_check(tmp_path):
    script = 'import { $ } from "zx";\nconst v = process.argv[2];\nawait $`npm ${v}`;\n'
    assert _blocks("node release.mjs publish", tmp_path, {"release.mjs": script})


# ---- 14, 15, 20. shift, dispatch through a variable, function ends -----------------------------


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("./w.sh -v", 'if [ "$1" = -v ]; then shift; fi\n"$@"\n'),
        (
            "./w.sh -v",
            'while [ $# -gt 0 ]; do case $1 in -v) shift;; *) break;; esac; done\n"$@"\n',
        ),
        ("./w.sh", '[ "$1" = -v ] && shift\n"$@"\n'),
        ("./w.sh", 'cat <<EOF\nshift\nEOF\n"$@"\n'),
        ("./w.sh", 'f() { shift; }\nf\n"$@"\n'),
    ],
    ids=["shift-in-if", "shift-in-case", "conditional-shift", "shift-in-heredoc", "in-function"],
)
def test_shift_counts_only_where_it_surely_runs(leaky, command, script):
    _files(leaky, {"w.sh": "#!/bin/sh\n" + script})
    assert _blocks(f"{command} {P1}", leaky)


@pytest.mark.parametrize(
    "dispatch",
    ['"cmd_$1" "$@"\n', 'cmd_$1 "$@"\n', 'eval "cmd_$1"\n'],
)
def test_a_prefixed_dispatch_keeps_its_functions(leaky, dispatch):
    script = f"#!/bin/bash\ncmd_release() {{\n  {P1}\n}}\ncmd_test() {{\n  npm test\n}}\n"
    _files(leaky, {"tasks.sh": script + dispatch})
    assert evaluate("./tasks.sh release", leaky).block


def test_a_brace_indented_deeper_than_its_function_still_ends_it(leaky):
    _files(leaky, {"r.sh": f"#!/bin/bash\nhelper() {{\n  echo a\n  }}\n{P1}\n}}\n"})
    assert evaluate("./r.sh", leaky).block


# ---- 16, 17, 18: never cry wolf ------------------------------------------------------------------

LIB = '#!/bin/bash\nrun() {\n  echo "+ $*"\n  "$@"\n}\nbuild() { run make; }\n'
LIB += 'release() { cd "$PKG"; npm publish; }\n'


@pytest.mark.parametrize(
    ("command", "files"),
    [
        ("source lib.sh && build", {"lib.sh": LIB}),
        ("./build.sh", {"lib.sh": LIB, "build.sh": "#!/bin/bash\n. ./lib.sh\nbuild\n"}),
        ("node b.js", {"b.js": "const { execSync } = require('child_process');\n"
                       "for (const p of ['a']) execSync(`pnpm --filter ${p} build`);\n"}),
        ("node v.js", {"v.js": "const { execSync } = require('child_process');\n"
                       "execSync(`git rev-parse --short ${process.argv[2] || 'HEAD'}`);\n"}),
        ("python g.py", {"g.py": "import subprocess\n\ndef git(*args):\n"
                         "    return subprocess.check_output(['git', *args])\n\n"
                         "print(git('rev-parse', 'HEAD'))\n"}),
        ("python i.py", {"i.py": "import subprocess, sys\nfor pkg in sys.argv[1:]:\n"
                         "    subprocess.run(f'npm install {pkg}', shell=True)\n"}),
        ("python3 -c 'import subprocess,sys; subprocess.run([\"git\", *sys.argv[1:]])' log -1",
         None),
        ("node -e 'require(\"child_process\").execSync(`npm run ${process.argv[1]}`)' build", None),
        ('MSG=\'Release: npm publish happens in CI\'; git commit -m "$MSG"', None),
        ("git commit -m \"$(cat <<'EOF'\nfeat: push notifications\nEOF\n)\"", None),
        ("rg 'npm publish' .", None),
        ("cat > NOTES.md <<'EOF'\nThen run npm publish.\nEOF\ngit add NOTES.md", None),
        ("eval $(minikube docker-env) && docker build -t app . && docker push app", None),
    ],
    ids=["lib-run-helper", "lib-sourced-by-script", "js-filter-build", "js-rev-parse",
         "py-git-helper", "py-npm-install", "py-git-argv", "js-npm-run", "message-variable",
         "commit-heredoc", "grep-pattern", "notes-file", "docker-push"],
)  # fmt: skip
def test_ordinary_work_stays_quiet(tmp_path, command, files):
    _files(tmp_path, files)
    assert evaluate(command, tmp_path) is None
    assert evaluate(command, tmp_path, strict=True) is None


@pytest.mark.parametrize(
    "command",
    [
        'eval "$(ssh-agent -s)" && ssh-add -l; git push origin main',
        'eval "$(/opt/homebrew/bin/brew shellenv)"; git push',
        'eval "$(direnv export bash)" && git stash push -m wip',
    ],
)
def test_eval_of_setup_text_leaves_the_push_a_normal_checked_push(tmp_path, command):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    git = ["git", "-C", str(tmp_path), "-c", "user.email=a@b.c", "-c", "user.name=a"]
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    for risky in detect(command, tmp_path):
        assert not risky.unsure, risky  # only the push itself, checked for real
    verdict = evaluate(command, tmp_path, strict=True)
    assert verdict is None or (not verdict.block and not verdict.warning)


# ---- 21. a dry run counts only when it is literal and true -------------------------------------


@pytest.mark.parametrize(
    ("command", "files"),
    [
        ("npm publish --dry-run=false demo-1.0.0.tgz", None),
        ("npm publish --dry-run false demo-1.0.0.tgz", None),
        ("npm publish --dry-run --no-dry-run demo-1.0.0.tgz", None),
        ("./r.sh", {"r.sh": "#!/bin/sh\nnpm publish --dry-run=false demo-1.0.0.tgz\n"}),
    ],
)
def test_a_dry_run_that_is_off_publishes(leaky, command, files):
    _files(leaky, files)
    assert evaluate(command, leaky).block


@pytest.mark.parametrize(
    "command", ["npm publish --dry-run", "npm publish --dry-run=true", "npm publish --dry-run true"]
)
def test_a_literal_dry_run_is_still_a_dry_run(tmp_path, command):
    assert detect(command, tmp_path) == []


# ---- the backstop and the limits -------------------------------------------------------------


def test_a_publish_the_reader_cannot_follow_is_still_seen_by_the_backstop(tmp_path):
    [risky] = detect("some-wrapper npm publish", tmp_path)
    assert risky.unsure and risky.label == "npm publish"


def test_make_fills_in_at_run_time_names_a_publish(leaky):
    files = {"Makefile": "pub = $(1) publish\nrelease:\n\t$(call pub,npm)\n"}
    assert _blocks("make release", leaky, files)
    _files(
        leaky, {"Makefile": "define PUB\nnpm publish demo-1.0.0.tgz\nendef\nrelease:\n\t$(PUB)\n"}
    )
    assert evaluate("make release", leaky).block  # a define block is a variable: read for real


def test_just_arguments_that_name_a_publish_are_never_silent(leaky):
    assert _blocks(
        "just run npm publish demo-1.0.0.tgz", leaky, {"justfile": "run *A:\n    {{A}}\n"}
    )


def test_a_huge_variable_in_one_recipe_line_stays_fast_and_seen(leaky):
    from agentprint.commands import detect
    from tests import timing

    big = "V2 = " + "x" * 100 + "\nV1 = " + "$(V2)" * 20 + "\nV0 = " + "$(V1)" * 30 + "\n"
    makefile = "NPM = npm\n" + big + "all:\n" + "\techo $(V0)\n" * 60
    _files(leaky, {"Makefile": makefile + "\t$(NPM) publish demo-1.0.0.tgz\n"})
    assert timing.best_of(lambda: detect("make", leaky), times=1) < timing.bound(1.0)  # was 2.5 s
    assert evaluate("make", leaky).block
