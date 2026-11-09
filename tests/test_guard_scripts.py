"""The guard on publishes hidden in scripts: a real check when it can read the commands, and
"could not check" (a warning, or a block in strict mode) when it cannot."""

import json

import pytest

from agentprint.guard import STRICT_HINT, evaluate
from agentprint.secrets import redact
from tests import fakes
from tests.test_guard import LEAKY, _tgz


@pytest.fixture
def leaky(tmp_path):
    _tgz(tmp_path / "demo-1.0.0.tgz", {"package/.claude/settings.local.json": LEAKY})
    return tmp_path


POSTVERSION = {"postversion": "npm publish demo-1.0.0.tgz"}


def _script(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("files", "command"),
    [
        ({"release.sh": "#!/bin/sh\nset -e\nnpm publish demo-1.0.0.tgz\n"}, "./release.sh"),
        ({"Makefile": "release:\n\t@npm publish demo-1.0.0.tgz\n"}, "make release"),
        ({"justfile": "release:\n    npm publish demo-1.0.0.tgz\n"}, "just release"),
        ({"package.json": json.dumps({"scripts": POSTVERSION})}, "npm version patch"),
    ],
    ids=["shell-script", "make", "just", "npm-version"],
)
def test_a_leak_published_from_a_script_blocks(leaky, files, command):
    for rel, text in files.items():
        _script(leaky, rel, text)
    verdict = evaluate(command, leaky)
    assert verdict.block
    assert verdict.reason.startswith("agentprint: BLOCK")
    assert command in verdict.reason  # the agent is told what to re-run
    assert fakes.ANTHROPIC not in verdict.reason


def test_a_clean_publish_from_a_script_is_allowed_quietly(tmp_path):
    _tgz(tmp_path / "ok-1.0.0.tgz", {"package/index.js": b"1"})
    _script(tmp_path, "release.sh", "npm publish ok-1.0.0.tgz\n")
    verdict = evaluate("./release.sh", tmp_path)
    assert not verdict.block and verdict.warning is None


def test_a_script_that_does_not_publish_is_not_our_business(tmp_path):
    _script(tmp_path, "build.sh", "#!/bin/sh\n# later: npm publish\nnpm run build\n")
    assert evaluate("./build.sh", tmp_path) is None


RELEASE_PY = 'import subprocess\nsubprocess.run(["twine", "upload", "dist/*"])\n'
COULD_NOT = (
    'agentprint could not check "scripts/release.py": it runs `twine upload` from code '
    "agentprint does not read as commands. Run the publish or push as its own command, so "
    "agentprint can check it."
)


def test_python_release_script_warns_by_default_and_blocks_in_strict_mode(tmp_path):
    _script(tmp_path, "scripts/release.py", RELEASE_PY)
    default = evaluate("python scripts/release.py", tmp_path)
    assert not default.block and default.warning == COULD_NOT
    strict = evaluate("python scripts/release.py", tmp_path, strict=True)
    assert strict.block
    assert strict.reason == f"agentprint: BLOCK (strict mode)\n{COULD_NOT}\n{STRICT_HINT}\n"


def test_a_publish_named_only_in_a_python_comment_is_never_a_block_by_default(tmp_path):
    _script(tmp_path, "build.py", "# CI runs npm publish after this step\nprint('built')\n")
    verdict = evaluate("python build.py", tmp_path)
    assert not verdict.block and 'could not check "build.py"' in verdict.warning


def test_a_large_script_that_names_a_publish_warns_or_blocks_in_strict_mode(tmp_path):
    _script(tmp_path, "release.sh", "#!/bin/sh\n" + PAD + "npm publish\n")
    default = evaluate("./release.sh", tmp_path)
    assert not default.block
    assert default.warning == (
        'agentprint could not check "release.sh": it is larger than 64 KB and names '
        "`npm publish`, so agentprint did not follow it. Run the publish or push as its own "
        "command, so agentprint can check it."
    )
    assert evaluate("./release.sh", tmp_path, strict=True).block


# ---- never cry wolf: ordinary work stays silent, in strict mode too -------------------------

PAD = "ac_cv_x=yes; echo checking for something\n" * 2500  # 100 KB of ordinary shell
BUNDLE = (
    "(function(){var a=[];" + "a.push({id:1,name:'x'});console.log(a.length);" * 2200 + "})()\n"
)


def _repo(root, files):
    for rel, text in files.items():
        _script(root, rel, text)
    return root


def _recursive_make(levels):
    files, path = {}, ""
    for i in range(levels):
        files[f"{path}Makefile"] = f"all:\n\t$(MAKE) -C d{i}\n"
        path += f"d{i}/"
    files[f"{path}Makefile"] = "all:\n\tcc -c x.c\n"
    return files


ORDINARY = [
    ({"configure": "#!/bin/sh\n" + PAD}, "./configure"),
    ({"configure": "#!/bin/sh\n" + PAD}, "sh ./configure --prefix=/usr"),
    ({"Makefile": "all: build\nbuild:\n\tcc -o app main.c\ntest:\n\t./app\n" + "# " + PAD[:70000]},
     "make"),
    ({"Makefile": "all: build\nbuild:\n\tcc -o app main.c\ntest:\n\t./app\n" + "# " + PAD[:70000]},
     "make test"),
    ({"Makefile": "all: build\nbuild:\n\tcc -o app main.c\ntest:\n\t./app\n" + "# " + PAD[:70000]},
     "make -j8"),
    ({"package.json": json.dumps({"scripts": {"build": "./build.sh"}}), "build.sh": PAD},
     "npm run build"),
    ({"dist/server.js": BUNDLE}, "node dist/server.js"),
    ({"manage.py": "import sys\n" + "x = 1  # push the value along\n" * 4000},
     "python manage.py runserver"),
    ({"Makefile": "all: " + " ".join(f"g{i}" for i in range(420)) + " sub\n"
      + "".join(f"g{i}:\n\techo gen {i}\n" for i in range(420)) + "sub:\n\t$(MAKE) -C lib\n",
      "lib/Makefile": "all:\n\tcc -c x.c\n"}, "make"),
    ({"Makefile": "all:\n" + "".join(f"\t$(MAKE) t{i}\n" for i in range(300))
      + "".join(f"t{i}:\n\techo {i}\n" for i in range(300))}, "make"),
    (_recursive_make(5), "make"),
    ({"package.json": json.dumps({"scripts": {"build": "sh scripts/build.sh"}}),
      "scripts/build.sh": "make all\n", "Makefile": "all:\n\t./scripts/gen.sh\n",
      "scripts/gen.sh": "./scripts/gen2.sh\n", "scripts/gen2.sh": "python3 tools/x.py\n",
      "tools/x.py": "print(1)\n"}, "npm run build"),
]  # fmt: skip


@pytest.mark.parametrize(("files", "command"), ORDINARY, ids=[c for _, c in ORDINARY])
def test_ordinary_work_is_never_a_warning_or_a_strict_block(tmp_path, files, command):
    root = _repo(tmp_path, files)
    assert evaluate(command, root) is None
    assert evaluate(command, root, strict=True) is None


# ---- where following stops: found, or "could not check"; strict blocks ----------------------

PACKAGE = {"scripts": {"release": "npm publish", "postversion": "npm publish"}}


@pytest.mark.parametrize(
    "last",
    [
        "npm run release",
        "yarn release",
        "pnpm release",
        "npm version patch",
        "make release",
        "bash -c 'npm publish'",
    ],
)
def test_a_publish_at_the_end_of_a_long_chain_is_never_silent(tmp_path, last):
    files = {f"s{i}.sh": f"./s{i + 1}.sh\n" for i in range(1, 5)}
    files.update(
        {
            "s5.sh": last + "\n",
            "package.json": json.dumps(PACKAGE),
            "Makefile": "release:\n\tnpm publish\n",
        }
    )
    root = _repo(tmp_path, files)
    verdict = evaluate("./s1.sh", root)
    assert verdict is not None and verdict.warning
    assert evaluate("./s1.sh", root, strict=True).block


def test_a_mixed_chain_of_package_scripts_scripts_and_make(tmp_path, leaky):
    scripts = {"release": "./release.sh", "pub": "bash -c 'npm publish demo-1.0.0.tgz'"}
    root = _repo(
        leaky,
        {
            "package.json": json.dumps({"scripts": scripts}),
            "release.sh": "make publish\n",
            "Makefile": "publish:\n\tnpm run pub\n",
        },
    )
    assert evaluate("npm run release", root).block  # followed all the way: a real BLOCK


# ---- a line shlex can't read hides only itself ----------------------------------------------

PUBLISH = "npm publish demo-1.0.0.tgz\n"
ANSI = "#!/bin/bash\necho $'it\\'s ok'\necho \"unclosed\n"


@pytest.mark.parametrize(
    "files",
    [
        {"release.sh": ANSI + "make release\n", "Makefile": "release:\n\t" + PUBLISH},
        {"release.sh": ANSI + "./publish.sh\n", "publish.sh": "npm publish demo-1.0.0.tgz\n"},
        {"release.sh": ANSI + "npm version patch\n",
         "package.json": json.dumps({"scripts": {"postversion": "npm publish demo-1.0.0.tgz"}})},
    ],
    ids=["make", "script", "npm-version"],
)  # fmt: skip
def test_a_bad_quote_does_not_hide_the_rest_of_a_script(leaky, files):
    root = _repo(leaky, files)
    assert evaluate("./release.sh", root).block


# ---- a script's own folder ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'cd "$(dirname "$0")"',
        "cd $(dirname $0)",
        'cd "$(dirname "${BASH_SOURCE[0]}")"',
        'cd "${BASH_SOURCE%/*}"',
        'cd "${0%/*}"',
        'cd "$(cd "$(dirname "$0")" && pwd)"',
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\ncd "$HERE"',
        'cd -- "$(dirname -- "$0")"',
        'cd -P "$(dirname "$0")"',
        'cd "$(dirname "$(realpath "$0")")"',
        'DIR=$(dirname "$(readlink -f "$0")")\ncd "$DIR"',
        'cd "$(dirname "${0}")"',
        'cd "$(dirname "$BASH_SOURCE")"',
        'D=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )\ncd "$D"',
        'D="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"\ncd "$D"',
    ],
)
def test_cd_to_the_scripts_own_folder(leaky, line):
    root = _repo(
        leaky,
        {
            "scripts/release.sh": f"#!/bin/sh\n{line}\n./publish-npm.sh\n",
            "scripts/publish-npm.sh": "npm publish ../demo-1.0.0.tgz\n",
        },
    )
    assert evaluate("./scripts/release.sh", root).block


def test_a_missing_script_after_an_unknown_cd_is_could_not_check_when_it_looks_like_a_release(
    tmp_path,
):
    root = _repo(
        tmp_path,
        {
            "release.sh": 'cd "$PKG_DIR"\n./publish.sh\n',
            "build.sh": 'cd "$BUILD_DIR"\n./configure\n',
        },
    )
    verdict = evaluate("./release.sh", root)
    assert not verdict.block and 'could not check "./publish.sh"' in verdict.warning
    assert evaluate("./build.sh", root) is None


def test_could_not_check_names_the_file_and_the_next_step():
    from pathlib import Path

    from agentprint.commands import Action, Risky
    from agentprint.guard import _could_not
    from agentprint.models import CannotCheck

    item = Risky(
        Action.publish, "sh", Path("/x"), unsure="agentprint could not open it", label="ship.sh"
    )
    assert _could_not(item, CannotCheck(item.unsure)) == (
        'agentprint could not check "ship.sh": agentprint could not open it. Run the publish or '
        "push as its own command, so agentprint can check it."
    )
    plain = Risky(Action.push, "git", Path("/x"), unsure="its folder is unknown")
    assert _could_not(plain, CannotCheck(plain.unsure)) == (
        "agentprint could not check this git push: its folder is unknown."
    )


def test_own_folder_is_written_with_forward_slashes_for_a_windows_path():
    from pathlib import PureWindowsPath

    from agentprint.following import _own_folder

    folder = PureWindowsPath(r"C:\Users\me\proj\scripts")
    text = 'cd $(dirname $0)\nHERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\ncd "$HERE"\n'
    assert _own_folder(text, folder) == (
        'cd C:/Users/me/proj/scripts\nHERE="C:/Users/me/proj/scripts"\n'
        'cd "C:/Users/me/proj/scripts"\n'
    )


def test_a_script_with_windows_line_ends_is_read_the_same(leaky):
    line = 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\r\ncd "$HERE"'
    _repo(leaky, {"scripts/publish-npm.sh": "npm publish ../demo-1.0.0.tgz\n"})
    (leaky / "scripts" / "release.sh").write_bytes(
        f"#!/bin/sh\r\n{line}\r\n./publish-npm.sh\r\n".encode()
    )
    assert evaluate("./scripts/release.sh", leaky).block


# ---- a `$'` inside an ordinary quote is a regex end, not an ANSI-C string --------------------

CLEAN = {
    "package/package.json": b'{"name": "demo", "version": "1.0.0"}',
    "package/index.js": b"1\n",
}


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("grep -q '1.0.0$' CHANGELOG.md && npm publish demo-1.0.0.tgz --tag 'next'", None),
        ("./release.sh", "#!/bin/sh\nif grep -q 'release$' notes.txt; then\n"
         "  npm publish demo-1.0.0.tgz\nfi\necho 'a' 'b'\n"),
    ],
    ids=["command-line", "script"],
)  # fmt: skip
def test_a_dollar_at_the_end_of_a_quoted_regex_is_just_text(leaky, command, script):
    if script:
        _script(leaky, "release.sh", script)
    assert evaluate(command, leaky).block


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("grep -q 'v1$' CHANGELOG.md && cd pkg && echo 'ok'; npm publish demo-1.0.0.tgz", None),
        ("./release.sh", "#!/bin/sh\nset -e\ngrep -q 'v1$' CHANGELOG.md && cd pkg && echo 'ok'\n"
         "npm publish demo-1.0.0.tgz\n"),
    ],
    ids=["command-line", "script"],
)  # fmt: skip
def test_a_dollar_quote_does_not_swallow_a_cd(tmp_path, command, script):
    (tmp_path / "pkg").mkdir()
    _tgz(tmp_path / "demo-1.0.0.tgz", CLEAN)  # the folder it would wrongly check: clean
    _tgz(
        tmp_path / "pkg" / "demo-1.0.0.tgz", {**CLEAN, "package/.claude/settings.local.json": LEAKY}
    )
    if script:
        _script(tmp_path, "release.sh", script)
    assert evaluate(command, tmp_path).block


def test_git_push_after_a_quoted_regex_is_read_exactly(tmp_path):
    from agentprint.commands import Action, detect

    want = [(Action.push, "git", tmp_path, "")]
    got = detect("grep -c 'x$' a.txt; git push origin 'main'", tmp_path)
    assert [(r.action, r.tool, r.cwd, r.unsure) for r in got] == want
    _script(
        tmp_path,
        "tag.sh",
        "#!/bin/sh\ngit tag -l | grep -q '^v1$' || exit 1\ngit push origin 'main'\n",
    )
    got = detect("./tag.sh", tmp_path)
    assert [(r.action, r.tool, r.cwd, r.unsure) for r in got] == want


# ---- an include too large to follow does not hide the makefile's own recipes ----------------

DEPS = "".join(f"obj/f{i}.o: src/f{i}.c include/common.h include/util.h\n" for i in range(1500))


@pytest.mark.parametrize("include", ["include deps.mk", "-include deps.mk", "sinclude deps.mk"])
def test_a_large_include_keeps_the_makefiles_own_recipes(leaky, include):
    _repo(
        leaky,
        {
            "deps.mk": DEPS,
            "Makefile": f"{include}\nall:\n\tcc -o app main.c\nrelease:\n\t{PUBLISH}",
        },
    )
    assert evaluate("make release", leaky).block
    assert evaluate("make all", leaky) is None  # and no wolf from the include's own scan


def test_a_large_include_or_second_makefile_that_names_a_publish_is_scanned(tmp_path):
    from agentprint.commands import detect

    _repo(
        tmp_path,
        {"deps.mk": DEPS + "ship:\n\tgit push\n", "Makefile": "include deps.mk\nall:\n\techo\n"},
    )
    got = detect("make all", tmp_path)
    assert [(r.tool, r.label) for r in got] == [("git", "deps.mk")] and got[0].unsure
    _repo(tmp_path, {"a.mk": "release:\n\techo hi\n"})
    assert [(r.tool, r.label) for r in detect("make -f a.mk -f deps.mk release", tmp_path)] == [
        ("git", "deps.mk")
    ]


# ---- never print a key ------------------------------------------------------------------------


def test_a_key_on_a_line_past_the_depth_limit_is_never_printed(tmp_path):
    key = fakes.NEW["npm"]
    files = {f"s{i}.sh": f"./s{i + 1}.sh\n" for i in range(1, 5)}
    files.update(
        {
            "s5.sh": "npm run release\n",
            "package.json": json.dumps({"scripts": {"release": f"NPM_TOKEN={key} npm publish"}}),
        }
    )
    root = _repo(tmp_path, files)
    for verdict in (evaluate("./s1.sh", root), evaluate("./s1.sh", root, strict=True)):
        text = (verdict.warning or "") + verdict.reason
        assert key not in text and key[4:] not in text and key[:5] not in text
    assert 'could not check "npm publish": it is nested deeper' in evaluate("./s1.sh", root).warning


def test_a_key_in_any_could_not_check_reason_is_cut_to_its_stub(tmp_path):
    key = fakes.GITHUB
    verdict = evaluate(f'cd "$HOME/{key}" && npm publish', tmp_path)
    assert key not in verdict.warning and redact(key) in verdict.warning
    _repo(tmp_path, {"run.sh": f'cd "$X/{key}"\n./release.sh\n'})
    verdict = evaluate("./run.sh", tmp_path)
    assert key not in verdict.warning and redact(key) in verdict.warning


SCRIPT_DIR = 'D=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )'


@pytest.mark.parametrize(
    "line",
    [
        SCRIPT_DIR + '\n"$D/publish-npm.sh"',
        'HERE="$(cd "$(dirname "$0")" && pwd)"\n"$HERE"/publish-npm.sh',
        '"$(dirname "$0")"/publish-npm.sh',
        '"${BASH_SOURCE%/*}/publish-npm.sh"',
        'source "$(dirname "$0")/publish-npm.sh"',
    ],
)
def test_a_sibling_script_named_through_the_scripts_own_folder(leaky, line):
    root = _repo(
        leaky,
        {
            "scripts/release.sh": f"#!/bin/bash\n{line}\n",
            "scripts/publish-npm.sh": PUBLISH,  # runs in the caller's folder
        },
    )
    assert evaluate("bash scripts/release.sh", root).block


@pytest.mark.parametrize("cd", ["cd -- pkg", "cd -P pkg", "cd -L pkg", "pushd -- pkg"])
def test_cd_options_come_before_the_folder(tmp_path, cd):
    from agentprint.commands import Action, detect

    got = detect(f"{cd} && npm publish", tmp_path)
    assert [(r.action, r.cwd, r.unsure) for r in got] == [(Action.publish, tmp_path / "pkg", "")]


def test_a_missing_release_script_after_an_unknown_cd_on_the_command_line(tmp_path):
    verdict = evaluate('cd "$PKG" && ./publish.sh', tmp_path)
    assert not verdict.block and 'could not check "./publish.sh"' in verdict.warning
    assert evaluate('cd "$PKG" && ./publish.sh', tmp_path, strict=True).block
    assert evaluate('cd "$BUILD" && ./configure', tmp_path) is None  # never cry wolf


# ---- wrappers that forward their arguments ---------------------------------------------------


@pytest.mark.parametrize(
    ("files", "command"),
    [
        ({"with-env.sh": '#!/bin/sh\nset -e\nexport FOO=1\nexec "$@"\n'},
         "./with-env.sh npm publish demo-1.0.0.tgz"),
        ({"npmw": '#!/bin/sh\nnpm "$@"\n'}, "./npmw publish demo-1.0.0.tgz"),
        ({"wrap.sh": '#!/bin/bash\n"$@"\n'}, "bash wrap.sh npm publish demo-1.0.0.tgz"),
        ({"pub.sh": '#!/bin/sh\nnpm publish "$1"\n'}, "./pub.sh demo-1.0.0.tgz"),
        ({"run.sh": "#!/bin/sh\nexec $*\n"}, "./run.sh npm publish demo-1.0.0.tgz"),
    ],
    ids=["exec-all", "npm-all", "bash-script", "first-arg", "star"],
)  # fmt: skip
def test_a_wrapper_is_followed_with_the_arguments_it_is_given(leaky, files, command):
    _repo(leaky, files)
    assert evaluate(command, leaky).block


def test_a_wrapper_given_nothing_to_publish_stays_quiet(tmp_path):
    _repo(tmp_path, {"with-env.sh": '#!/bin/sh\nexec "$@"\n'})
    assert evaluate("./with-env.sh npm test", tmp_path) is None
    assert evaluate("./with-env.sh", tmp_path) is None


# ---- a function runs only when it is called -------------------------------------------------


def test_sourcing_a_library_does_not_run_its_functions(tmp_path):
    lib = 'build() { make; }\nrelease() { cd "$PKG"; npm publish; }\n'
    _repo(tmp_path, {"scripts/lib.sh": lib})
    assert evaluate("source scripts/lib.sh && build", tmp_path) is None
    assert evaluate("source scripts/lib.sh && build", tmp_path, strict=True) is None


def test_a_function_the_script_calls_is_read(leaky):
    script = f"#!/bin/sh\nship() {{\n  {PUBLISH}}}\nlint() {{ git push; }}\nship\n"
    _repo(leaky, {"release.sh": script})
    verdict = evaluate("./release.sh", leaky)
    assert verdict.block and "git push" not in verdict.reason
