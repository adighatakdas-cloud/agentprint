---
title: The verifiable badge
description: Show agentprint CLEAR or BLOCK in your README, with a signed GitHub attestation anyone can check with gh attestation verify.
---

:::note
These steps work after the first release.
:::

The agentprint GitHub Action can write a badge for your README: `agentprint | CLEAR` or `agentprint | BLOCK`. It can also sign the result, so anyone can check where it came from.

## What it shows, and what it proves

The badge shows the verdict of the last run on your default branch: **CLEAR** when nothing is at or above `--fail-on`, **BLOCK** when something is. See the [glossary](/agentprint/glossary/).

Anyone can put any image in a README, so the image alone proves nothing. What can't be faked is the signed proof. The action signs `badge.json` with a GitHub build attestation, and `gh attestation verify` checks the file against it. A passing check proves:

- The file was produced by a workflow run in that repository.
- It is the file that run signed, byte for byte.

It does not prove the repository is safe. It says one scan, with those settings, gave that verdict. It also doesn't say the badge in the README is current. Verify the file, not the picture.

`badge.json` and `verdict.json` hold only the verdict and counts. No paths, no findings, no keys.

## Set it up

Add this to `.github/workflows/agentprint.yml`:

```yaml
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: write       # publish-badge: pushes the agentprint-badge branch
  id-token: write       # attest-badge: signs with GitHub's OIDC identity
  attestations: write   # attest-badge: stores the attestation
jobs:
  agentprint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: adighatakdas-cloud/agentprint@v0.1.0  # better: pin the release's commit SHA
        with:
          command: pack
          badge-dir: agentprint-badge-out
          attest-badge: true
          publish-badge: true
```

| Input | What it does | Permission it needs |
|---|---|---|
| `badge-dir` | Writes `badge.json` and `verdict.json` into this folder. Without it nothing is written. | None |
| `attest-badge: true` | Signs `badge.json`. Needs `badge-dir`. | `id-token: write` and `attestations: write` |
| `publish-badge: true` | Commits both files to the `agentprint-badge` branch. Needs `badge-dir`. | `contents: write` |

The action sets two outputs when the scan ran without an error: `badge-file` and `verdict-file`. Use `publish-badge: dry-run` to see what would be committed. It pushes nothing and needs no permissions.

The `agentprint-badge` branch has one commit with the two files. Each publish replaces it, so the history never grows.

### Publishing only happens on the default branch

The publish step runs only on a `push` to your default branch. On a `pull_request`, including one from a fork, nothing is pushed and no write token is used. The log says the step was skipped. The scan still runs and still fails the job as usual.

## Add it to your README

Replace `OWNER/REPO` with your repository:

```markdown
[![agentprint](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/OWNER/REPO/agentprint-badge/badge.json)](https://github.com/OWNER/REPO/attestations)
```

The image comes from [shields.io](https://shields.io/endpoint), which reads `badge.json` from the `agentprint-badge` branch. The link opens your repository's attestations page, where the signed proof lives.

## Verify a badge

You need the [GitHub CLI](https://cli.github.com/). Replace `OWNER/REPO` with the repository you are checking.

1. Download the file from the branch:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/OWNER/REPO/agentprint-badge/badge.json
   ```

2. Verify it:

   ```bash
   gh attestation verify badge.json -R OWNER/REPO --predicate-type https://adighatakdas-cloud.github.io/agentprint/badge/v1
   ```

3. Read the result. On success `gh` prints the workflow that produced the file. If it fails, the file wasn't signed by that repository's workflow, and you shouldn't trust the badge.

## Private repositories

The files are still written, so `badge-dir` and `publish-badge` work. The attestation is skipped with a notice, because build attestations need a public repository. The job doesn't fail. A private repository's `agentprint-badge` branch is not publicly visible either, so the README badge won't render for people without access.
