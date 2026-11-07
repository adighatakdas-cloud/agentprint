"""Read the files a command hands its work to: shell scripts, Makefiles, justfiles, Taskfiles.

Pure text functions with no file access and nothing run: `commands` reads the files (inside the
folder the agent's command runs in, 64 KB at most) and sends what these return back through its
own command reader.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

# ---- shell scripts --------------------------------------------------------------------------

_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.@+-]+))")
_HEREDOC_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh"}  # their stdin is commands
_INTERPRETER = re.compile(
    r"^(?:python[0-9.]*|pypy[0-9.]*|node|nodejs|bun|deno|tsx|ts-node|ruby|perl|php|lua|pwsh"
    r"|powershell)$"
)
_SHEBANG = re.compile(r"#!\s*(\S+)(.*)")
_BY_EXTENSION = {
    ".py": "python3",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".ts": "tsx",
    ".mts": "tsx",
    ".cts": "tsx",
    ".rb": "ruby",
    ".pl": "perl",
    ".php": "php",
    ".ps1": "pwsh",
    ".lua": "lua",
}


@dataclass(frozen=True)
class Step:
    """One thing a recipe hands to a program: a shell line, or a whole script."""

    text: str
    program: str = "sh"  # what runs it: a shell, or e.g. python3 for a shebang recipe
    here: bool = False  # just `[no-cd]`: runs in the caller's folder, not the justfile's


def interpreter(text: str, name: str) -> str:
    """The program that runs a script file: its shebang, else its extension, else `sh`.

    A text file with no shebang that is run as `./x` is run by the shell."""
    match = _SHEBANG.match(text.split("\n", 1)[0].strip())
    if match:
        program = PurePosixPath(match.group(1)).name
        if program == "env":  # #!/usr/bin/env -S bash -e
            words = [w for w in match.group(2).split() if not w.startswith("-") and "=" not in w]
            program = PurePosixPath(words[0]).name if words else program
        return program
    return _BY_EXTENSION.get(PurePosixPath(name).suffix.lower(), "sh")


def shell_text(text: str) -> tuple[str, list[str]]:
    """A shell script's commands, without its comments and here-document bodies.

    Also returns the bodies of here-documents fed to another language (`python3 - <<EOF`):
    code, but not shell. A body fed to a shell (`bash <<EOF`, `ssh host <<EOF`) stays in the
    commands; a body fed to anything else (`cat > ci.yml <<EOF`) is data and is dropped."""
    lines = text.splitlines()
    kept: list[str] = []
    other: list[str] = []
    quote = ""
    i = 0
    while i < len(lines):
        line, quote, heredocs = _scan(lines[i], quote)
        kept.append(line)
        i += 1
        fed_to = _fed_to(line) if heredocs else ""
        for strip_tabs, word in heredocs:
            body: list[str] = []
            while i < len(lines):
                end = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if end == word:
                    break
                body.append(lines[i - 1])
            if fed_to == "shell":
                inner, more = shell_text("\n".join(body))
                kept.append(inner)
                other += more
            elif fed_to == "other":
                other.append("\n".join(body))
    return "\n".join(kept), other


def _scan(line: str, quote: str) -> tuple[str, str, list[tuple[bool, str]]]:
    """One line without its comment; the quote still open after it; here-documents it opens."""
    out: list[str] = []
    heredocs: list[tuple[bool, str]] = []
    arithmetic = 0  # open `((`: a `<<` inside $(( 1 << 2 )) is a shift, not a here-document
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\" and quote == '"':
                out.append(line[i : i + 2])
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char == "\\":
            out.append(line[i : i + 2])
            i += 2
            continue
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1] in " \t;&|()"):
            break
        elif line.startswith("((", i) or line.startswith("))", i):
            arithmetic += 1 if char == "(" else -1
            out.append(line[i : i + 2])
            i += 2
            continue
        elif line.startswith("<<<", i):  # a here-string: its word is on this line
            out.append("<<<")
            i += 3
            continue
        elif line.startswith("<<", i) and arithmetic <= 0:
            match = _HEREDOC.match(line, i)
            if match:
                word = next(g for g in match.groups()[1:] if g is not None)
                heredocs.append((match.group(1) == "-", word))
                out.append(match.group(0))
                i = match.end()
                continue
        out.append(char)
        i += 1
    return "".join(out).rstrip(), quote, heredocs


def _fed_to(line: str) -> str:
    names = {PurePosixPath(w).name for w in re.findall(r"[^\s;&|()<>]+", line)}
    if names & _HEREDOC_SHELLS:
        return "shell"
    if any(_INTERPRETER.match(name) for name in names):
        return "other"
    return "data"


# ---- Makefiles ------------------------------------------------------------------------------

_MAKE_ASSIGN = re.compile(
    r"^(?:(?:override|export|private)\s+)*([^\s:#=]+)\s*(:::=|::=|:=|\?=|\+=|!=|=)\s*(.*)$"
)
_MAKE_INCLUDE = re.compile(r"^(-include|sinclude|include)\s+(.*)$")
_MAKE_CONDITIONAL = re.compile(r"^(?:ifeq|ifneq|ifdef|ifndef|else|endif)\b")
_MAKE_DEFINE = re.compile(r"^(?:(?:override|export)\s+)?define\b")
_RECIPE_PREFIX = re.compile(r"^[\s@+-]+")
_MAKE_NAME = re.compile(r"[A-Za-z_][\w.-]*")
_EXPANSION_DEPTH = 8
_MAX_EXPANSION = 64 * 1024


@dataclass
class Makefile:
    rules: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)  # prereqs, recipe
    variables: dict[str, str] = field(default_factory=dict)
    default: str = ""
    includes: list[tuple[str, bool]] = field(default_factory=list)  # (include line, optional)
    oneshell: bool = False


def parse_make(text: str, into: Makefile | None = None) -> Makefile:
    """Rules, simple variables and includes of one makefile, added to `into` when given."""
    mf = into or Makefile()
    prefix = "\t"
    current: list[str] | None = None  # the targets whose recipe the next tab line belongs to
    in_define = False
    for line in _logical_lines(text):
        if in_define:
            in_define = not re.match(r"^\s*endef\b", line)
            continue
        if line.startswith(prefix):
            for target in current or []:
                mf.rules[target][1].append(line[len(prefix) :])
            continue
        stripped = re.sub(r"(?<!\\)#.*", "", line).strip()
        if not stripped or _MAKE_CONDITIONAL.match(stripped):
            continue  # neither ends a recipe
        current = None
        if _MAKE_DEFINE.match(stripped):
            in_define = True
        elif assign := _MAKE_ASSIGN.match(stripped):
            name, op, value = assign.groups()
            if name == ".RECIPEPREFIX":
                prefix = value[:1] or "\t"
            elif name == ".DEFAULT_GOAL":
                mf.default = value.strip()
            elif op == "+=" and name in mf.variables:
                mf.variables[name] += " " + value
            elif op != "!=" and not (op == "?=" and name in mf.variables):
                mf.variables[name] = value
        elif include := _MAKE_INCLUDE.match(stripped):
            mf.includes.append((include.group(2), include.group(1) != "include"))
        else:
            current = _make_rule(stripped, line, mf)
    return mf


def _make_rule(stripped: str, line: str, mf: Makefile) -> list[str] | None:
    colon = stripped.find(":")
    if colon <= 0:
        return None
    targets = expand_make(stripped[:colon], mf.variables).split()  # `$(TARGETS): ...`
    rest = stripped[colon + 1 :].removeprefix(":")
    if _MAKE_ASSIGN.match(rest.strip()):  # target-specific variable: `release: V = 1`
        return None
    raw_rest = line[line.find(":") + 1 :].removeprefix(":")
    prereqs, _, inline = raw_rest.partition(";")
    prereqs = re.sub(r"(?<!\\)#.*", "", prereqs).replace("|", " ")
    for target in targets:
        entry = mf.rules.setdefault(target, ([], []))
        entry[0].extend(prereqs.split())
        if inline.strip():
            entry[1].append(inline.strip())
        if target == ".ONESHELL":
            mf.oneshell = True
        if not mf.default and not target.startswith(".") and "%" not in target:
            mf.default = target
    return targets


def _logical_lines(text: str) -> list[str]:
    """Lines with backslash-newline continuations joined."""
    out: list[str] = []
    parts: list[str] = []
    for raw in text.splitlines():
        line = raw.lstrip() if parts else raw
        if (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            parts.append(line[:-1].rstrip())
        else:
            out.append(" ".join([*parts, line]))
            parts = []
    if parts:
        out.append(" ".join(parts))
    return out


def make_steps(
    mf: Makefile, goals: list[str], overrides: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The shell commands `make <goals>` runs, prerequisites first, and the goals with no rule.

    Each recipe line runs in its own shell (one shell per recipe with `.ONESHELL:`). Variables
    set in the makefile are filled in; `$(MAKE)` becomes `make`; anything else only make knows
    becomes `${NAME}`, which the command reader treats as known only at run time."""
    variables = {**mf.variables, **overrides}
    memo: dict[str, str] = {}
    goals = goals or ([mf.default] if mf.default else [])
    order: list[str] = []
    seen: set[str] = set()
    stack = [(goal, False) for goal in reversed(goals)]
    while stack:
        target, ready = stack.pop()
        if ready:
            order.append(target)
            continue
        if target in seen or target not in mf.rules:
            continue
        seen.add(target)
        stack.append((target, True))
        prereqs = expand_make(" ".join(mf.rules[target][0]), variables, memo).split()
        stack += [(p, False) for p in reversed(prereqs)]
    steps: list[str] = []
    for target in order:
        recipe = [expand_make(line, variables, memo) for line in mf.rules[target][1]]
        lines = [_RECIPE_PREFIX.sub("", line) for line in recipe]
        lines = [line for line in lines if line.strip()]
        if mf.oneshell and lines:
            steps.append("\n".join(lines))
        else:
            steps += lines
    return steps, [goal for goal in goals if goal not in mf.rules]


def expand_make(
    text: str, variables: dict[str, str], memo: dict[str, str] | None = None, depth: int = 0
) -> str:
    """`text` with make variables filled in. Each variable is expanded once (`memo`); one that
    refers to itself, nests too deeply or grows past 64 KB is left as `${NAME}`."""
    memo = {} if memo is None else memo
    out: list[str] = []
    size = 0
    i = 0
    while i < len(text) and size <= _MAX_EXPANSION:
        char = text[i]
        if char != "$" or i + 1 >= len(text):
            piece, i = char, i + 1
        elif text[i + 1] == "$":
            piece, i = "$", i + 2
        elif text[i + 1] in "({":
            end = _closing(text, i + 1)
            if end < 0:
                piece, i = text[i:], len(text)
            else:
                piece, i = _make_value(text[i + 2 : end], variables, memo, depth), end + 1
        else:  # $@, $<, $^ and one-letter variables: only make knows
            piece, i = "$" + text[i + 1], i + 2
        out.append(piece)
        size += len(piece)
    if size > _MAX_EXPANSION:
        return "${MAKE_VALUE}"
    return "".join(out)


def _closing(text: str, start: int) -> int:
    opening = text[start]
    closing = ")" if opening == "(" else "}"
    level = 0
    for i in range(start, len(text)):
        if text[i] == opening:
            level += 1
        elif text[i] == closing:
            level -= 1
            if level == 0:
                return i
    return -1


def _make_value(inner: str, variables: dict[str, str], memo: dict[str, str], depth: int) -> str:
    name = inner.strip()
    if name in {"MAKE", "MAKE_COMMAND"}:
        return "make"
    if not _MAKE_NAME.fullmatch(name):
        return "${MAKE_VALUE}"  # $(shell ...), $(VAR:a=b) and other functions
    unknown = "${" + name + "}"
    if name not in variables or depth >= _EXPANSION_DEPTH:
        return unknown
    if name not in memo:
        memo[name] = unknown  # while it expands: a variable that refers to itself stays unknown
        memo[name] = expand_make(variables[name], variables, memo, depth + 1)
    return memo[name]


# ---- justfiles ------------------------------------------------------------------------------

_JUST_NAME = re.compile(r"@?([A-Za-z_][A-Za-z0-9_-]*)")
_JUST_ASSIGN = re.compile(r"^(?:export\s+)?([A-Za-z_][\w-]*)\s*:=\s*(.*)$")
_JUST_LITERAL = re.compile(r"""^(?:'([^']*)'|"([^"\\]*)")\s*(?:#.*)?$""")
_JUST_IMPORT = re.compile(r"""^import(\?)?\s+(?:'([^']*)'|"([^"]*)")""")
_JUST_SETTING = re.compile(r"^set\s+([\w-]+)(?:\s*:=\s*(.*))?$")
_JUST_ALIAS = re.compile(r"^alias\s+([\w-]+)\s*:=\s*([\w:-]+)")
_INTERPOLATION = re.compile(r"\{\{\{\{|\{\{(?!\{)[^\n]{0,400}?\}\}")  # `{{{{` is a literal `{{`


@dataclass
class Recipe:
    deps: list[str]  # run before it
    after: list[str]  # run after it: `release: build && tag`
    body: list[str]
    attributes: set[str]


@dataclass
class Justfile:
    recipes: dict[str, Recipe] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)  # only plain string values
    default: str = ""
    imports: list[tuple[str, bool]] = field(default_factory=list)  # (path, optional)
    modules: bool = False
    shell: str = "sh"
    workdir: str = ""


def parse_just(text: str, into: Justfile | None = None) -> Justfile:
    """Recipes, aliases, settings and imports of one justfile, added to `into` when given."""
    jf = into or Justfile()
    attributes: set[str] = set()
    current: Recipe | None = None
    for raw in text.splitlines():
        if current is not None and (raw[:1] in {" ", "\t"} or not raw.strip()):
            current.body.append(raw)
            continue
        current = None
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            attributes |= _attributes(line)
            continue
        recipe = _just_line(line, jf, attributes)
        attributes = set()
        current = recipe
    return jf


def _just_line(line: str, jf: Justfile, attributes: set[str]) -> Recipe | None:
    if match := _JUST_IMPORT.match(line):
        jf.imports.append((match.group(2) or match.group(3) or "", bool(match.group(1))))
    elif re.match(r"^mod\??\s", line):
        jf.modules = True
    elif match := _JUST_ALIAS.match(line):
        jf.aliases[match.group(1)] = match.group(2)
    elif match := _JUST_SETTING.match(line):
        _just_setting(jf, match.group(1), match.group(2) or "")
    elif match := _JUST_ASSIGN.match(line):
        literal = _JUST_LITERAL.match(match.group(2).strip())
        if literal:
            jf.variables[match.group(1)] = literal.group(1) or literal.group(2) or ""
    elif header := _just_header(line):
        name, rest = header
        before, _, after = rest.partition("&&")
        recipe = Recipe(_just_deps(before), _just_deps(after), [], attributes)
        jf.recipes[name] = recipe
        if not jf.default or "default" in attributes:
            jf.default = name
        return recipe
    return None


def _attributes(line: str) -> set[str]:
    """`[no-cd, script("python3")]` -> {"no-cd", "script:python3"}."""
    out = set()
    for name, value in re.findall(r"([\w-]+)(?:\s*\(\s*['\"]([^'\"]*)['\"])?", line):
        words = value.split()
        out.add(f"{name}:{words[0]}" if words else name)
    return out


def _just_setting(jf: Justfile, name: str, value: str) -> None:
    if name == "shell":
        first = re.search(r"""['"]([^'"]+)['"]""", value)
        jf.shell = PurePosixPath(first.group(1)).name if first else jf.shell
    elif name == "working-directory":
        literal = _JUST_LITERAL.match(value.strip())
        jf.workdir = (literal.group(1) or literal.group(2) or "") if literal else "${JUST_VALUE}"


def _just_header(line: str) -> tuple[str, str] | None:
    match = _JUST_NAME.match(line)
    if not match:
        return None
    quote = ""
    for i in range(match.end(), len(line)):
        char = line[i]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"`":
            quote = char
        elif char == ":":
            return None if line[i + 1 : i + 2] == "=" else (match.group(1), line[i + 1 :])
    return None


_JUST_DEP = re.compile(r"\(\s*([A-Za-z_][\w:-]*)[^)]*\)|([A-Za-z_][\w:-]*)")


def _just_deps(rest: str) -> list[str]:
    """`build (test "x")` -> build, test."""
    rest = re.sub(r"'[^']*'|\"[^\"]*\"|#.*", " ", rest)
    return [called or bare for called, bare in _JUST_DEP.findall(rest)]


def just_steps(jf: Justfile, names: list[str]) -> tuple[list[Step], list[str]]:
    """What `just <names>` runs, dependencies first, and the names with no recipe."""
    names = [jf.aliases.get(n, n) for n in (names or ([jf.default] if jf.default else []))]
    order: list[str] = []
    seen: set[str] = set()
    stack = [(name, False) for name in reversed(names)]
    while stack:
        name, ready = stack.pop()
        if ready:
            order.append(name)
            continue
        name = jf.aliases.get(name, name)
        if name in seen or name not in jf.recipes:
            continue
        seen.add(name)
        recipe = jf.recipes[name]
        stack += [(dep, False) for dep in reversed(recipe.after)]
        stack.append((name, True))
        stack += [(dep, False) for dep in reversed(recipe.deps)]
    steps: list[Step] = []
    for name in order:
        steps += _recipe_steps(jf, jf.recipes[name])
    return steps, [name for name in names if name not in jf.recipes]


def _recipe_steps(jf: Justfile, recipe: Recipe) -> list[Step]:
    body = _dedent(recipe.body)
    here = "no-cd" in recipe.attributes
    scripts = [a.partition(":")[2] or "sh" for a in recipe.attributes if a.startswith("script")]
    script = scripts[0] if scripts else ""
    if body and body[0].startswith("#!"):
        script = interpreter(body[0], "")
    if script:
        return [Step(_just_value(jf, "\n".join(body)), script, here)]
    steps = []
    for line in _logical_lines("\n".join(body)):
        line = _RECIPE_PREFIX.sub("", line)
        if line and not line.startswith("#"):
            steps.append(Step(_just_value(jf, line), jf.shell, here))
    return steps


def _dedent(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    cut = min(indents, default=0)
    return [line[cut:] for line in lines]


def _just_value(jf: Justfile, text: str) -> str:
    def value(match: re.Match[str]) -> str:
        if match.group(0) == "{{{{":
            return "{{"
        return jf.variables.get(match.group(0)[2:-2].strip(), "${JUST_VALUE}")

    return _INTERPOLATION.sub(value, text)


# ---- Taskfiles (go-task) --------------------------------------------------------------------

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # libyaml when available


def taskfile_text(text: str, names: list[str]) -> str:
    """The part of a Taskfile that `task <names>` runs: those tasks and the tasks they call.

    Falls back to the whole file when that can't be told exactly (an alias, an included
    Taskfile, a task name built from a variable, YAML anchors, or text that isn't YAML)."""
    if re.search(r"(?:^|\s)[&*][\w-]", text) or re.search(r"[\[{]{32}", text):
        return text  # anchors, aliases, deep nesting: not worth a slow or huge parse
    try:
        return _taskfile_part(yaml.load(text, Loader=_YAML_LOADER), names) or text
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):  # deep nesting, odd values
        return text


def _taskfile_part(data: object, names: list[str]) -> str:
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(tasks, dict):
        return ""
    exact = "includes" not in data and not any(  # else a name may mean more than its key
        "*" in str(name) or (isinstance(task, dict) and task.get("aliases"))
        for name, task in tasks.items()
    )
    todo = [(name, True) for name in reversed(names or ["default"])]
    seen: set[str] = set()
    picked: list[object] = []
    while todo:
        name, asked = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in tasks:
            if asked and exact and ":" not in name:
                continue  # task itself stops: no such task, nothing runs
            return ""
        picked.append(tasks[name])
        todo += [(call, False) for call in reversed(_task_calls(tasks[name]))]
    return json.dumps(picked, ensure_ascii=False, default=str)


def _task_calls(node: object) -> list[str]:
    calls: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("task"), str):
            calls.append(node["task"])
        deps = node.get("deps")
        if isinstance(deps, list):
            calls += [dep for dep in deps if isinstance(dep, str)]
        for value in node.values():
            calls += _task_calls(value)
    elif isinstance(node, list):
        for value in node:
            calls += _task_calls(value)
    return calls
