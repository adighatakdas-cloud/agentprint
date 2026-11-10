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

To check each `git push` instead of each commit, use `agentprint-pre-push` in place of `agentprint-scan`, and install the push hook once with `pre-commit install --hook-type pre-push`. It runs `agentprint push-check`, the check `agentprint protect` sets up. For strict mode, add `args: [--strict]` under its id.

## lefthook

Add to `lefthook.yml`, then run `lefthook install`:

```yaml
pre-push:
  commands:
    agentprint:
      use_stdin: true
      run: |
        ap_strict=0; case "${AGENTPRINT_STRICT:-}" in 1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) ap_strict=1 ;; esac
        s=0; out=$(uvx agentprint==0.1.1 push-check --pre-push --remote {1}) || s=$?
        case "$s:$out" in 0:|"0:agentprint: OK") ;; *) printf '%s\n' "$out" >&2 ;; esac
        case "$s:$out:$ap_strict" in 0:*) ;; "1:agentprint: BLOCK"*) exit 1 ;; *:1) echo "agentprint: the push check could not run (exit $s); push blocked (strict mode)" >&2; exit 1 ;; *) echo "agentprint: the push check could not run (exit $s); push allowed" >&2 ;; esac
```

## husky

After `npx husky init`, add these lines to `.husky/pre-push`:

```sh
ap_strict=0; case "${AGENTPRINT_STRICT:-}" in 1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) ap_strict=1 ;; esac
s=0; out=$(uvx agentprint==0.1.1 push-check --pre-push --remote "$1") || s=$?
case "$s:$out" in 0:|"0:agentprint: OK") ;; *) printf '%s\n' "$out" >&2 ;; esac
case "$s:$out:$ap_strict" in 0:*) ;; "1:agentprint: BLOCK"*) exit 1 ;; *:1) echo "agentprint: the push check could not run (exit $s); push blocked (strict mode)" >&2; exit 1 ;; *) echo "agentprint: the push check could not run (exit $s); push allowed" >&2 ;; esac
```
<!-- ci-hooks:end -->

These run `push-check`, the same check `agentprint protect` adds as a git hook (run `agentprint protect` in a repository that uses lefthook or husky and it prints these lines). It reads what the push uploads from git's own objects: the pushed commits, never your working tree, so a gitignored file such as `.claude/settings.local.json` never blocks a push. A finding at or above `--fail-on` stops the push.

The lefthook and husky lines need [uv](https://docs.astral.sh/uv/). These files are committed, so they run for every teammate: where agentprint can't run (no uv), they print one line and the push goes on. To block instead, set `AGENTPRINT_STRICT=1` where they run (in CI, say), or use the strict lines that `agentprint protect --strict` prints. A strict push check blocks any push it can't check, including when agentprint itself can't start.
