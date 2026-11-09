"""AP011: MCP servers started from a package with no fixed version."""

import json
from pathlib import Path

import pytest

from agentprint import advice, fix
from agentprint.checks import SCAN_CHECKS
from agentprint.checks.ap011_unpinned_mcp import CHECK as AP011
from agentprint.checks.ap011_unpinned_mcp import unpinned
from agentprint.models import McpServer, Severity
from agentprint.scanner import scan
from tests.fixtures import extensions as ext


def _server(*argv: str) -> McpServer:
    return McpServer("x", (), "", argv)


def _ap011(home, project):
    return [f for f in scan(home, project) if f.check_id == "AP011"]


def test_ap011_is_registered():
    assert AP011 in SCAN_CHECKS
    assert (AP011.id, AP011.severity) == ("AP011", Severity.MEDIUM)


@pytest.mark.parametrize(
    ("argv", "spec", "problem"),
    [
        (
            ("npx", "-y", "@modelcontextprotocol/server-github"),
            "@modelcontextprotocol/server-github",
            "no version",
        ),
        (("npx", "maps-mcp"), "maps-mcp", "no version"),
        (("npx", "-y", "maps-mcp@latest"), "maps-mcp@latest", "the latest tag"),
        (("npx", "-y", "maps-mcp@next"), "maps-mcp@next", "the next tag"),
        (("npx", "-y", "maps-mcp@^1.2.0"), "maps-mcp@^1.2.0", "a version range (^1.2.0)"),
        (("npx", "-y", "--package=maps-mcp", "maps"), "maps-mcp", "no version"),
        (("npx -y maps-mcp",), "maps-mcp", "no version"),
        (("cmd", "/c", "npx", "-y", "maps-mcp"), "maps-mcp", "no version"),
        (("bash", "-c", "npx -y maps-mcp"), "maps-mcp", "no version"),
        (("bunx", "maps-mcp"), "maps-mcp", "no version"),
        (("pnpm", "dlx", "maps-mcp"), "maps-mcp", "no version"),
        (("npx", "-y", "github:someone/mcp"), "github:someone/mcp", "a git source with no commit"),
        (("uvx", "mcp-server-fetch"), "mcp-server-fetch", "no version"),
        (("uvx", "mcp-server-fetch@latest"), "mcp-server-fetch@latest", "the latest tag"),
        (("uvx", "mcp-server-fetch>=1.0"), "mcp-server-fetch>=1.0", "a version range (>=1.0)"),
        (("uvx", "--python", "3.12", "mcp-server-fetch"), "mcp-server-fetch", "no version"),
        (("uvx", "--from", "mcp-server-fetch", "fetch"), "mcp-server-fetch", "no version"),
        (("uv", "tool", "run", "mcp-server-fetch"), "mcp-server-fetch", "no version"),
        (("pipx", "run", "mcp-server-fetch"), "mcp-server-fetch", "no version"),
        (
            ("docker", "run", "-i", "--rm", "-e", "TOKEN", "ghcr.io/x/mcp"),
            "ghcr.io/x/mcp",
            "no tag",
        ),
        (("docker", "run", "-i", "mcp/search:latest"), "mcp/search:latest", "the latest tag"),
        (("podman", "run", "-i", "localhost:5000/mcp"), "localhost:5000/mcp", "no tag"),
    ],
)
def test_unpinned_launches(argv, spec, problem):
    hit = unpinned(_server(*argv))
    assert hit is not None and (hit.spec, hit.problem) == (spec, problem)


@pytest.mark.parametrize(
    "argv",
    [
        ("npx", "-y", "@modelcontextprotocol/server-github@2025.4.8"),
        ("npx", "-y", "maps-mcp@1.2.3"),
        ("npx", "-y", "maps-mcp@1.2.3-beta.1"),
        ("npx", "-y", "tsx", "./server.ts"),
        ("npx", "-y", "github:someone/mcp#0123456789abcdef0123456789abcdef01234567"),
        ("npx", "./local-server"),
        ("npx",),
        ("uvx", "mcp-server-fetch==2025.1.17"),
        ("uvx", "mcp-server-fetch@2025.1.17"),
        ("uvx", "--from", "agentprint==0.1.1", "agentprint", "mcp"),
        ("uvx", "--from", "git+https://example.invalid/x/y@0123abc", "y"),
        ("uvx", "--from", "./tools/server", "server"),
        ("pipx", "run", "--spec", "mcp-server-fetch==1.0.0", "mcp-server-fetch"),
        ("docker", "run", "-i", "--rm", "mcp/search:1.4.0"),
        ("docker", "run", "-i", "mcp/search@sha256:" + "ab" * 32),
        ("docker", "run", "-i", "${IMAGE}"),
        ("docker", "ps"),
        ("node", "/opt/mcp/server.js"),
        ("python", "-m", "my_server"),
        ("uv", "run", "server.py"),
        ("./bin/server", "--stdio"),
    ],
)
def test_pinned_or_local_launches_pass(argv):
    assert unpinned(_server(*argv)) is None


def test_scan_flags_each_unpinned_server_once_with_its_line(tmp_path):
    project = tmp_path / "proj"
    mcp = ext.unpinned_servers(project)
    findings = sorted(_ap011(None, project), key=lambda f: f.line or 0)
    assert [(f.location, f.line) for f in findings] == [
        (str(mcp), 7),
        (str(mcp), 13),
        (str(mcp), 22),
    ]
    first = findings[0]
    assert first.detail == (
        "MCP server 'github' runs npx @modelcontextprotocol/server-github with no version"
    )
    assert "npx -y @modelcontextprotocol/server-github@<version>" in first.fix
    assert "mcp/search:<version> or mcp/search@sha256:<digest>" in findings[2].fix


def test_benign_world_has_no_unpinned_server(tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    ext.benign(home, project)
    assert _ap011(home, project) == []


def test_plugin_mcp_config_is_read(tmp_path):
    home = tmp_path / "home"
    plugin = home / ".gemini" / "extensions" / "maps"
    manifest = {"name": "maps", "mcpServers": {"maps": {"command": "npx", "args": ["maps-mcp"]}}}
    ext.write(plugin / "gemini-extension.json", json.dumps(manifest, indent=2))
    # Only config at the plugin's top counts: an example inside it is not a server it starts.
    ext.write(plugin / "examples" / "mcp.json", json.dumps(manifest))
    [finding] = _ap011(home, None)
    assert (
        finding.location == str(plugin / "gemini-extension.json") and finding.agent == "gemini-cli"
    )


def test_every_agents_mcp_config_is_read(tmp_path):
    home = tmp_path / "home"
    servers = {"mcpServers": {"m": {"command": "npx", "args": ["-y", "maps-mcp"]}}}
    for rel in (
        ".cursor/mcp.json",
        ".copilot/mcp-config.json",
        ".codeium/windsurf/mcp_config.json",
    ):
        ext.write(home / rel, json.dumps(servers))
    ext.write(
        home / ".codex" / "config.toml", '[mcp_servers.m]\ncommand = "uvx"\nargs = ["fetch-mcp"]\n'
    )
    agents = sorted(f.agent for f in _ap011(home, None))
    assert agents == ["codex", "copilot", "cursor", "windsurf"]


def test_fix_leaves_the_version_to_the_human(tmp_path):
    project = tmp_path / "proj"
    ext.unpinned_servers(project)
    findings = _ap011(None, project)
    plan = fix.plan(findings)
    assert plan.changes == [] and len(plan.todo) == 3
    agent = advice.advise(findings[0], advice.Moment.scan)
    person = advice.advise(findings[0], advice.Moment.scan, person=True)
    assert agent.ask_human[0].startswith("Ask the human which exact version to pin")
    assert person.ask_human[0].startswith("Choose the exact version")
    assert Path(findings[0].location).read_text().count("@") >= 1  # never edited


@pytest.mark.parametrize(
    "argv",
    [
        ("env", "NODE_ENV=production", "npx", "-y", "maps-mcp"),
        ("npx", "--", "maps-mcp"),
        ("npm", "exec", "--", "maps-mcp"),
        ("docker", "container", "run", "-i", "mcp/search"),
        ("docker", "--context", "remote", "run", "-i", "mcp/search"),
    ],
)
def test_wrapped_and_long_forms(argv):
    assert unpinned(_server(*argv)) is not None


def test_only_extension_files_carry_plugin_mcp_servers(tmp_path):
    from agentprint.agents.base import ExtensionArtifact, extension_mcp_servers
    from agentprint.models import Artifact, Kind

    f = ext.write(tmp_path / "p" / ".mcp.json", json.dumps({"mcpServers": {"m": {"command": "x"}}}))
    assert extension_mcp_servers(Artifact("a", Kind.SETTINGS, f)) == ()
    other = ExtensionArtifact("a", Kind.EXTENSION, f, root=tmp_path / "elsewhere")
    assert extension_mcp_servers(other) == ()
    [server] = extension_mcp_servers(ExtensionArtifact("a", Kind.EXTENSION, f, root=f.parent))
    assert server.argv == ("x",)


def test_qwen_extension_manifest_mcp_servers_are_read(tmp_path):
    home = tmp_path / "home"
    manifest = {"name": "x", "mcpServers": {"x": {"command": "uvx", "args": ["x-mcp"]}}}
    path = home / ".qwen" / "extensions" / "x" / "qwen-extension.json"
    ext.write(path, json.dumps(manifest, indent=2))
    [finding] = _ap011(home, None)
    assert (finding.location, finding.agent) == (str(path), "qwen-code")


@pytest.mark.parametrize(
    "argv",
    [
        ("docker", "run", "-i", "--rm", "--pids-limit", "100", "mcp/x:1.0"),
        ("docker", "run", "-i", "--memory-reservation", "1g", "mcp/x:1.0"),
        ("docker", "run", "-i", "--dns-search", "corp", "mcp/x:1.0"),
        ("docker", "run", "-i", "--health-start-period", "5s", "mcp/x:1.0"),
        ("docker", "run", "-i", "--cpu-quota", "5000", "--stop-timeout", "3", "mcp/x:1.0"),
        ("docker", "run", "-i", "--pull", "always", "mcp/x@sha256:" + "ab" * 32),
        (
            "npx",
            "-y",
            "https://x.example.invalid/x/y/tarball/0123456789abcdef0123456789abcdef01234567",
        ),
        ("npx", "-y", "https://registry.example.invalid/pkg-1.2.3.tgz"),
        ("deno", "run", "-A", "npm:some-mcp@1.2.3"),
        ("deno", "run", "-A", "./server.ts"),
    ],
)
def test_review_pinned_forms_pass(argv):
    assert unpinned(_server(*argv)) is None


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (("docker", "run", "-i", "--rm", "--pull=always", "mcp/x:1.0"), "--pull=always"),
        (("pnpm", "--silent", "dlx", "some-mcp"), "no version"),
        (("sh", "-c", "exec npx -y some-mcp"), "no version"),
        (("npx", "-y", "https://x.example.invalid/x/y/tarball/main"), "remote archive"),
        (("deno", "run", "-A", "npm:some-mcp"), "no version"),
    ],
)
def test_review_unpinned_forms(argv, problem):
    hit = unpinned(_server(*argv))
    assert hit is not None and problem in hit.problem
