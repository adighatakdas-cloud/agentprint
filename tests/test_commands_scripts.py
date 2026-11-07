"""Publishes hidden in the files a command runs, and the word-scan backstop that reads them.

Each test builds a small fake repo: a shell script, a Makefile, a justfile, a Taskfile or a
release script in another language, and checks what `detect` makes of the command that runs it.
"""

import json
import os
from pathlib import Path

import pytest

from agentprint import commands
from agentprint.commands import Action, detect
from tests import timing

CWD = Path("/work/app")
N = 64 * 1024  # the most of any one file the guard reads
P, G = Action.publish, Action.push


def write(root: Path, rel: str, text: str, mode: int = 0o644) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def found(command, cwd):
    return [(r.action, r.tool, r.cwd, r.unsure) for r in detect(command, cwd)]


def tools(command, cwd):
    return [(r.action, r.tool) for r in detect(command, cwd)]


def unsure(verb: str, name: str) -> str:
    return f"{verb} from inside {name}; could not check"


# ---- the backstop word scan -----------------------------------------------------------------


def _backstop(text):
    return [(r.action, r.tool) for r in commands._backstop(text, CWD)]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("A=1 B=2 npm publish", [(Action.publish, "npm")]),
        ("TAG=$(git-describe)-rc npm publish", [(Action.publish, "npm")]),
        ("T=${X}x git push", [(Action.push, "git")]),
        ("\n\n  \n\t  npm publish", [(Action.publish, "npm")]),  # after a run of blank lines
        ("(cd x)/npm publish", [(Action.publish, "npm")]),
        ("do  twine upload dist/*", [(Action.publish, "twine")]),
        ("a=b;c npm publish", []),  # `c` is the program here, not npm
        ("D=./bin/twine upload", []),  # an assignment, then a program called `upload`
    ],
)
def test_backstop_reads_command_positions(text, want):
    assert _backstop(text) == want


@pytest.mark.parametrize(
    "text",
    [
        ";a=" * (N // 3),  # was quadratic: each `;` rescanned the rest of the word
        "\n" * N,  # was quadratic: each newline rescanned the whole blank run
        "\n " * (N // 2),
        "A=$(x)" * (N // 6),
        ";A=$(x)" * (N // 7),
        ("A=$(x)" + "a" * 50) * (N // 56),
        "do " * (N // 3),
        "git" + " -a b" * (N // 5),
        "npm" + " -a" * (N // 3),
        "{x}/" * (N // 4),
    ],
    ids=["semicolon-assign", "newlines", "newline-space", "subst", "semicolon-subst",
         "subst-word", "keywords", "git-options", "npm-options", "braces"],
)  # fmt: skip
def test_backstop_is_linear_on_a_64kb_script(text):
    assert timing.best_of(lambda: commands._backstop(text, CWD)) < timing.bound(0.5)


# ---- (1) shell scripts ----------------------------------------------------------------------

RELEASE_SH = """\
#!/usr/bin/env bash
set -euo pipefail
# npm publish   <- only a comment
npm run build
cd packages/a && npm publish --access public
git push --follow-tags
"""


@pytest.mark.parametrize(
    "command",
    [
        "./release.sh",
        "bash release.sh",
        "sh ./release.sh",
        "zsh -e release.sh",
        "bash -o pipefail release.sh",
        "bash -- release.sh",
        "source release.sh",
        ". ./release.sh",
        "env CI=1 ./release.sh",
        "time ./release.sh patch",
        "npm test && ./release.sh",
    ],
)
def test_shell_script_is_read_as_commands(tmp_path, command):
    write(tmp_path, "release.sh", RELEASE_SH, 0o755)
    a = tmp_path / "packages" / "a"  # a `cd` in a script lasts to its end
    assert found(command, tmp_path) == [(P, "npm", a, ""), (G, "git", a, "")]


@pytest.mark.parametrize(
    "text",
    ["#!/bin/sh\nnpm publish\n", "#!/usr/bin/env -S bash -e\nnpm publish\n", "npm publish\n"],
)
def test_extensionless_script_with_a_shell_shebang_or_none(tmp_path, text):
    write(tmp_path, "scripts/ship", text, 0o755)
    assert found("scripts/ship", tmp_path) == [(P, "npm", tmp_path, "")]


def test_script_runs_in_the_callers_folder(tmp_path):
    write(tmp_path, "tools/release.sh", "npm publish\n", 0o755)
    (tmp_path / "pkg").mkdir()
    assert found("cd pkg && ../tools/release.sh", tmp_path) == [(P, "npm", tmp_path / "pkg", "")]


def test_package_script_that_runs_a_shell_script(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"release": "./scripts/release.sh"}}))
    write(tmp_path, "scripts/release.sh", "#!/bin/sh\nnpm publish\n", 0o755)
    assert found("npm run release", tmp_path) == [(P, "npm", tmp_path, "")]


@pytest.mark.parametrize(
    "text",
    [
        "#!/bin/sh\n# npm publish\n  # git push\necho done\n",
        "echo 'npm publish'\necho \"git push\"\n",
        "cat > .github/workflows/release.yml <<'EOF'\n      - run: npm publish\nEOF\necho ok\n",
        "cat <<-EOF > notes.md\n\tgit push\n\tEOF\n",
        "tee README.md << EOF\nRun npm publish when ready.\nEOF\n",
        "grep -q x <<< 'npm publish'\n",
    ],
    ids=["comments", "quoted", "heredoc-to-file", "heredoc-dash", "heredoc-tee", "here-string"],
)
def test_shell_text_that_only_mentions_a_publish_is_not_one(tmp_path, text):
    write(tmp_path, "x.sh", text)
    assert found("bash x.sh", tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        "bash <<EOF\nnpm publish\nEOF\n",
        "ssh build-host <<'EOF'\n# remote\nnpm publish\nEOF\n",
        "x=$((1 << 2))\nnpm publish\n",
        'grep -q x <<< "$v"\nnpm publish\n',
        "echo hi # npm test\nnpm publish\n",
        "release() {\n  npm publish\n}\nrelease\n",
        "if [ -n \"$CI\" ]; then\n  npm publish\nfi\n",
    ],
    ids=["heredoc-to-bash", "heredoc-to-ssh", "arithmetic", "here-string", "comment", "function",
         "if"],
)  # fmt: skip
def test_shell_text_still_finds_real_publishes(tmp_path, text):
    write(tmp_path, "x.sh", text)
    assert tools("bash x.sh", tmp_path) == [(P, "npm")]


def test_here_document_fed_to_python_is_could_not_check(tmp_path):
    text = "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['npm', 'publish'])\nEOF\n"
    write(tmp_path, "x.sh", text)
    assert found("bash x.sh", tmp_path) == [(P, "npm", tmp_path, unsure("publishes", "x.sh"))]


# ---- (2) make -------------------------------------------------------------------------------

MAKEFILE = """\
NPM ?= npm
Q ?= @
.PHONY: all build test release tag

all: build

build:
\t$(Q)echo building

test:
\tnpm test

release: build test  # prerequisites run first
\t-$(NPM) publish --access public
\t$(MAKE) tag

tag:
\t@git push --tags
"""


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("make release", [(P, "npm"), (G, "git")]),
        ("gmake release", [(P, "npm"), (G, "git")]),
        ("make -j4 release", [(P, "npm"), (G, "git")]),
        ("make -j 4 release", [(P, "npm"), (G, "git")]),
        ("make --jobs=2 -s -k release", [(P, "npm"), (G, "git")]),
        ("make --jobs 2 release", [(P, "npm"), (G, "git")]),
        ("make release NPM=pnpm", [(P, "pnpm"), (G, "git")]),
        ("make tag", [(G, "git")]),
        ("make", []),  # the default goal: all -> build
        ("make build test", []),
        ("make -n release", []),
        ("make --dry-run release", []),
        ("make -q release", []),
        ("make -nk release", []),
        ("make --version", []),
        ("make no-such-target", []),
    ],
)
def test_make_targets(tmp_path, command, want):
    write(tmp_path, "Makefile", MAKEFILE)
    assert tools(command, tmp_path) == want


def test_make_reports_the_folder_and_checks_for_real(tmp_path):
    write(tmp_path, "Makefile", MAKEFILE)
    assert found("make release", tmp_path) == [(P, "npm", tmp_path, ""), (G, "git", tmp_path, "")]


@pytest.mark.parametrize(
    "command",
    ["make -C pkg release", "make -Cpkg release", "make --directory=pkg release",
     "make -f release.mk ship", "make --file release.mk ship"],
)  # fmt: skip
def test_make_options_for_folder_and_file(tmp_path, command):
    write(tmp_path, "pkg/Makefile", "release:\n\tnpm publish\n")
    write(tmp_path, "release.mk", "ship:\n\tcd pkg && npm publish\n")
    assert found(command, tmp_path) == [(P, "npm", tmp_path / "pkg", "")]


def test_make_follows_prerequisites_all_the_way_down(tmp_path):
    write(
        tmp_path,
        "Makefile",
        "ship: publish-all\npublish-all: publish-npm\npublish-npm:\n\tnpm publish\n",
    )
    assert tools("make ship", tmp_path) == [(P, "npm")]


def test_each_recipe_line_runs_in_its_own_shell_unless_oneshell(tmp_path):
    write(tmp_path, "Makefile", "release:\n\tcd pkg\n\tnpm publish\n")
    assert found("make release", tmp_path) == [(P, "npm", tmp_path, "")]
    write(tmp_path, "Makefile", ".ONESHELL:\nrelease:\n\tcd pkg\n\tnpm publish\n")
    assert found("make release", tmp_path) == [(P, "npm", tmp_path / "pkg", "")]


def test_makefile_syntax_that_is_not_a_recipe(tmp_path):
    makefile = """\
define HELP
\tnpm publish
endef
Q = @
release: VERSION = 2.0
release: \\
    build
ifeq ($(CI),true)
\tgit push
else
\t@echo "not on CI"
endif
\t$(Q)twine upload dist/*
build: ; @echo inline
"""
    write(tmp_path, "Makefile", makefile)
    assert tools("make release", tmp_path) == [(G, "git"), (P, "twine")]
    assert tools("make build", tmp_path) == []


def test_make_reads_included_makefiles_inside_the_folder(tmp_path):
    write(tmp_path, "Makefile", "include mk/*.mk\n-include missing.mk\nall:\n\techo\n")
    write(tmp_path, "mk/release.mk", "release:\n\tnpm publish\n")
    assert tools("make release", tmp_path) == [(P, "npm")]


def test_make_target_from_an_include_it_cannot_read_is_could_not_check(tmp_path):
    write(tmp_path, "Makefile", "include $(shell ls *.mk)\nall:\n\techo\n")
    [risky] = detect("make release", tmp_path)
    assert risky.unsure == "`make release` may run a recipe from a makefile agentprint did not read"
    assert detect("make all", tmp_path) == []


# ---- (3) just -------------------------------------------------------------------------------

JUSTFILE = """\
set shell := ["bash", "-uc"]
npm := "npm"

alias r := release

default: build

build:
    echo building

# the release
release: build && tag
    @{{npm}} publish --access public

tag:
    -git push --tags

[no-cd]
here:
    npm publish

[private]
notes:
    #!/usr/bin/env python3
    import subprocess
    subprocess.run(["twine", "upload", "dist/*"])

shebang:
    #!/usr/bin/env bash
    set -e
    cd pkg
    npm publish

[script("python3")]
scripted:
    import os
    os.system("git push")
"""


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("just release", [(P, "npm", ".", ""), (G, "git", ".", "")]),
        ("just r", [(P, "npm", ".", ""), (G, "git", ".", "")]),
        ("just -f justfile release", [(P, "npm", ".", ""), (G, "git", ".", "")]),
        ("just shebang", [(P, "npm", "pkg", "")]),
        ("just here", [(P, "npm", ".", "")]),
        ("just notes", [(P, "twine", ".", unsure("publishes", "justfile"))]),
        ("just scripted", [(G, "git", ".", unsure("pushes", "justfile"))]),
        ("just --command npm publish", [(P, "npm", ".", "")]),
        ("just", []),  # the first recipe: default -> build
        ("just build", []),
        ("just --list", []),
        ("just -n release", []),
        ("just --dry-run release", []),
        ("just --show release", []),
        ("just nope", []),
    ],
)
def test_just_recipes(tmp_path, command, want):
    write(tmp_path, "justfile", JUSTFILE)
    assert found(command, tmp_path) == [(a, t, tmp_path / rel, u) for a, t, rel, u in want]


def test_just_runs_recipes_in_the_justfiles_folder_unless_no_cd(tmp_path):
    write(tmp_path, "justfile", JUSTFILE)
    (tmp_path / "sub").mkdir()
    [(_, _, cwd, _), _] = found("cd sub && just release", tmp_path)
    assert cwd.resolve() == tmp_path.resolve()  # just looked upward and runs in its own folder
    assert found("cd sub && just here", tmp_path) == [(P, "npm", tmp_path / "sub", "")]


def test_just_imports_are_read_and_modules_are_could_not_check(tmp_path):
    write(tmp_path, "justfile", "import 'just/release.just'\nmod docs\nbuild:\n    echo\n")
    write(tmp_path, "just/release.just", "release:\n    npm publish\n")
    assert tools("just release", tmp_path) == [(P, "npm")]
    assert tools("just build", tmp_path) == []
    [risky] = detect("just docs::build", tmp_path)
    assert risky.unsure.startswith("`just docs::build` may run a recipe from a just module")


# ---- (4) npm version ------------------------------------------------------------------------

VERSION_SCRIPTS = {
    "preversion": "npm test",
    "version": "git add -A",
    "postversion": "git push --follow-tags && npm publish",
}


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("npm version patch", [(G, "git"), (P, "npm")]),
        ("npm version 1.2.3 -m 'v%s'", [(G, "git"), (P, "npm")]),
        ("pnpm version minor", [(G, "git"), (P, "npm")]),
        ("yarn version", [(G, "git"), (P, "npm")]),
        ("yarn version --new-version 1.2.3", [(G, "git"), (P, "npm")]),
        ("npm version", []),  # only prints the versions
        ("npm version patch --ignore-scripts", []),
    ],
)
def test_npm_version_runs_its_lifecycle_scripts(tmp_path, command, want):
    write(tmp_path, "package.json", json.dumps({"name": "x", "scripts": VERSION_SCRIPTS}))
    assert tools(command, tmp_path) == want


# ---- (5) scripts in other languages: "could not check" --------------------------------------

RELEASE_PY = """\
#!/usr/bin/env python3
import subprocess

# Build first, then upload.
subprocess.run(["twine", "upload", "dist/*"], check=True)
"""


@pytest.mark.parametrize(
    "command",
    [
        "python scripts/release.py",
        "python3 -u scripts/release.py --dry",
        "python3.12 -W ignore scripts/release.py",
        "uv run python scripts/release.py",
        "uv run scripts/release.py",
        "uv run --with build python scripts/release.py",
        "poetry run python scripts/release.py",
        "./scripts/release.py",
    ],
)
def test_python_release_script_is_could_not_check(tmp_path, command):
    write(tmp_path, "scripts/release.py", RELEASE_PY, 0o755)
    want = [(P, "twine", tmp_path, unsure("publishes", "scripts/release.py"))]
    assert found(command, tmp_path) == want


RELEASE_JS = """\
import { execSync } from "node:child_process";
import simpleGit from "simple-git";
const git = simpleGit();
const tags = [];
tags.push("v1");
execSync("npm publish --provenance", { stdio: "inherit" });
await git.push();
"""


@pytest.mark.parametrize(
    "command",
    [
        "node scripts/release.mjs",
        "node --experimental-vm-modules scripts/release.mjs",
        "npx tsx scripts/release.mjs",
        "ts-node -P tsconfig.json scripts/release.mjs",
        "bun scripts/release.mjs",
        "bun run scripts/release.mjs",
        "deno run -A scripts/release.mjs",
    ],
)
def test_js_release_script_is_could_not_check(tmp_path, command):
    write(tmp_path, "scripts/release.mjs", RELEASE_JS)
    name = "scripts/release.mjs"
    assert found(command, tmp_path) == [
        (P, "npm", tmp_path, unsure("publishes", name)),
        (G, "git", tmp_path, unsure("pushes", name)),
    ]


@pytest.mark.parametrize(
    ("command", "flag"),
    [
        ("python -c \"import os; os.system('npm publish')\"", "python -c"),
        ("node -e \"require('child_process').execSync('npm publish')\"", "node -e"),
        ("node --eval=\"require('child_process').execSync('npm publish')\"", "node --eval"),
        ("ruby -e 'system(\"npm publish\")'", "ruby -e"),
    ],
)
def test_inline_code_naming_a_publish_is_could_not_check(command, flag):
    assert found(command, CWD) == [(P, "npm", CWD, unsure("publishes", f"the {flag} code"))]


def test_python_dash_m_after_options_is_read_as_a_command():
    assert found("python -u -m twine upload dist/*", CWD) == [(P, "twine", CWD, "")]


TASKFILE = """\
version: '3'
tasks:
  default:
    cmds: [task: build]
  build:
    cmds: [go build ./...]
  test:
    deps: [build]
    cmds: [go test ./...]
  release:
    deps: [test]
    cmds:
      - task: publish
  publish:
    cmds:
      - npm publish --access public
      - git push --tags
"""


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("task release", [(P, "npm"), (G, "git")]),
        ("task publish", [(P, "npm"), (G, "git")]),
        ("cd sub && task release", [(P, "npm"), (G, "git")]),  # task looks upward
        ("task", []),  # default -> build
        ("task test", []),
        ("task build test", []),
        ("task nope", []),  # task itself says there is no such task
        ("task --list", []),
        ("task -n release", []),
    ],
)
def test_taskfile_tasks_that_publish_are_could_not_check(tmp_path, command, want):
    write(tmp_path, "Taskfile.yml", TASKFILE)
    (tmp_path / "sub").mkdir()
    got = detect(command, tmp_path)
    assert [(r.action, r.tool) for r in got] == want
    assert all(r.unsure.endswith("from inside Taskfile.yml; could not check") for r in got)


def test_taskfile_with_includes_is_read_whole(tmp_path):
    write(
        tmp_path,
        "Taskfile.yml",
        "version: '3'\nincludes:\n  ci: ./ci\ntasks:\n  x:\n    cmds: [npm publish]\n",
    )
    assert tools("task ci:release", tmp_path) == [(P, "npm")]


@pytest.mark.parametrize(
    "text",
    [
        "const items = [];\nitems.push(1);\nconsole.log(items);\n",
        "import numpy as np\nprint(np.zeros(3))\n",
        "# publish the docs later\nprint('done')\n",
        "hooks = '.git/hooks/pre-push'\n",
        "subprocess.run(['npm', 'run', 'publish:docs'])\n",
        "subprocess.run(['./publish.sh'])\n",
        "npmrc = '.npmrc'\npublished = True\n",
        "log(['git', err.message]); summary.push(line)\n",
    ],
    ids=["js-push", "numpy", "publish-comment", "pre-push-hook", "publish-script-name",
         "publish-path", "lookalikes", "method-near-git"],
)  # fmt: skip
def test_ordinary_code_is_not_a_publish(tmp_path, text):
    write(tmp_path, "tool.py", text)
    assert detect("node tool.py", tmp_path) == []
    assert detect("python tool.py", tmp_path) == []


# ---- limits ---------------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="symlinks and POSIX paths in a shell command")
def test_files_outside_the_folder_are_never_read(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    write(tmp_path, "outside.sh", "npm publish\n", 0o755)
    write(tmp_path, "release.py", "run(['npm', 'publish'])\n")
    write(tmp_path, "Makefile", "release:\n\tnpm publish\n")
    write(tmp_path, "justfile", "release:\n    npm publish\n")
    write(tmp_path, "Taskfile.yml", "tasks:\n  release:\n    cmds: [npm publish]\n")
    (project / "link.sh").symlink_to(tmp_path / "outside.sh")
    reads = []
    load = commands._load
    monkeypatch.setattr(
        commands, "_load", lambda path, name: reads.append(path) or load(path, name)
    )
    for command in [
        "../outside.sh",
        "./link.sh",
        f"{tmp_path}/outside.sh",
        "bash ../outside.sh",
        "cd .. && ./outside.sh",
        "python ../release.py",
        "make -C .. release",
        "make -f ../Makefile release",
        "just -f ../justfile release",
        "just release",  # the justfile above the folder is not even looked for
        "task release",
    ]:
        assert detect(command, project) == [], command
    assert reads == []


def test_compiled_programs_are_not_followed(tmp_path):
    (tmp_path / "a.out").write_bytes(b"\x7fELF\x02\x01\x01\x00npm publish" + b"\0" * 64)
    (tmp_path / "a.out").chmod(0o755)
    assert detect("./a.out", tmp_path) == []


def test_a_script_too_large_to_read_is_could_not_check(tmp_path):
    write(tmp_path, "configure", "#!/bin/sh\n" + "# padding\n" * 7000, 0o755)  # 70 KB
    why = "configure is larger than 64 KB, so agentprint did not read it to see whether it "
    assert found("./configure", tmp_path) == [(P, "sh", tmp_path, why + "publishes or pushes")]
    write(tmp_path, "Makefile", "all:\n\techo\n" + "# padding\n" * 7000)
    [risky] = detect("make", tmp_path)
    assert (risky.tool, risky.unsure.split(" is ")[0]) == ("make", "Makefile")


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs binding permissions")
def test_an_unreadable_script_is_could_not_check(tmp_path):
    path = write(tmp_path, "release.sh", "npm publish\n", 0o000)
    try:
        why = "agentprint could not read release.sh to see whether it publishes or pushes"
        assert found("bash release.sh", tmp_path) == [(P, "bash", tmp_path, why)]
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize(
    "command",
    ["./missing.sh", "bash missing.sh", "make", "just", "task", "python missing.py",
     '"$DIR"/release.sh', "bash $SCRIPT", "bash", "bash -s < release.sh", "./"],
)  # fmt: skip
def test_nothing_to_read_is_not_a_publish(tmp_path, command):
    write(tmp_path, "release.sh", "npm publish\n", 0o755)
    assert detect(command, tmp_path) == []


def test_a_chain_deeper_than_agentprint_follows_is_could_not_check(tmp_path):
    for i in range(1, 8):
        write(tmp_path, f"s{i}.sh", f"echo step {i}\n./s{i + 1}.sh\n", 0o755)
    write(tmp_path, "s8.sh", "npm publish\n", 0o755)
    [risky] = detect("./s1.sh", tmp_path)  # s5.sh is as deep as it reads: never a silent OK
    stops = "s5.sh runs more scripts than agentprint follows, so it could not check them"
    assert risky.unsure == stops
    write(tmp_path, "s5.sh", "npm publish\n", 0o755)  # named right where it stops
    [risky] = detect("./s1.sh", tmp_path)
    assert (risky.action, risky.tool) == (P, "npm")
    deep = "s5.sh, deeper in its scripts than agentprint follows"
    assert risky.unsure == unsure("publishes", deep)


def test_a_script_that_runs_itself_stops(tmp_path):
    write(tmp_path, "loop.sh", "echo again\n./loop.sh\n", 0o755)
    assert [r.action for r in detect("./loop.sh", tmp_path)] == [P]  # could not check, no hang
    assert detect("./loop.sh", tmp_path)[0].unsure


def test_too_many_commands_fall_back_to_a_word_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "MAX_STEPS", 1)
    write(tmp_path, "Makefile", "release:\n\techo one\n\tnpm publish\n")
    [risky] = detect("make release", tmp_path)
    assert (risky.tool, "deeper in its scripts" in risky.unsure) == ("npm", True)


def test_each_command_line_starts_a_fresh_reading(tmp_path):
    write(tmp_path, "release.sh", "npm publish\n", 0o755)
    assert detect("./release.sh", tmp_path) == detect("./release.sh", tmp_path)
    assert commands._READING.get() is None


# ---- options and corners --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("deno eval \"await new Deno.Command('npm', {args: ['publish']}).output()\"", [(P, "npm")]),
        ("deno fmt scripts/release.mjs", []),
        ("node -- scripts/release.mjs", [(P, "npm")]),
        ("node -r dotenv/config scripts/release.mjs", [(P, "npm")]),
        ("node", []),
        ("python -", []),
        ("python -m", []),
    ],
)
def test_interpreter_options(tmp_path, command, want):
    write(tmp_path, "scripts/release.mjs", "execSync('npm publish')\n")
    assert tools(command, tmp_path) == want


@pytest.mark.parametrize(
    "command",
    ["make -C $PKG release", "make -C missing release", "make -C", "make -f - release"],
)
def test_make_folder_or_file_it_cannot_know_is_not_followed(tmp_path, command):
    write(tmp_path, "Makefile", "release:\n\tnpm publish\n")
    assert detect(command, tmp_path) == []


def test_make_include_named_by_a_variable_marks_the_makefile_incomplete(tmp_path):
    write(tmp_path, "Makefile", "include $(HOME)/x.mk\nall:\n\techo\n")
    [risky] = detect("make release", tmp_path)
    assert risky.tool == "make" and "did not read" in risky.unsure


@pytest.mark.parametrize(
    "command",
    ["just --set npm pnpm release", "just -d . -f justfile release",
     "just --justfile=justfile release", "just --color never release", "just NAME=x release"],
)  # fmt: skip
def test_just_options(tmp_path, command):
    write(tmp_path, "justfile", "release:\n    npm publish\n")
    assert tools(command, tmp_path) == [(P, "npm")]


def test_just_import_it_cannot_read_and_its_working_directory(tmp_path):
    write(tmp_path, "justfile", "import 'missing.just'\nimport? 'optional.just'\nb:\n    echo\n")
    [risky] = detect("just release", tmp_path)
    assert risky.unsure.endswith(
        "may run a recipe from a just module or import agentprint did not read"
    )
    write(tmp_path, "justfile", "set working-directory := env('DIR')\nrelease:\n    npm publish\n")
    [risky] = detect("just release", tmp_path)
    assert risky.unsure == "the folder its recipes run in is only known when just runs"


def test_just_or_taskfile_too_large_to_read_is_could_not_check(tmp_path):
    write(tmp_path, "justfile", "release:\n    npm publish\n" + "# pad\n" * 12000)
    [risky] = detect("just release", tmp_path)
    assert (risky.tool, "larger than 64 KB" in risky.unsure) == ("just", True)
    write(tmp_path, "justfile", "import 'big.just'\nrelease:\n    npm publish\n")
    write(tmp_path, "big.just", "x:\n    echo\n" + "# pad\n" * 12000)
    assert detect("just release", tmp_path)[0].tool == "just"
    write(tmp_path, "Taskfile.yml", "tasks: {}\n" + "# pad\n" * 12000)
    [risky] = detect("task release", tmp_path)
    assert (risky.tool, "larger than 64 KB" in risky.unsure) == ("task", True)


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("task -t ci/Taskfile.yml release", [(P, "npm")]),
        ("task --taskfile=ci/Taskfile.yml release", [(P, "npm")]),
        ("task -d ci release", [(P, "npm")]),
        ("task --dir=ci release -- --tag next", [(P, "npm")]),
        ("task -t ../Taskfile.yml release", []),  # outside the folder: not read
        ("task -C 2 -t ci/Taskfile.yml release", [(P, "npm")]),
    ],
)
def test_task_options(tmp_path, command, want):
    project = tmp_path / "project"
    write(project, "ci/Taskfile.yml", "tasks:\n  release:\n    cmds: [npm publish]\n")
    write(tmp_path, "Taskfile.yml", "tasks:\n  release:\n    cmds: [npm publish]\n")
    assert tools(command, project) == want


def test_a_folder_that_does_not_exist_has_nothing_to_read(tmp_path):
    for command in ("just release", "task release", "make release", "./release.sh"):
        assert detect(command, tmp_path / "missing") == [], command


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs binding permissions")
def test_a_folder_that_can_be_listed_but_not_entered_has_nothing_to_read(tmp_path):
    folder = tmp_path / "locked"
    write(folder, "justfile", "release:\n    npm publish\n")
    folder.chmod(0o444)  # names can be listed, files cannot be opened or even looked at
    try:
        assert detect("just release", folder) == []
    finally:
        folder.chmod(0o755)
