---
title: Use it in CI
description: Stop a leaky npm publish with prepublishOnly, run agentprint as a GitHub Action, or add it to pre-commit.
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

## GitHub Action

Add `.github/workflows/agentprint.yml`:

```yaml
on: [push, pull_request]
permissions: { contents: read, security-events: write }  # security-events: for upload-sarif
jobs:
  agentprint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: adighatakdas-cloud/agentprint@v0.1.0  # better: pin the release's commit SHA
        with: { command: pack, upload-sarif: true }  # command: pack or scan
```

| Input | Meaning | Default |
|---|---|---|
| `command` | `pack` or `scan` | |
| `path` | Folder to check | `.` |
| `fail-on` | Lowest severity that fails the job. `none` only reports. | `high` |
| `upload-sarif` | Send the [SARIF](/agentprint/glossary/#sarif) file to GitHub code scanning | `false` |

Outputs: `exit-code` and `sarif-file`.

## pre-commit

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/adighatakdas-cloud/agentprint
    rev: v0.1.0
    hooks:
      - id: agentprint-pack   # agent files or keys in your package
      - id: agentprint-scan   # keys in this repo's agent settings
```
