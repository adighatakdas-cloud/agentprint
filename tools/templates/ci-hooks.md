```yaml
repos:
  - repo: https://github.com/adighatakdas-cloud/agentprint
    rev: v{{version}}
    hooks:
      - id: agentprint-pack   # agent files or keys in your package
      - id: agentprint-scan   # keys in this repo's agent settings
```

To check each `git push` instead of each commit, use `agentprint-pre-push` in place of `agentprint-scan`, and install the push hook once with `pre-commit install --hook-type pre-push`. It runs `agentprint push-check`, the check `agentprint protect` sets up. For strict mode, add `args: [--strict]` under its id.

## lefthook

Add to `lefthook.yml`, then run `lefthook install`:

```yaml
{{lefthook}}
```

## husky

After `npx husky init`, add these lines to `.husky/pre-push`:

```sh
{{husky}}
```
