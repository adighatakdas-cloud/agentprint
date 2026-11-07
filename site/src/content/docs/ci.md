---
title: Use it in CI
description: Stop a leaky npm publish with prepublishOnly, run agentprint as a GitHub Action, or add it to pre-commit, lefthook or husky.
---

:::note
These steps work after the first release.
:::

## Stop a leaky `npm publish`

Add a `prepublishOnly` script to `package.json`. The npm name `agentprint` belongs to an unrelated project, so the wrapper is `agentprint-cli`. It runs the same tool through uvx or pipx.

```json
"scripts": { "prepublishOnly": "npx agentprint-cli pack" }
```

`pack` is a preflight: it checks what the package would publish and exits `1` if something is at or above `--fail-on` (see [exit codes](/agentprint/get-started/#exit-codes)). npm then stops the publish.

If `pack` cannot check the package at all (a missing or unreadable file, say), it exits `2`, never `0`, so a CI step fails closed already. To make the agent hook (`agentprint guard`) fail closed too, set `AGENTPRINT_STRICT=1` in the job's environment.

## GitHub Action

Add `.github/workflows/agentprint.yml`:

<!-- ci-action:start -->
```yaml
on: [push, pull_request]
permissions: { contents: read, security-events: write }  # security-events: for upload-sarif
jobs:
  agentprint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: adighatakdas-cloud/agentprint@v0.1.1  # better: pin the release's commit SHA
        with: { command: pack, upload-sarif: true }  # command: pack or scan
```
<!-- ci-action:end -->

| Input | Meaning | Default |
|---|---|---|
| `command` | `pack` or `scan` | |
| `path` | Folder to check | `.` |
| `fail-on` | Lowest severity that fails the job. `none` only reports. | `high` |
| `upload-sarif` | Send the [SARIF](/agentprint/glossary/#sarif) file to GitHub code scanning | `false` |

Outputs: `exit-code` and `sarif-file`. To also show a signed CLEAR or BLOCK badge in your README, see [the verifiable badge](/agentprint/badge/).

## pre-commit

Add to `.pre-commit-config.yaml`:

<!-- ci-hooks:start -->
```yaml
repos:
  - repo: https://github.com/adighatakdas-cloud/agentprint
    rev: v0.1.1
    hooks:
      - id: agentprint-pack   # agent files or keys in your package
      - id: agentprint-scan   # keys in this repo's agent settings
```

To run the repository check before each `git push` instead of each commit, use `agentprint-pre-push` in place of `agentprint-scan`, and install the push hook once with `pre-commit install --hook-type pre-push`.

## lefthook

Add to `lefthook.yml`, then run `lefthook install`:

```yaml
pre-push:
  jobs:
    - run: uvx agentprint==0.1.1 scan --no-home
```

## husky

After `npx husky init`, add the check to `.husky/pre-push`:

```bash
echo "uvx agentprint==0.1.1 scan --no-home" > .husky/pre-push
```
<!-- ci-hooks:end -->

Both need [uv](https://docs.astral.sh/uv/). `scan --no-home` checks only this repository, never your home folder, and exits `1` when it finds something at or above `--fail-on`, which stops the push.
