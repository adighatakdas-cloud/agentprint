"""The guard on publishes hidden in scripts: a real check when it can read the commands, and
"could not check" (a warning, or a block in strict mode) when it cannot."""

import json

import pytest

from agentprint.guard import STRICT_HINT, evaluate
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
    "agentprint could not check `scripts/release.py`: it runs `twine upload` from code "
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
    assert not verdict.block and "could not check `build.py`" in verdict.warning


def test_a_large_script_that_names_a_publish_warns_or_blocks_in_strict_mode(tmp_path):
    _script(tmp_path, "release.sh", "#!/bin/sh\n" + PAD + "npm publish\n")
    default = evaluate("./release.sh", tmp_path)
    assert not default.block
    assert default.warning == (
        "agentprint could not check `release.sh`: it is larger than 64 KB and names "
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


def test_could_not_check_names_the_file_and_the_next_step():
    from pathlib import Path

    from agentprint.commands import Action, Risky
    from agentprint.guard import _could_not
    from agentprint.models import CannotCheck

    item = Risky(
        Action.publish, "sh", Path("/x"), unsure="agentprint could not open it", label="ship.sh"
    )
    assert _could_not(item, CannotCheck(item.unsure)) == (
        "agentprint could not check `ship.sh`: agentprint could not open it. Run the publish or "
        "push as its own command, so agentprint can check it."
    )
    plain = Risky(Action.push, "git", Path("/x"), unsure="its folder is unknown")
    assert _could_not(plain, CannotCheck(plain.unsure)) == (
        "agentprint could not check this git push: its folder is unknown."
    )
