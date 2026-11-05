---
name: Scan round
about: Organize a volunteer scan of recent npm and PyPI packages (organizers only)
title: "Scan round N: npm + PyPI"
labels: scan-round
---

<!-- Organizer: fill in the windows from `python -m tools.ecoscan window npm|pypi`,
     keep the shard count at 10 or change the checklist to match, then delete this comment. -->

**Volunteers: read [docs/VOLUNTEER_SCAN.md](https://github.com/adighatakdas-cloud/agentprint/blob/main/docs/VOLUNTEER_SCAN.md) first.**
Never use or test a key you find. Never post private findings here.

## Round

| | npm | PyPI |
|---|---|---|
| Window | `FROM:TO` | `FROM:TO` |
| Shards | 10 | 10 |
| Tool version (commit) | `abc1234` | `abc1234` |

Per shard: npm about 1 GB and 55 minutes, PyPI about 3.7 GB and 45 minutes (default caps).

```bash
git clone https://github.com/adighatakdas-cloud/agentprint && cd agentprint
git checkout <commit above> && uv sync
uv run python -m tools.ecoscan run npm  --window FROM:TO --shard K/10 --out scans/round-N/npm-K
uv run python -m tools.ecoscan run pypi --window FROM:TO --shard K/10 --out scans/round-N/pypi-K
```

## How to take part

1. **Claim** a free shard below with a comment like `claiming npm 3/10`.
2. **Run** the command above with your shard number.
3. **Post** the contents of `public/summary.json` as a comment here (counts only).
4. **Send private findings privately** if the run found any: GitHub private vulnerability
   reporting (Security tab, "Report a vulnerability") or email adi65das@gmail.com.
   Never paste anything from the `private/` folder here.

## Shards

The organizer ticks a box when its public summary is posted.

### npm

- [ ] npm 1/10 (claimed by: )
- [ ] npm 2/10 (claimed by: )
- [ ] npm 3/10 (claimed by: )
- [ ] npm 4/10 (claimed by: )
- [ ] npm 5/10 (claimed by: )
- [ ] npm 6/10 (claimed by: )
- [ ] npm 7/10 (claimed by: )
- [ ] npm 8/10 (claimed by: )
- [ ] npm 9/10 (claimed by: )
- [ ] npm 10/10 (claimed by: )

### PyPI

- [ ] pypi 1/10 (claimed by: )
- [ ] pypi 2/10 (claimed by: )
- [ ] pypi 3/10 (claimed by: )
- [ ] pypi 4/10 (claimed by: )
- [ ] pypi 5/10 (claimed by: )
- [ ] pypi 6/10 (claimed by: )
- [ ] pypi 7/10 (claimed by: )
- [ ] pypi 8/10 (claimed by: )
- [ ] pypi 9/10 (claimed by: )
- [ ] pypi 10/10 (claimed by: )

## Totals

When all shards are in, the organizer saves each summary and runs
`uv run python -m tools.ecoscan merge round-N-summaries/*.json --out round-N-total.json`,
then posts the merged counts here.
