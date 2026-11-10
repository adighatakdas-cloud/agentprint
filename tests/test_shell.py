"""The shell tokenizer and parser behind the command reader: text read the way the shell reads
it, nothing cut out but blanks, comments and line continuations."""

import pytest

from agentprint import shell
from agentprint.commands import Action, detect
from agentprint.shell import Compound, Function, Op, Redirect, Word


def words(text: str) -> list[str]:
    return [t.value for t in shell.lex(text).tokens if isinstance(t, Word)]


def heredocs(text: str) -> list[shell.Heredoc]:
    return [t.heredoc for t in shell.lex(text).tokens if isinstance(t, Redirect) and t.heredoc]


# ---- words, quotes, comments ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ('echo "a \\" # not a comment" # comment', ["echo", 'a " # not a comment']),
        ("echo a\\#b # comment", ["echo", "a#b"]),
        ("echo ${#list[@]} $# # count", ["echo", "${#list[@]}", "$#"]),
        ("echo 'multi\n# still quoted' # done", ["echo", "multi\n# still quoted"]),
        ("echo a#b", ["echo", "a#b"]),  # `#` inside a word is text
        ("n\"p\"m pub''lish", ["npm", "publish"]),
    ],
)
def test_comments_go_and_quoted_text_stays(text, want):
    assert words(text) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("$'publish'", "publish"),
        ("$'npm\\x20publish'", "npm publish"),
        ("$'it\\'s'", "it's"),
        ("$'\\u0070ush'", "push"),
        ("$'\\160ublish'", "publish"),
        ("$'a\\nb'", "a\nb"),
        ("\"$'x'\"", "$'x'"),  # inside "...", $' is not special
        ("'v1$'", "v1$"),  # a regex end in single quotes is plain text
    ],
)
def test_ansi_c_strings_are_decoded_never_dropped(text, want):
    assert words(text) == [want]


def test_apostrophes_inside_double_quotes_do_not_hide_a_substitution():
    [word] = [t for t in shell.lex("\"it's $(npm publish) that's\"").tokens if isinstance(t, Word)]
    [sub] = [p for p in word.parts if p.kind == "sub"]
    assert sub.text == "npm publish"


def test_only_a_newline_ends_a_comment():
    for char in ("\x0b", "\r", "\x0c", " ", " ", "\x85"):
        text = f"echo x # {char}cat <<EOF\nnpm publish\nEOF"
        assert words(text) == ["echo", "x", "npm", "publish", "EOF"], repr(char)
        assert heredocs(text) == []


@pytest.mark.parametrize(
    ("text", "regex"),
    [
        ("[[ x =~ ^(#|$) ]] || npm publish x", "^(#|$)"),
        ("[[ $l =~ ^(a b)$ ]] && npm publish x", "^(a b)$"),
        ("[[ x =~ (a|b) ]]&& npm publish x", "(a|b)"),
    ],
)
def test_the_regex_after_a_match_operator_is_one_word(tmp_path, text, regex):
    """bash keeps `(`, `|` and what is inside the parentheses (`#`, blanks) in the regex: a `#`
    there is no comment, so the command after the test is still read."""
    assert regex in words(text)
    assert [(r.action, r.tool) for r in detect(text, tmp_path)] == [(Action.publish, "npm")]


def test_a_regex_in_a_loop_that_reads_a_file_hides_nothing_after_it(tmp_path):
    text = "while read -r l; do [[ $l =~ ^(#|$) ]] && continue; done < .env; npm publish x"
    assert [(r.action, r.tool) for r in detect(text, tmp_path)] == [(Action.publish, "npm")]


# ---- here-documents ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("opener", "quoted"),
    [("<<EOF", False), ("<<'EOF'", True), ('<<"EOF"', True), ('<<E"O"F', True),
     ("<<E\\OF", True), ("<<\\EOF", True), ("<< EOF", False)],
)  # fmt: skip
def test_here_document_end_words_are_matched_after_quote_removal(opener, quoted):
    [doc] = heredocs(f"cat {opener}\nhello\nEOF\nnpm publish")
    assert (doc.delimiter, doc.quoted, doc.body, doc.closed) == ("EOF", quoted, "hello\n", True)
    assert words(f"cat {opener}\nhello\nEOF\nnpm publish")[-2:] == ["npm", "publish"]


def test_here_documents_with_tabs_several_on_a_line_and_unclosed():
    [doc] = heredocs("cat <<-EOF\n\thello\n\tEOF\nnpm publish")
    assert doc.strip and doc.body == "\thello\n"
    a, b = heredocs("cat <<A <<B\na\nA\nb\nB\nnpm publish")
    assert (a.body, b.body) == ("a\n", "b\n")
    [doc] = heredocs("cat <<EOF\nnpm publish")
    assert (doc.body, doc.closed) == ("npm publish", False)  # runs to the end, as bash reads it


def test_here_document_bodies_are_kept_whole():
    text = "\n".join(
        [
            "cat <<'A' > data.txt",
            "npm publish",
            "A",
            "bash <<B",
            "git push # inside a shell heredoc",
            "B",
            "echo $((1 << 4)) <<< word",
        ]
    )
    a, b = heredocs(text)
    assert a.body == "npm publish\n" and b.body == "git push # inside a shell heredoc\n"
    assert words(text)[-2:] == ["echo", "$((1 << 4))"]
    [here] = [t for t in shell.lex(text).tokens if isinstance(t, Redirect) and t.op == "<<<"]
    assert here.target is not None and here.target.value == "word"


@pytest.mark.parametrize(
    "text", ["echo $((1 << 2))", "echo $[1 << 2]", "((x = 1 << 2))", "x=$((1 <<EOF))"]
)
def test_a_shift_in_arithmetic_is_not_a_here_document(text):
    assert heredocs(text) == []


def test_a_continued_line_carries_the_here_document_on():
    text = "cat <<'EOF' \\\n  | bash\nnpm publish\nEOF"
    [doc] = heredocs(text)
    assert doc.body == "npm publish\n"
    ops = [t.op for t in shell.lex(text).tokens if isinstance(t, Op)]
    assert ops[0] == "|"  # the pipe is on the same command line


# ---- substitutions ----------------------------------------------------------------------------


def test_substitutions_nest_and_case_patterns_do_not_close_them():
    [word] = [t for t in shell.lex("x=$(case $a in b) echo $(date);; esac)").tokens]
    assert isinstance(word, Word)
    [sub] = [p for p in word.parts if p.kind == "sub"]
    assert sub.text == "case $a in b) echo $(date);; esac"


def test_an_unclosed_quote_hides_only_its_own_line():
    lexed = shell.lex('echo "unclosed\nnpm publish\n')
    assert lexed.broken == 0
    assert words('echo "unclosed\nnpm publish\n') == ["echo", "unclosed", "npm", "publish"]


def test_deep_nesting_stays_text_and_linear():
    text = "$(" * 5000 + "npm publish" + ")" * 5000
    assert "npm" in " ".join(words(text))


# ---- the parser -------------------------------------------------------------------------------


def _all_words(nodes):
    return [w.value for w in shell.walk_words(nodes)]


def test_every_word_is_in_the_tree():
    text = "if a; then b; elif c; then d; else e; fi; while f; do g; done; case h in i) j;; esac"
    text += "; { k; }; (l); m() { n; }; function o { p; }; for q in r s; do t; done; fi u"
    nodes = shell.parse(shell.lex(text).tokens)
    got = _all_words(nodes)
    for name in "abcdefghijklmnopqrstu":
        assert name in got, name


def test_functions_end_where_their_braces_match():
    text = "  helper() {\n    echo hi\n}\nnpm publish\n"
    nodes = shell.parse(shell.lex(text).tokens)
    assert isinstance(nodes[0].pipelines[0].commands[0], Function)
    assert _all_words(nodes[1:]) == ["npm", "publish"]
    text = "helper() {\n  echo a\n  }\nnpm publish\n}\n"  # the `}` indented deeper ends it
    nodes = shell.parse(shell.lex(text).tokens)
    assert _all_words(nodes[1:2]) == ["npm", "publish"]
    [unclosed] = [
        c
        for n in shell.parse(shell.lex("f() { npm publish\n").tokens)
        for c in n.pipelines[0].commands
    ]
    assert isinstance(unclosed, Function) and isinstance(unclosed.body, Compound)
    assert not unclosed.body.closed


# ---- functions run only when called (was recipes.called_only) ---------------------------------


@pytest.mark.parametrize(
    ("script", "want"),
    [
        ('build() { make; }\nrelease() {\n  cd "$PKG"\n  npm publish\n}\n', []),
        ('build() { make; }\nrelease() {\n  cd "$PKG"\n  npm publish\n}\nrelease\n', ["npm"]),
        ("pub() { npm publish; }\nship() {\n  pub\n}\nship\n", ["npm"]),  # ship calls pub
        ("f() { npm publish; }; f", ["npm"]),
        ("function deploy {\n  git push\n}\nfunction other() { echo; }\ndeploy\n", ["git"]),
        ("f() { npm publish\n", ["npm"]),  # no closing brace: never taken as unused
    ],
)
def test_only_called_functions_run(tmp_path, script, want):
    (tmp_path / "x.sh").write_text(script)
    assert [r.tool for r in detect("bash x.sh", tmp_path)] == want


def test_a_function_called_later_runs_in_the_folder_it_is_called_in(tmp_path):
    (tmp_path / "pkg").mkdir()
    found = detect("pub() { npm publish; }; cd pkg; pub", tmp_path)
    assert [(r.action, r.cwd) for r in found] == [(Action.publish, tmp_path / "pkg")]
