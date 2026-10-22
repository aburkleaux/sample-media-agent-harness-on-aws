#!/usr/bin/env sh
# The version this build is, from the repo's VERSION file (the NEXT release, MAJOR.MINOR.PATCH).
#
#   main     -> 0.9.0                         a release: tagged v0.9.0, notes in a GitLab Release
#   staging  -> 0.9.0-rc.<pipeline>+<sha>     a candidate for that release (never tagged)
#   anything -> 0.9.0-dev+<sha>               a local or branch build
#
# Bump VERSION in the staging -> main MR: MAJOR for a breaking change (an API route, a deploy parameter, a
# data migration), MINOR for features, PATCH for fixes. CI refuses to deploy main if v<VERSION> is tagged.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
version=$(tr -d ' \t\r\n' < "$root/VERSION")
case "$version" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "VERSION must be MAJOR.MINOR.PATCH, got '$version'" >&2; exit 1 ;;
esac
sha=${CI_COMMIT_SHORT_SHA:-$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown)}
branch=${CI_COMMIT_BRANCH:-}
default=${CI_DEFAULT_BRANCH:-main}
if [ "$branch" = "$default" ]; then
  echo "$version"
elif [ "$branch" = "staging" ]; then
  echo "$version-rc.${CI_PIPELINE_IID:-0}+$sha"
else
  echo "$version-dev+$sha"
fi
