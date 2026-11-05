# Releasing agentprint

A release is one command: push a version tag such as `v0.1.0`. GitHub Actions
(`.github/workflows/release.yml`) then:

1. checks the tag matches the version in `agentprint/__init__.py`, and that the npm name is free
   or already ours;
2. runs every test, builds the Python package, and runs agentprint on its own release files;
3. publishes to **PyPI** (so `uvx agentprint` works);
4. publishes the npm wrapper to **npm** (so `npx agentprint-cli` works). npm goes second because the
   wrapper downloads the PyPI version.

No PyPI password or token is stored anywhere. PyPI and npm both use **trusted publishing**: the
registry is told once "accept uploads from this repository's `release.yml`", and GitHub proves
who is uploading each time.

## One-time setup (owner)

Do these once, before the first tag. Step 2 is a decision, the rest are clicks.

1. **npm name: `agentprint-cli`.** `agentprint` on npm belongs to an unrelated project
   (registered 26 Oct 2026), so the npm wrapper is published as `agentprint-cli`. The command
   people type after installing is still `agentprint`; one-off use is `npx agentprint-cli pack`.
   The PyPI name stays `agentprint` (free on 5 Nov 2026 — claim it with the first release).
2. **Make the repository public first.** npm provenance (the "built by this GitHub repo" badge)
   only works for public repositories, so the first release happens after the repo goes public.
3. **PyPI.** Sign in at pypi.org (turn on two-factor authentication). Go to *Your account →
   Publishing → Add a new pending publisher → GitHub* and enter:
   - PyPI project name: `agentprint`
   - Owner: `adighatakdas-cloud`
   - Repository name: `agentprint`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   "Pending" means the project is created by the first upload. It does not reserve the name,
   so do the first release soon after.
4. **GitHub environments.** In the repo: *Settings → Environments → New environment*, create
   `pypi` and then `npm`. For each one:
   - *Deployment branches and tags → Selected branches and tags → Add rule → Tag* `v*`, so only
     version tags can publish;
   - *Required reviewers → you* (recommended): every publish then waits for your click on the
     Actions page.
5. **Protect version tags.** *Settings → Rules → Rulesets → New tag ruleset*: target `v*`,
   turn on *Restrict creations*, *Restrict updates* and *Restrict deletions*, and put yourself
   (Repository admin) in the bypass list. Now only you can create a release tag.
6. **npm.** Sign in at npmjs.com (turn on two-factor authentication). npm's trusted publishing is
   set up from the package's own settings page, which only exists after a first version, so the
   first release uses a short-lived token:
   1. *npmjs.com → your avatar → Access Tokens → Generate New Token → Granular Access Token*:
      read and write for packages, the shortest expiry offered (7 days is plenty). If npm asks,
      allow it to publish without a 2FA prompt, because CI can't type a code.
   2. In the repo: *Settings → Environments → npm → Add environment secret*, name `NPM_TOKEN`,
      paste the token.
   3. After the first release succeeds: on npmjs.com open the package → *Settings → Trusted
      publishing → GitHub Actions* and enter owner `adighatakdas-cloud`, repository
      `agentprint`, workflow `release.yml`, environment `npm`. Then delete the `NPM_TOKEN`
      secret on GitHub and the token on npmjs.com. Optionally choose *Require two-factor
      authentication and disallow tokens* in the package settings. From then on no token
      exists.
7. **Optional:** *Settings → Code security → Private vulnerability reporting → Enable*, so people
   can also report problems privately through GitHub (`SECURITY.md` gives the email).

## Every release

1. Pick the version, for example `0.1.0`. Put it in two places:
   - `agentprint/__init__.py`: `__version__ = "0.1.0"`
   - `npm/package.json`: `"version": "0.1.0"`

   Pre-releases are spelled differently: Python `0.2.0rc1` is npm `0.2.0-rc.1` (`a1` →
   `-alpha.1`, `b1` → `-beta.1`, `.dev0` → `-dev.0`). A test fails if the two don't match.
   npm pre-releases are published under the `next` tag, so `npx agentprint-cli` never picks them.
2. Merge that change into `main` through a pull request, and wait for CI to pass.
3. Tag the merged commit and push the tag:

   ```bash
   git switch main && git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. Watch *Actions → Release*. Approve the `pypi` and `npm` steps if you set reviewers.
5. Check it worked: `uvx agentprint==0.1.0 --version` and `npx agentprint-cli@0.1.0 --version`
   (use the npm name you chose).
6. Optional: on GitHub, *Releases → Draft a new release* from the tag. Tick *Publish this Action
   to the GitHub Marketplace* to list the Action there.

## When something fails

- **Before PyPI** (tag check, tests, npm name): nothing was published. Fix it, delete the tag
  (`git push origin :v0.1.0 && git tag -d v0.1.0`), and tag again.
- **PyPI upload failed**: nothing reached npm. Check the trusted publisher details match exactly
  (owner, repo, `release.yml`, `pypi`). Re-run the failed job from the Actions page.
- **PyPI worked, npm failed**: fix the npm side (token, trusted publisher details, name) and
  re-run only the failed `npm` job. Don't re-tag: PyPI never accepts the same version twice.
  If the PyPI release itself is broken, bump to the next version.
