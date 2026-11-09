"""The reader can only add risk (Ruling X18): every way of writing a publish that the reader
misses is "could not check" (a strict block), never a silent pass; and ordinary work stays quiet.

Each case is a publish of a leaky package (fake key from tests.fakes) written in a way the reader
round 3 review found silent. Strict mode must block it.
"""

import pytest

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


# ---- 17. code that writes its subcommand out stays quiet ---------------------------------------


@pytest.mark.parametrize(
    ("command", "files"),
    [
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
    ],
    ids=["js-filter-build", "js-rev-parse", "py-git-helper", "py-npm-install", "py-git-argv",
         "js-npm-run"],
)  # fmt: skip
def test_code_with_a_written_out_subcommand_stays_quiet(tmp_path, command, files):
    _files(tmp_path, files)
    assert evaluate(command, tmp_path) is None
    assert evaluate(command, tmp_path, strict=True) is None


# ---- make: what make fills in at run time; define blocks; huge variables ----------------------


def test_make_fills_in_at_run_time_names_a_publish(leaky):
    files = {"Makefile": "pub = $(1) publish\nrelease:\n\t$(call pub,npm)\n"}
    assert _blocks("make release", leaky, files)
    _files(
        leaky, {"Makefile": "define PUB\nnpm publish demo-1.0.0.tgz\nendef\nrelease:\n\t$(PUB)\n"}
    )
    assert evaluate("make release", leaky).block  # a define block is a variable: read for real


def test_a_huge_variable_in_one_recipe_line_stays_fast_and_seen(leaky):
    from agentprint.commands import detect
    from tests import timing

    big = "V2 = " + "x" * 100 + "\nV1 = " + "$(V2)" * 20 + "\nV0 = " + "$(V1)" * 30 + "\n"
    makefile = "NPM = npm\n" + big + "all:\n" + "\techo $(V0)\n" * 60
    _files(leaky, {"Makefile": makefile + "\t$(NPM) publish demo-1.0.0.tgz\n"})
    assert timing.best_of(lambda: detect("make", leaky), times=1) < timing.bound(1.0)  # was 2.5 s
    assert evaluate("make", leaky).block
