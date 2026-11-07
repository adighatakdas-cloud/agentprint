```yaml
repos:
  - repo: https://github.com/adighatakdas-cloud/agentprint
    rev: v{{version}}
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
    - run: uvx agentprint=={{version}} scan --no-home
```

## husky

After `npx husky init`, add the check to `.husky/pre-push`:

```bash
echo "uvx agentprint=={{version}} scan --no-home" > .husky/pre-push
```
