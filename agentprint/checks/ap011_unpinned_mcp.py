"""AP011: an MCP server is started from a package with no fixed version.

`npx -y some-mcp-server` installs and runs whatever version was published last, every time the
agent starts it, with the user's full permissions: a hijacked maintainer account or a typo'd
name runs on this machine without anything changing on disk. A pinned version changes only
when someone edits the config.

What counts as unpinned:
- npx, npm exec, pnpm dlx, yarn dlx, bunx: a package with no `@<version>`, `@latest` (or any
  other dist-tag) or a version range (`@^1.2.0`). `-p/--package` specs are read the same way.
- uvx and `uv tool run`: a package with no `==<version>` or `@<version>` (or `@latest`, or a
  range); with `--from <spec>`, that spec is the package. pipx run: the same, `--spec` included.
- docker and podman run: an image with no tag, or the `latest` tag, and no `@sha256:` digest;
  a tag with `--pull=always` (pulled again on every start).
- a git source with no commit (`github:user/repo`, `git+https://...` without `#<sha>` or
  `@<sha>`), and a remote archive URL that names no version or commit (`.../tarball/main`).
- deno run with an `npm:` package that has no version.

Never flagged: local paths and programs (`node server.js`, `./bin/server`, `uv run`), an exact
version or a digest, a runner such as `npx tsx ./server.ts` that starts a local script, and
values built from variables (`${IMAGE}`). Read from every agent's MCP config and from the MCP
config that installed plugins and extensions carry. `agentprint fix` never edits it: picking a
version is the human's call.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from ..agents.base import extension_mcp_servers
from ..models import Artifact, Finding, Kind, McpServer, Severity
from ..structured import find_line
from .base import agent_configs, safe_snippet

ID = "AP011"
TITLE = "MCP server runs an unpinned package"
FIX = (
    "Pin the package to an exact version (npx -y pkg@1.2.3, uvx pkg==1.2.3, image:1.2.3 or "
    "image@sha256:...) so the server only changes when you change the config."
)

_EXACT_NPM = re.compile(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_EXACT_PYPI = re.compile(r"v?\d+(?:\.\d+)*(?:[-_.]?(?:a|b|rc|alpha|beta|post|dev)\d*)*(?:\+\S+)?")
_COMMIT = re.compile(r"[#@][0-9a-f]{7,40}$")
_SCRIPT = re.compile(r"\.(?:[cm]?[jt]s|tsx|jsx|py)$")

_NPX_VALUE = {"-c", "--call", "--registry", "--cache", "--userconfig", "--prefix", "-w"}
_NPX_VALUE |= {"--workspace", "--node-options", "--shell"}
_UV_VALUE = {"--with", "--with-editable", "--with-requirements", "--python", "-p", "--index"}
_UV_VALUE |= {"--index-url", "-i", "--extra-index-url", "--default-index", "--find-links", "-f"}
_UV_VALUE |= {"--directory", "--project", "--config-file", "--cache-dir", "--constraints", "-c"}
_UV_VALUE |= {"--overrides", "--build-constraints", "-b", "--env-file", "--color", "-C"}
_UV_VALUE |= {"--exclude-newer", "--resolution", "--prerelease", "--index-strategy", "-P"}
_UV_VALUE |= {"--keyring-provider", "--link-mode", "--reinstall-package", "--upgrade-package"}
_UV_VALUE |= {"--no-binary-package", "--no-build-package", "--config-setting"}
_UV_VALUE |= {"--refresh-package", "--python-platform", "--torch-backend"}
_PIPX_VALUE = {"--python", "--index-url", "-i", "--pip-args", "--backend", "--fetch-python"}
_DOCKER_GLOBAL = {"--context", "-c", "-H", "--host", "--config", "-l", "--log-level"}
# Every `docker run` option that takes a value, so a value is never read as the image
# (https://docs.docker.com/reference/cli/docker/container/run/, read 2026-11-09; podman's extras).
_DOCKER_VALUE: set[str] = {
    "-a", "--attach", "--add-host", "--annotation", "--blkio-weight", "--blkio-weight-device",
    "-c", "--cpu-shares", "--cap-add", "--cap-drop", "--cgroup-parent", "--cgroupns",
    "--cidfile", "--cpu-count", "--cpu-percent", "--cpu-period", "--cpu-quota",
    "--cpu-rt-period", "--cpu-rt-runtime", "--cpus", "--cpuset-cpus", "--cpuset-mems",
    "--detach-keys", "--device", "--device-cgroup-rule", "--device-read-bps",
    "--device-read-iops", "--device-write-bps", "--device-write-iops", "--dns", "--dns-option",
    "--dns-search", "--domainname", "-e", "--env", "--entrypoint", "--env-file", "--expose",
    "--gpus", "--group-add", "--health-cmd", "--health-interval", "--health-retries",
    "--health-start-interval", "--health-start-period", "--health-timeout", "-h", "--hostname",
    "--ip", "--ip6", "--ipc", "--isolation", "--kernel-memory", "-l", "--label", "--label-file",
    "--link", "--link-local-ip", "--log-driver", "--log-opt", "--mac-address", "-m", "--memory",
    "--memory-reservation", "--memory-swap", "--memory-swappiness", "--mount", "--name",
    "--network", "--net", "--network-alias", "--oom-score-adj", "--pid", "--pids-limit",
    "--platform", "-p", "--publish", "--pull", "--restart", "--runtime", "--security-opt",
    "--shm-size", "--stop-signal", "--stop-timeout", "--storage-opt", "--sysctl", "--tmpfs",
    "--ulimit", "-u", "--user", "--userns", "--uts", "-v", "--volume", "--volume-driver",
    "--volumes-from", "-w", "--workdir", "--arch", "--os", "--variant", "--secret",
    "--hooks-dir", "--cgroup-conf", "--chrootdirs", "--conmon-pidfile", "--env-merge",
    "--gidmap", "--uidmap", "--subgidname", "--subuidname", "--pod", "--pod-id-file",
    "--preserve-fds", "--requires", "--sdnotify", "--seccomp-policy", "--timeout", "--tz",
    "--umask", "--unsetenv", "--passwd-entry",
}  # fmt: skip


@dataclass(frozen=True)
class Unpinned:
    launcher: str  # "npx", "uvx", "pipx run", "docker run"
    spec: str  # the package or image as written
    problem: str  # "no version", "the latest tag", "a version range (^1.2.0)", ...
    example: str  # the pinned form to write instead


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _program(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        name = name.removesuffix(suffix)
    return name


def _unwrap(argv: list[str]) -> list[str]:
    """The command a wrapper runs: cmd /c ..., sh -c "...", env VAR=x ..."""
    for _ in range(4):
        if not argv:
            return argv
        program = _program(argv[0])
        if program == "cmd" and len(argv) > 1 and argv[1].lower() in ("/c", "/k"):
            rest = argv[2:]
            argv = _split(rest[0]) if len(rest) == 1 else rest
        elif program in ("sh", "bash", "zsh") and "-c" in argv[1:3]:
            at = argv.index("-c")
            argv = _split(argv[at + 1]) if at + 1 < len(argv) else []
        elif program == "env":
            argv = argv[1:]
            while argv and ("=" in argv[0] or argv[0].startswith("-")):
                argv = argv[1:]
        elif program == "exec":  # sh -c "exec npx -y pkg"
            argv = argv[1:]
        else:
            return argv
    return argv


def _options(
    args: list[str], value_options: set[str], spec_options: set[str]
) -> tuple[list[str], int]:
    """(specs given with a spec option such as --from or -p, index of the first positional)."""
    specs: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition("=")
        if arg == "--":
            return specs, i + 1
        if name in spec_options:
            if eq:
                specs.append(value)
            elif i + 1 < len(args):
                specs.append(args[i + 1])
                i += 1
        elif arg in value_options:
            i += 1
        elif not arg.startswith("-"):
            return specs, i
        i += 1
    return specs, i


_VERSIONED_URL = re.compile(
    r"[/@#-]v?\d+\.\d+\.\d+[^/]*$|/[0-9a-f]{7,40}(?:\.(?:tar\.gz|tgz|zip))?$"
)


def _remote_problem(spec: str) -> str | None:
    """A package fetched from a URL: pinned when the URL names a version or a commit."""
    return None if _VERSIONED_URL.search(spec) else "a remote archive with no fixed version"


def _local(spec: str) -> bool:
    return (
        spec.startswith((".", "/", "~", "file:"))
        or "\\" in spec
        or bool(re.match(r"[A-Za-z]:", spec))
        or "$" in spec
        or "{" in spec
    )


_GIT = "a git source with no commit"


def _git_problem(spec: str) -> str | None:
    return None if _COMMIT.search(spec) else _GIT


def _npm_problem(spec: str) -> str | None:
    if _local(spec):
        return None
    if spec.startswith(("http://", "https://")):
        return _remote_problem(spec)
    if spec.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:")) or re.fullmatch(
        r"[\w.-]+/[\w.-]+(?:#.*)?", spec
    ):
        return _git_problem(spec)
    at = spec.rfind("@")
    version = spec[at + 1 :] if at > 0 else ""
    if not version:
        return "no version"
    if _EXACT_NPM.fullmatch(version):
        return None
    if re.fullmatch(r"[A-Za-z][\w.-]*", version):
        return f"the {version} tag"
    return f"a version range ({version})"


def _pypi_problem(spec: str) -> str | None:
    if _local(spec):
        return None
    if spec.startswith(("http://", "https://")):
        return _remote_problem(spec)
    if spec.startswith("git+"):
        return _git_problem(spec.partition("#")[0])
    match = re.match(r"([A-Za-z0-9][\w.-]*)(\[[^\]]*\])?\s*(.*)$", spec)
    rest = match.group(3).strip() if match else ""
    if not rest:
        return "no version"
    for exact in ("===", "==", "@"):
        if rest.startswith(exact):
            version = rest[len(exact) :].strip()
            if _EXACT_PYPI.fullmatch(version):
                return None
            if version.lower() == "latest" or not version:
                return "the latest tag" if version else "no version"
            break
    return f"a version range ({rest})"


def _package(spec: str, sep: str) -> str:
    """The package name of a spec, for the pinned example."""
    if sep == "@":
        at = spec.rfind("@")
        return spec[:at] if at > 0 else spec
    return re.split(r"[=<>!~@\s;]", spec, maxsplit=1)[0]


def _npm(launcher: str, args: list[str]) -> Unpinned | None:
    specs, at = _options(args, _NPX_VALUE, {"-p", "--package"})
    if not specs:
        if at >= len(args):
            return None
        if any(_SCRIPT.search(a) or a.startswith(("./", "../", "/")) for a in args[at + 1 :]):
            return None  # a runner (tsx, ts-node) that starts a local script
        specs = [args[at]]
    for spec in specs:
        problem = _npm_problem(spec)
        if problem:
            yes = " -y" if launcher in ("npx", "npm exec") else ""
            pinned = f"{spec}#<commit>" if _GIT in problem else f"{_package(spec, '@')}@<version>"
            return Unpinned(launcher, spec, problem, f"{launcher}{yes} {pinned}")
    return None


def _uvx(launcher: str, args: list[str]) -> Unpinned | None:
    specs, at = _options(args, _UV_VALUE, {"--from"})
    command = args[at] if at < len(args) else ""
    spec = specs[0] if specs else command
    if not spec:
        return None
    problem = _pypi_problem(spec)
    if problem is None:
        return None
    pinned = f"{spec}@<commit>" if _GIT in problem else f"{_package(spec, '=')}==<version>"
    if specs and command:
        example = f"{launcher} --from {pinned} {command}"
    else:
        example = f"{launcher} {pinned}"
    return Unpinned(launcher, spec, problem, example)


def _pipx(args: list[str]) -> Unpinned | None:
    specs, at = _options(args, _PIPX_VALUE, {"--spec"})
    app = args[at] if at < len(args) else ""
    spec = specs[0] if specs else app
    if not spec:
        return None
    problem = _pypi_problem(spec)
    if problem is None:
        return None
    pinned = f"{spec}@<commit>" if _GIT in problem else f"{_package(spec, '=')}==<version>"
    return Unpinned("pipx run", spec, problem, f"pipx run --spec {pinned} {app or spec}")


def _deno(args: list[str]) -> Unpinned | None:
    """deno run [options] npm:pkg[@version]: an npm: specifier is an npm package."""
    for arg in args:
        if arg.startswith("npm:"):
            spec = arg[4:]
            problem = _npm_problem(spec)
            if problem is None:
                return None
            name = _package(spec, "@")
            return Unpinned("deno run", arg, problem, f"deno run npm:{name}@<version>")
        if not arg.startswith("-"):
            return None  # a local file or a jsr:/https: module: not an npm package
    return None


def _image_problem(ref: str) -> str | None:
    if "$" in ref or "{" in ref or "@sha256:" in ref:
        return None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        return "the latest tag" if last.split(":", 1)[1] == "latest" else None
    return "no tag"


def _docker(program: str, args: list[str]) -> Unpinned | None:
    _, at = _options(args, _DOCKER_GLOBAL, set())
    rest = args[at:]
    if rest[:2] == ["container", "run"]:
        rest = rest[2:]
    elif rest[:1] == ["run"]:
        rest = rest[1:]
    else:
        return None
    pulls, at = _options(rest, _DOCKER_VALUE, {"--pull"})
    if at >= len(rest):
        return None
    image = rest[at]
    problem = _image_problem(image)
    if problem is None and "always" in pulls and "@sha256:" not in image and "$" not in image:
        problem = "a tag pulled again on every start (--pull=always)"
    if problem is None:
        return None
    name = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    return Unpinned(f"{program} run", image, problem, f"{name}:<version> or {name}@sha256:<digest>")


def unpinned(server: McpServer) -> Unpinned | None:
    """How this server's command runs a package with no fixed version, or None."""
    argv = list(server.argv)
    if len(argv) == 1 and any(ch.isspace() for ch in argv[0]):
        argv = _split(argv[0])  # the whole command line written in "command"
    argv = _unwrap(argv)
    if not argv:
        return None
    program, args = _program(argv[0]), argv[1:]
    if program in ("npx", "bunx"):
        return _npm(program, args)
    if program in ("pnpm", "yarn") and "dlx" in args[:4]:
        at = args.index("dlx")
        if all(arg.startswith("-") for arg in args[:at]):  # pnpm --silent dlx pkg
            return _npm(f"{program} dlx", args[at + 1 :])
    if program == "deno" and args[:1] == ["run"]:
        return _deno(args[1:])
    if program == "npm" and args[:1] in (["exec"], ["x"]):
        return _npm("npm exec", args[1:])
    if program == "uvx":
        return _uvx("uvx", args)
    if program == "uv" and args[:2] == ["tool", "run"]:
        return _uvx("uv tool run", args[2:])
    if program == "pipx" and args[:1] == ["run"]:
        return _pipx(args[1:])
    if program in ("docker", "podman"):
        return _docker(program, args)
    return None


class UnpinnedMcpServer:
    id = ID
    severity = Severity.MEDIUM
    title = TITLE
    fix = FIX

    def run(self, artifacts: Iterable[Artifact]) -> list[Finding]:
        artifacts = list(artifacts)
        findings: list[Finding] = []
        settings = set()
        for artifact, config in agent_configs(artifacts):
            settings.add(artifact.path)
            findings += self._findings(artifact, config.mcp_servers)
        for artifact in artifacts:
            if artifact.kind is Kind.EXTENSION and artifact.path not in settings:
                findings += self._findings(artifact, extension_mcp_servers(artifact))
        return findings

    def _findings(self, artifact: Artifact, servers: Iterable[McpServer]) -> list[Finding]:
        found: list[Finding] = []
        for server in servers:
            hit = unpinned(server)
            if hit is None:
                continue
            name = safe_snippet(server.name, 60)
            spec = safe_snippet(hit.spec, 100)
            example = safe_snippet(hit.example, 160)
            found.append(
                Finding(
                    check_id=self.id,
                    severity=self.severity,
                    title=self.title,
                    agent=artifact.agent,
                    location=str(artifact.path),
                    line=_line(artifact, server, hit),
                    detail=f"MCP server '{name}' runs {hit.launcher} {spec} with {hit.problem}",
                    fix=(
                        f"In this file, pin MCP server '{name}' to an exact version: {example}. "
                        "Every start then runs that version until the config changes."
                    ),
                )
            )
        return found


def _line(artifact: Artifact, server: McpServer, hit: Unpinned) -> int | None:
    for needle in (json.dumps(hit.spec)[1:-1], hit.spec, json.dumps(server.name)):
        line = find_line(artifact.path, needle)
        if line:
            return line
    return None


CHECK = UnpinnedMcpServer()
