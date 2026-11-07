"""Build the benchmark corpus at run time: labelled cases in realistic agent file shapes.

    uv run python -m bench.build_corpus <empty-dir>

Nothing key-shaped is committed. Every key is generated here, from a seeded generator:

- The format (prefix, alphabet, length) is the real one for each provider, so detectors that
  check structure see a plausible key.
- The body is random, not a real credential: the chance of colliding with a live key is
  negligible, and the generator starts every body with the marker FAKE where the format allows.
- Same seed, same corpus, byte for byte. Nothing is read from the machine it runs on.

Each case is one folder `cases/<case-id>/` holding one agent file at the path an agent would
use. Positive cases hold one key; negative cases hold look-alikes and no key. The manifest
(`manifest.json`) maps case id to category, label and file; it holds no key values.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SEED = 20261107
ZERO_WIDTH = "​"  # zero-width space

ALNUM = string.ascii_letters + string.digits
UPPER_DIGITS = string.ascii_uppercase + string.digits
URLSAFE = ALNUM + "_-"


def _body(rng: random.Random, alphabet: str, n: int) -> str:
    """`FAKE` marker first, then random characters; the result is exactly n long."""
    return ("FAKE" + "".join(rng.choice(alphabet) for _ in range(n)))[:n]


def make_key(kind: str, rng: random.Random) -> str:
    """A fake key in the real format of `kind`. Prefixes are joined so no literal is whole."""
    if kind == "anthropic":
        return "sk-ant-" + "api03-" + _body(rng, URLSAFE, 93) + "AA"
    if kind == "openai":
        return "sk-" + "proj-" + _body(rng, ALNUM, 58) + "T3Blbk" + "FJ" + _body(rng, ALNUM, 58)
    if kind == "github":
        return "gh" + "p_" + _body(rng, ALNUM, 36)
    if kind == "aws":
        return "AK" + "IA" + _body(rng, UPPER_DIGITS, 16)
    if kind == "stripe":
        return "sk" + "_live_" + _body(rng, ALNUM, 24)
    if kind == "slack":
        digits = string.digits
        return (
            "xo"
            + "xb-"
            + "".join(rng.choice(digits) for _ in range(12))
            + "-"
            + "".join(rng.choice(digits) for _ in range(13))
            + "-"
            + _body(rng, ALNUM, 24)
        )
    if kind == "google":
        return "AI" + "za" + _body(rng, URLSAFE, 35)
    raise ValueError(kind)


def make_secret_access_key(rng: random.Random) -> str:
    """The paired AWS secret access key (40 chars, FAKE first) that scanners look for."""
    return _body(rng, ALNUM + "/+", 40)


KINDS = ("anthropic", "openai", "github", "aws", "stripe", "slack", "google")
ENV_NAMES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "github": "GITHUB_TOKEN",
    "aws": "AWS_ACCESS_KEY_ID",
    "stripe": "STRIPE_API_KEY",
    "slack": "SLACK_BOT_TOKEN",
    "google": "GOOGLE_API_KEY",
}


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    positive: bool
    file: str  # path of the agent file, relative to the case folder
    lines: tuple[int, int] | None = None  # 1-based line span of the injected key; None = negative


# --- file shapes: each takes a payload (a key, a transformed key or a look-alike) -------------

Entries = list[tuple[str, str]]  # (variable name, value); the first value holds the key
Shape = Callable[[Entries], tuple[str, str]]  # entries -> (path, text)


def _j(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def history(entries: Entries) -> tuple[str, str]:
    lines = [{"display": "fix the failing test in auth_test.py", "timestamp": 1762500000000}]
    for n, v in entries:  # one prompt per variable, so a paired value sits on its own line
        lines.append(
            {
                "display": f"export {n}={v} && npm run dev",
                "timestamp": 1762500060000,
                "project": "/Users/dev/shop",
            }
        )
    lines.append({"display": "now run the linter", "timestamp": 1762500120000})
    return ".claude/history.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)


def claude_settings(entries: Entries) -> tuple[str, str]:
    calls = [f"Bash(curl -s -H '{n}: {v}' https://api.example.com/v1/ping)" for n, v in entries]
    data = {
        "permissions": {
            "allow": [
                "Bash(git status)",
                "Bash(npm run test)",
                *calls,
            ]
        }
    }
    return ".claude/settings.local.json", _j(data)


def claude_mcp(entries: Entries) -> tuple[str, str]:
    data = {
        "mcpServers": {
            "search": {
                "command": "npx",
                "args": ["-y", "@example/search-mcp"],
                "env": dict(entries),
            }
        }
    }
    return ".mcp.json", _j(data)


def codex_toml(entries: Entries) -> tuple[str, str]:
    env = "".join(f'{n} = "{v}"\n' for n, v in entries)
    text = (
        'model = "gpt-5"\n'
        'approval_policy = "on-request"\n\n'
        "[mcp_servers.search]\n"
        'command = "npx"\n'
        'args = ["-y", "@example/search-mcp"]\n\n'
        "[mcp_servers.search.env]\n"
        f"{env}"
    )
    return ".codex/config.toml", text


def cursor_mcp(entries: Entries) -> tuple[str, str]:
    data = {"mcpServers": {"search": {"command": "npx", "args": ["-y", "x"], "env": dict(entries)}}}
    return ".cursor/mcp.json", _j(data)


def hermes_yaml(entries: Entries) -> tuple[str, str]:
    env = "".join(f"      {n}: {v}\n" for n, v in entries)
    text = (
        "approvals:\n  mode: manual\n"
        "mcp_servers:\n  search:\n    command: npx\n    args: [-y, '@example/search-mcp']\n"
        f"    env:\n{env}"
    )
    return ".hermes/config.yaml", text


SHAPES: dict[str, Shape] = {
    "claude-history": history,
    "claude-settings": claude_settings,
    "claude-mcp": claude_mcp,
    "codex-toml": codex_toml,
    "cursor-mcp": cursor_mcp,
    "hermes-yaml": hermes_yaml,
}

# --- obfuscations of the key itself -----------------------------------------------------------


def _zero_width(key: str, rng: random.Random) -> str:
    cut = rng.randrange(5, len(key) - 5)
    return key[:cut] + ZERO_WIDTH + key[cut:]


def _split_string(key: str, rng: random.Random) -> str:
    cut = rng.randrange(6, len(key) - 6)
    return key[:cut] + '"' + '"' + key[cut:]  # shell adjacency: "head""tail"


def _split_plus(key: str, rng: random.Random) -> str:
    cut = rng.randrange(6, len(key) - 6)
    return key[:cut] + "' + '" + key[cut:]


def _continuation(key: str, rng: random.Random) -> str:
    cut = rng.randrange(6, len(key) - 6)
    return key[:cut] + "\\\n" + key[cut:]  # backslash-newline, as in a shell command


def _base64(key: str, rng: random.Random) -> str:
    return "$(echo " + base64.b64encode(key.encode()).decode() + " | base64 -d)"


def _folded(key: str, rng: random.Random) -> str:
    cut = rng.randrange(6, len(key) - 6)
    return ">\n        " + key[:cut] + "\n        " + key[cut:]


# category -> (obfuscator, shape name). The shape is where that trick would really occur.
OBFUSCATIONS: dict[str, tuple[Callable[[str, random.Random], str], str]] = {
    "obf-zero-width": (_zero_width, "claude-history"),
    "obf-split-string": (_split_string, "claude-history"),
    "obf-split-concat": (_split_plus, "claude-settings"),
    "obf-line-continuation": (_continuation, "claude-history"),
    "obf-base64": (_base64, "claude-history"),
    "obf-yaml-folded": (_folded, "hermes-yaml"),
}

# --- negatives: realistic look-alikes with no key ----------------------------------------------


def _sha1(rng: random.Random) -> str:
    return hashlib.sha1(rng.randbytes(16)).hexdigest()


def _sha256(rng: random.Random) -> str:
    return hashlib.sha256(rng.randbytes(16)).hexdigest()


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _png(rng: random.Random) -> str:
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4" + base64.b64encode(
        rng.randbytes(120)
    ).decode().replace("=", "")


def _minified_js(rng: random.Random) -> str:
    names = ["a", "b", "c", "d", "e", "f"]
    parts = [
        f"function {n}(t,e){{return t&&e?t[{rng.randrange(9)}]+e.length:{rng.randrange(1000)}}}"
        for n in names
    ]
    return (
        "".join(parts)
        + "var token=localStorage.getItem('t');module.exports={"
        + ",".join(names)
        + "};"
    )


def _jwt_shaped(rng: random.Random) -> str:
    # Header and payload of a toy token with a dummy signature: a test string, no key format.
    head = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    claims = (
        f'{{"sub":"test-user-{rng.randrange(10**6)}","iat":{1700000000 + rng.randrange(10**6)}}}'
    )
    body = base64.urlsafe_b64encode(claims.encode()).decode().rstrip("=")
    return f"{head}.{body}."


LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt "
    "ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation."
)

# category -> (label for the field, value maker, shape)
NEGATIVES: dict[str, tuple[Callable[[random.Random], str], str]] = {
    "neg-git-sha": (_sha1, "claude-history"),
    "neg-uuid": (_uuid, "claude-history"),
    "neg-sha256": (_sha256, "claude-mcp"),
    "neg-base64-image": (_png, "claude-history"),
    "neg-minified-js": (_minified_js, "claude-history"),
    "neg-jwt-shaped": (_jwt_shaped, "claude-history"),
    "neg-jwt-in-token-field": (_jwt_shaped, "codex-toml"),
    "neg-lorem-ipsum": (lambda rng: LOREM, "claude-history"),
    "neg-placeholder": (lambda rng: "YOUR_API_KEY_HERE", "cursor-mcp"),
}


def _negative_file(category: str, value: str, shape: str) -> tuple[str, str]:
    """The shape's file with a look-alike where a key would sit, under a harmless name."""
    if shape == "claude-history":
        lines = [
            {"display": "show me what changed since the last release", "timestamp": 1762500000000},
            {"display": f"git show {value}" if category == "neg-git-sha" else value[:600]},
        ]
        if category == "neg-uuid":
            lines[1] = {"display": f"resume session {value}", "sessionId": value}
        return ".claude/history.jsonl", "".join(json.dumps(x) + "\n" for x in lines)
    name = {"neg-sha256": "SHA256_DIGEST", "neg-jwt-in-token-field": "TEST_TOKEN"}.get(
        category, "EXAMPLE"
    )
    return SHAPES[shape]([(name, value)])


SENTINEL = "PAYLOADSENTINEL"


def key_span(shape: Shape, names: list[str], payload: str, secret: str | None) -> tuple[int, int]:
    """1-based line span of the injected key (the key ID only, never its paired secret).

    The file is rendered once with sentinels in place of the values to find the key's lines, then
    with the real values; extra lines the key itself adds (continuation, folded block) extend
    the span's end.
    """
    sentinels: Entries = [(names[0], SENTINEL + "0")]
    if secret is not None:
        sentinels.append((names[1], SENTINEL + "1"))
    _, template = shape(sentinels)
    hits = [i + 1 for i, line in enumerate(template.split("\n")) if SENTINEL + "0" in line]
    entries: Entries = [(names[0], payload)] + ([(names[1], secret)] if secret is not None else [])
    _, real = shape(entries)
    extra = real.count("\n") - template.count("\n")
    return hits[0], hits[-1] + extra


def build(root: Path, seed: int = SEED) -> list[Case]:
    """Write the corpus under root/cases and the manifest under root; return the cases."""
    rng = random.Random(seed)
    cases: list[Case] = []

    def add_positive(category: str, n: int, shape: Shape, kind: str, payload: str) -> None:
        names = [ENV_NAMES[kind]]
        secret = None
        if kind == "aws":
            # trufflehog's AWS detector needs a secret access key in the file. It sits on a
            # different line from the key ID and is outside the scored span.
            names.append("AWS_SECRET_ACCESS_KEY")
            secret = make_secret_access_key(rng)
        entries: Entries = [(names[0], payload)] + ([(names[1], secret)] if secret else [])
        path, text = shape(entries)
        span = key_span(shape, names, payload, secret)
        write(Case(f"{category}-{n}", category, True, path, span), text)

    def write(case: Case, text: str) -> None:
        target = root / "cases" / case.id / case.file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        target.chmod(0o600)
        cases.append(case)

    for shape_name, shape in SHAPES.items():
        for n, kind in enumerate(KINDS):
            add_positive(f"plain-{shape_name}", n, shape, kind, make_key(kind, rng))
    for category, (obfuscate, shape_name) in OBFUSCATIONS.items():
        for n, kind in enumerate(KINDS):
            payload = obfuscate(make_key(kind, rng), rng)
            add_positive(category, n, SHAPES[shape_name], kind, payload)
    for category, (make, shape_name) in NEGATIVES.items():
        for n in range(7):
            path, text = _negative_file(category, make(rng), shape_name)
            write(Case(f"{category}-{n}", category, False, path), text)

    manifest = [
        {
            "id": c.id,
            "category": c.category,
            "positive": c.positive,
            "file": c.file,
            "lines": list(c.lines) if c.lines else None,
        }
        for c in cases
    ]
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return cases


def load_manifest(root: Path) -> list[Case]:
    raw = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return [
        Case(
            r["id"],
            r["category"],
            r["positive"],
            r["file"],
            tuple(r["lines"]) if r["lines"] else None,
        )
        for r in raw
    ]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m bench.build_corpus <empty-dir>", file=sys.stderr)
        return 2
    root = Path(args[0])
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        print(f"{root} is not empty", file=sys.stderr)
        return 2
    cases = build(root)
    positives = sum(c.positive for c in cases)
    print(f"{len(cases)} cases: {positives} with a key, {len(cases) - positives} without")
    return 0


if __name__ == "__main__":
    sys.exit(main())
