---
title: The verifiable badge
description: Show agentprint CLEAR or BLOCK in your README, with a signed GitHub attestation anyone can check with gh attestation verify.
---

:::note
These steps work after the first release. The signed-by-agentprint mode works from v0.1.0, because it installs the released agentprint from PyPI.
:::

The agentprint GitHub Action can write a badge for your README: `agentprint | CLEAR` or `agentprint | BLOCK`. It can also sign the result, so anyone can check which workflow signed it.

## What it shows, and what it proves

The badge shows the verdict of the last run on your default branch: **CLEAR** when nothing is at or above `--fail-on`, **BLOCK** when something is. See the [glossary](/agentprint/glossary/).

Anyone can put any image in a README, so the image alone proves nothing. The action signs `badge.json` with a GitHub build attestation, and `gh attestation verify` checks the file against it. A passing check, bound to the current commit as shown [below](#verify-a-badge), proves:

- This exact badge file was signed by a workflow in that repository.
- It was signed on the default branch, at the commit you name.
- agentprint's verdict is attached to the signature.

It does not prove the workflow ran agentprint unchanged. The repository owner controls that workflow, so read the workflow file in the repository. It does not prove the repository is safe either: one scan, with those settings, gave that verdict. An old CLEAR file is byte-identical to a new one, so a check that doesn't name the current commit can pass on a stale CLEAR. Always bind the check to the current commit.

`badge.json` and `verdict.json` hold only the verdict and counts. No paths, no findings, no keys.

## Signed by agentprint (recommended)

Your workflow calls a reusable workflow that lives in the agentprint repository. It checks out your repository, installs the released agentprint from PyPI, scans, and signs the result. Because the signature names agentprint's own workflow file, a passing check proves that agentprint's own steps, which you cannot edit, produced the verdict.

Add this to `.github/workflows/agentprint.yml`:

```yaml
on:
  push:
    branches: [main]
permissions: {}
jobs:
  badge:
    permissions:
      contents: write       # pushes the agentprint-badge branch
      id-token: write       # signs with GitHub's OIDC identity
      attestations: write   # stores the attestation
    uses: adighatakdas-cloud/agentprint/.github/workflows/badge.yml@v0.1.0  # better: pin the release's commit SHA
    with:
      command: pack
```

The inputs are `command` (`pack` or `scan`, default `pack`), `path` (relative to your repository, default `.`) and `fail-on` (default `high`). `fail-on: none` is refused: a report-only run must never publish a signed CLEAR. Like the other mode, it only signs and publishes on a push to the default branch of a public repository.

In this mode `verdict.json` also holds the `path` that was scanned (relative) and the `command`, so a verifier can see the whole repository was checked and not an empty folder. They are public repositories and the path is relative, so nothing private is added. The other mode leaves them out.

A passing check, bound to the current commit, proves:

- agentprint's own workflow file, at the release you pinned, signed this exact badge file.
- It was signed on the default branch, at the commit you name.
- The verdict, with `path`, `command`, `fail_on` and `version`, is attached to the signature.

It does not prove the repository is safe: one scan, with those settings, gave that verdict. An old CLEAR file is byte-identical to a new one, so always bind the check to the current commit. The `pack` command runs your project's own build, so project code runs in that job.

Verify it with the commands [below](#verify-a-badge), using the signer workflow `adighatakdas-cloud/agentprint/.github/workflows/badge.yml`.

## Signed by your own workflow

Add this to `.github/workflows/agentprint.yml`:

```yaml
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions: {}
concurrency:
  group: agentprint-badge
  cancel-in-progress: true
jobs:
  agentprint:
    runs-on: ubuntu-latest
    permissions:
      contents: write       # publish-badge: pushes the agentprint-badge branch
      id-token: write       # attest-badge: signs with GitHub's OIDC identity
      attestations: write   # attest-badge: stores the attestation
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: adighatakdas-cloud/agentprint@v0.1.0  # better: pin the release's commit SHA
        with:
          command: pack
          badge-dir: ${{ runner.temp }}/agentprint-badge
          attest-badge: true
          publish-badge: true
```

Keep this workflow push-only. `pack` runs your project's own build (`uv build` for a Python project), so project code runs in this job, and every step in it can use the write and signing permissions. To check pull requests, add a separate `pull_request` workflow that uses the action without the badge inputs and with `contents: read`.

| Input | What it does | Permission it needs |
|---|---|---|
| `badge-dir` | Writes `badge.json` and `verdict.json` into this folder. Without it nothing is written. | None |
| `attest-badge: true` | Signs `badge.json`. Needs `badge-dir`. | `id-token: write` and `attestations: write` |
| `publish-badge: true` | Commits both files to the `agentprint-badge` branch. Needs `badge-dir`. | `contents: write` |

The action sets two outputs when the scan ran without an error: `badge-file` and `verdict-file`. Use `publish-badge: dry-run` to see what would be committed. It pushes nothing and needs no permissions.

The `agentprint-badge` branch has one commit with the two files. Each publish replaces it, so the history never grows.

### Signing and publishing only happen on the default branch

The sign and publish steps run only on a `push` to your default branch. On any other event, nothing is signed, nothing is pushed and no write token is used. The log says the steps were skipped. This is a safety measure for your repository. It is not something a verifier can see, so the verify command below checks the branch itself.

### Report-only runs never publish a badge

With `fail-on: none` the action still writes the badge files, but it skips signing and publishing and logs a warning. A run set up never to block must not publish a signed CLEAR.

### The signed record is public and permanent

Signing a public repository sends the verdict and counts, with the repository, commit and workflow, to a public transparency log for good. A BLOCK badge publicly flags the repository. If a key may still be live, rotate it first. The signed record cannot be taken back.

## Add it to your README

Replace `OWNER/REPO` with your repository:

```markdown
[![agentprint](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/OWNER/REPO/agentprint-badge/badge.json)](https://github.com/OWNER/REPO/attestations)
```

The image comes from [shields.io](https://shields.io/endpoint), which reads `badge.json` from the `agentprint-badge` branch. The link opens your repository's attestations page, where the signed proof lives.

## Verify a badge

You need the [GitHub CLI](https://cli.github.com/), signed in with `gh auth login`. Replace `OWNER/REPO` with the repository you are checking, `main` with its default branch, and `agentprint.yml` with the name of its badge workflow file.

1. Get the current commit of the default branch:

   ```bash
   git ls-remote https://github.com/OWNER/REPO refs/heads/main
   ```

   The first column is the commit. Call it `<commit>`.

2. Download the file from the branch:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/OWNER/REPO/agentprint-badge/badge.json
   ```

3. Verify it against that commit and that workflow, and print what was signed:

   ```bash
   gh attestation verify badge.json -R OWNER/REPO \
     --predicate-type https://adighatakdas-cloud.github.io/agentprint/badge/v1 \
     --source-ref refs/heads/main \
     --source-digest <commit> \
     --signer-workflow OWNER/REPO/.github/workflows/agentprint.yml \
     --format json --jq '.[].verificationResult.statement.predicate'
   ```

   For a badge signed by agentprint, use `--signer-workflow adighatakdas-cloud/agentprint/.github/workflows/badge.yml` instead. Then read `path`, `command`, `fail_on` and `version` in the printed predicate: `path` should be `.` (or the folder you expect) and `command` should be the one you expect.

4. Read the result. If the command fails, the file wasn't signed for that commit by that workflow, and you shouldn't trust the badge. If it passes, look at the printed verdict and at `fail_on`. A `fail_on` of `none` means the run was set up never to block.

For a badge signed by your own workflow, a passing check does not prove the workflow ran agentprint unchanged. Read the workflow file at that commit to see what it does.

## Private repositories

The files are still written, so `badge-dir` and `publish-badge` work. The attestation is skipped with a notice, because build attestations need a public repository (private and internal repositories need GitHub Enterprise Cloud). The job doesn't fail. A private repository's `agentprint-badge` branch is not publicly visible either, so the README badge won't render for people without access.
