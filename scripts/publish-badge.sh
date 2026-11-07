#!/usr/bin/env bash
# Publish badge.json and verdict.json to the orphan branch `agentprint-badge`: one commit,
# force-updated. Works in a temporary directory, never in the caller's checkout.
#
# Environment:
#   AP_BADGE_DIR    folder holding badge.json and verdict.json (required)
#   AP_PUBLISH_MODE "true" pushes, "dry-run" only prints what it would do (required)
#   AP_REPOSITORY   owner/name, used to build the remote URL
#   AP_REMOTE       overrides the remote URL (tests use a local bare repository)
#   AP_TOKEN        GitHub token; sent as an Authorization header, never in a URL
set -euo pipefail

branch="agentprint-badge"
dir="${AP_BADGE_DIR:?AP_BADGE_DIR is required}"
mode="${AP_PUBLISH_MODE:?AP_PUBLISH_MODE is required}"
remote="${AP_REMOTE:-https://github.com/${AP_REPOSITORY:-}.git}"
token="${AP_TOKEN:-}"

[ -n "$token" ] && echo "::add-mask::$token"
for f in badge.json verdict.json; do
  [ -f "$dir/$f" ] || { echo "::error title=agentprint::$dir/$f is missing"; exit 2; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$dir/badge.json" "$dir/verdict.json" "$work/"
cd "$work"
git init --quiet --initial-branch="$branch"
git add badge.json verdict.json
git -c user.name="github-actions[bot]" \
  -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "agentprint badge"

if [ "$mode" = "dry-run" ]; then
  echo "agentprint: dry-run, would commit these files to the orphan branch $branch and force-push (nothing pushed):"
  git show --stat --format= HEAD
  exit 0
fi

# Only the pushing path gets credentials; the header is masked in the log.
auth=()
if [ -n "$token" ]; then
  header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')"
  echo "::add-mask::${header#AUTHORIZATION: basic }"
  auth=(-c "http.extraheader=$header")
fi
git ${auth[@]+"${auth[@]}"} push --quiet --force "$remote" "HEAD:refs/heads/$branch"
echo "agentprint: published the badge to $branch"
