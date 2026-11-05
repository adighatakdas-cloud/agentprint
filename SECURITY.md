# Security policy

## Reporting a vulnerability

Please report security problems in agentprint **privately, by email: adi65das@gmail.com**.
Please don't open a public issue for them.

You will get a first reply **within 7 days**. After that, we agree together on a fix and a date
to publish it, and you are credited in the release notes unless you would rather not be.

A good report says:

- what is wrong, and the steps to see it happen (agentprint version, operating system, command);
- what an attacker could do with it.

**Never send a real key, token or password**, not even one you found with agentprint. If a key
matters to the report, send it the way agentprint prints it: redacted to the first 4 characters
and its length.

## What counts

In scope:

- the agentprint Python package and command line (`scan`, `pack` and the other commands);
- the npm wrapper (`npx agentprint-cli`), the GitHub Action (`action.yml`) and the pre-commit hooks
  in this repository;
- the release workflow in this repository.

Examples: agentprint shows a full secret instead of the redacted form; a crafted file or package
makes agentprint run code, write files, hang, hide a finding, or print text that could mislead a
person or an AI agent reading the output.

Out of scope:

- keys or agent files that agentprint finds in someone else's package or repository. Report
  those to that project's maintainers (and to the registry's security team if they don't reply);
- `agentprint pack` running a Python project's build backend. This is documented: only run it on
  projects you trust, or point it at an already-built archive.

## What agentprint does with your data

Nothing. agentprint never sends data anywhere: no telemetry, no accounts, and no network calls
during a scan. It never uses a key it finds, not even to check whether the key still works, and
it always shows keys redacted.

The only downloads are ones you start: `uvx agentprint` / `npx agentprint-cli` fetch agentprint
itself, and `agentprint pack` on a project runs your own packaging tool (`npm pack --dry-run` or
`uv build`), which may fetch build tools.

## Supported versions

Security fixes go into the latest release only.
