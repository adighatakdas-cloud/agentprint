"""The text readers behind script following: shell text, makefiles, justfiles and Taskfiles."""

import pytest

from agentprint import recipes
from agentprint.recipes import Step

# ---- shell text -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ('echo "a \\" # not a comment" # comment', 'echo "a \\" # not a comment"'),
        ("echo a\\#b # comment", "echo a\\#b"),
        ("echo ${#list[@]} $# # count", "echo ${#list[@]} $#"),
        ("echo 'multi\n# still quoted' # done", "echo 'multi\n# still quoted'"),
    ],
)
def test_comments_go_and_quoted_hashes_stay(text, kept):
    assert recipes.shell_text(text) == (kept, [])


def test_here_document_bodies_are_kept_dropped_or_handed_over():
    text = "\n".join(
        [
            "cat <<'A' > data.txt",
            "npm publish",
            "A",
            "bash <<B",
            "git push # inside a shell heredoc",
            "B",
            "node - <<C",
            "require('child_process').execSync('npm publish')",
            "C",
            "echo $((1 << 4)) <<< word",
        ]
    )
    kept, other = recipes.shell_text(text)
    assert "npm publish" not in kept.split("\n")[1:3]
    assert "git push" in kept and "inside a shell heredoc" not in kept
    assert other == ["require('child_process').execSync('npm publish')"]
    assert kept.endswith("echo $((1 << 4)) <<< word")


def test_unterminated_here_document_runs_to_the_end():
    assert recipes.shell_text("cat <<EOF\nnpm publish") == ("cat <<EOF", [])


@pytest.mark.parametrize(
    ("first_line", "name", "program"),
    [
        ("#!/bin/bash", "x", "bash"),
        ("#! /usr/bin/env -S bash -e", "x", "bash"),
        ("#!/usr/bin/env FOO=1 python3", "x", "python3"),
        ("#!/usr/bin/env", "x", "env"),
        ("echo hi", "release.py", "python3"),
        ("echo hi", "release.mjs", "node"),
        ("echo hi", "release", "sh"),
        ("echo hi", "release.SH", "sh"),
    ],
)
def test_interpreter_from_shebang_or_extension(first_line, name, program):
    assert recipes.interpreter(first_line + "\nrest\n", name) == program


# ---- makefiles ------------------------------------------------------------------------------


def _make(text, goals=(), overrides=None):
    return recipes.make_steps(recipes.parse_make(text), list(goals), overrides or {})


def test_make_variables_and_special_settings():
    text = """\
.RECIPEPREFIX = >
.DEFAULT_GOAL := ship
FLAGS = --access
FLAGS += public
TOOL != echo npm
SELF = $(SELF) x
export
ship:
> npm publish $(FLAGS) $$HOME $@ $(TOOL) $(SELF) $(subst a,b,c) ${FLAGS}
"""
    steps, missing = _make(text)
    assert missing == []
    assert steps == [
        "npm publish --access public $HOME $@ ${TOOL} ${SELF} x ${MAKE_VALUE} --access public"
    ]


def test_make_continuation_at_end_of_file_and_unbalanced_reference():
    steps, _ = _make("all:\n\techo $(oops \\")
    assert steps == ["echo $(oops"]


def test_make_expansion_that_grows_too_large_stays_unknown():
    text = "".join(f"V{i} = $(V{i + 1})$(V{i + 1})\n" for i in range(7)) + "V7 = " + "x" * 2000
    text += "\nall:\n\techo $(V0)\n"
    assert _make(text)[0] == ["echo ${MAKE_VALUE}${MAKE_VALUE}"]  # each half capped at 64 KB


def test_make_goal_with_no_rule_is_reported_missing():
    assert _make("all:\n\techo\n", ["nope"]) == ([], ["nope"])


# ---- justfiles ------------------------------------------------------------------------------


def test_just_settings_headers_and_values():
    text = """\
set working-directory := 'pkg'
set shell := ['zsh', '-c']
greeting := "hi"
dynamic := env_var('X')
bad header without colon
!not a recipe either
release name='a:b' $flag="x": (build "arg") && after
    echo {{greeting}} {{dynamic}} {{name}} {{{{literal}}
build arg:
    echo build {{arg}} \\
        continued
after:
    echo after
"""
    jf = recipes.parse_just(text)
    assert jf.workdir == "pkg" and jf.shell == "zsh"
    steps, missing = recipes.just_steps(jf, ["release"])
    assert missing == []
    assert [s.text for s in steps] == [
        "echo build ${JUST_VALUE} continued",
        "echo hi ${JUST_VALUE} ${JUST_VALUE} {{literal}}",
        "echo after",
    ]
    assert {s.program for s in steps} == {"zsh"}


def test_just_working_directory_from_an_expression_is_unknown():
    assert recipes.parse_just("set working-directory := env('D')\n").workdir == "${JUST_VALUE}"


def test_just_script_attribute_without_an_interpreter_is_shell():
    jf = recipes.parse_just("[script]\nrelease:\n    npm publish\n")
    assert recipes.just_steps(jf, ["release"])[0] == [Step("npm publish", "sh")]


# ---- Taskfiles ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "x: &anchor\n  a: 1\ntasks:\n  t: *anchor\n",
        "tasks: " + "[" * 40 + "]" * 40,
        "tasks:\n  t: [unclosed\n",
        "- just\n- a list\n",
    ],
    ids=["anchors", "deep", "not-yaml", "no-tasks"],
)
def test_taskfile_read_whole_when_it_cannot_be_narrowed(text):
    assert recipes.taskfile_text(text, ["t"]) == text


def test_taskfile_with_aliases_or_wildcards_is_read_whole_for_unknown_names():
    text = "tasks:\n  release:\n    aliases: [r]\n    cmds: [npm publish]\n"
    assert recipes.taskfile_text(text, ["r"]) == text
    text = "tasks:\n  'deploy-*':\n    cmds: [git push]\n"
    assert recipes.taskfile_text(text, ["deploy-x"]) == text


def test_make_targets_named_by_a_variable_are_filled_in():
    text = "TARGETS = release ship\n$(TARGETS):\n\tnpm publish\n"
    assert _make(text, ["ship"]) == (["npm publish"], [])


def test_just_attribute_with_a_blank_argument_does_not_crash():
    jf = recipes.parse_just('[script(" ")]\n[confirm("")]\nrelease:\n    npm publish\n')
    assert recipes.just_steps(jf, ["release"])[0] == [Step("npm publish", "sh")]


def test_only_called_functions_keep_their_bodies():
    lib = 'build() { make; }\nrelease() {\n  cd "$PKG"\n  npm publish\n}\n'
    assert "npm publish" not in recipes.called_only(lib) and "make" not in recipes.called_only(lib)
    assert recipes.called_only(lib).count("\n") == lib.count("\n")  # line numbers stay put
    kept = recipes.called_only(lib + "release\n")
    assert "npm publish" in kept and "make" not in kept  # release is called, build is not
    chain = "pub() { npm publish; }\nship() {\n  pub\n}\nship\n"
    assert recipes.called_only(chain) == chain  # ship calls pub
    one_line = "f() { npm publish; }; f"
    assert recipes.called_only(one_line) == one_line
    keyword = "function deploy {\n  git push\n}\nfunction other() { echo; }\ndeploy\n"
    assert "git push" in recipes.called_only(keyword) and "echo" not in recipes.called_only(keyword)
    broken = "f() { npm publish\n"
    assert recipes.called_only(broken) == broken  # no closing brace: left alone


def test_a_define_block_is_a_variable_a_recipe_can_use():
    text = "define PUB\nnpm publish\ngit push\nendef\nrelease:\n\t$(PUB)\n"
    assert _make(text, ["release"]) == (["npm publish\ngit push"], [])


def test_evidence_follows_the_variables_a_recipe_line_names():
    mf = recipes.parse_make(
        "pub = $(1) publish\nx = $(y)\ny = push\nrelease:\n\t$(call pub,npm) $(x)\n"
    )
    plan = recipes.make_plan(mf, ["release"], {})
    [raw] = plan.raw
    assert "publish" in plan.evidence(raw) and "push" in plan.evidence(raw)
