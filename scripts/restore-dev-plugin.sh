#!/usr/bin/env bash
set -euo pipefail

# Restore dev plugin state on main after a release tag.
#
# Usage:
#   scripts/restore-dev-plugin.sh [release-prep-commit]
#
# If no argument is given, auto-detects the last "prepare plugin for release"
# commit and restores from its parent.
#
# CONTRACT: This script restores dev-state files and stages them. It does
# NOT commit. The org bans the --no-verify escape hatch this script used to
# pass on its own commit; committing here now means the caller either skips
# hooks or resolves a hook failure mid-release with no re-stamp step to fold
# it into. The caller commits the staged restore with hooks running. See
# pkit-hsyi and punt-kit 462c65d for the sibling fix this mirrors.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_JSON="${REPO_ROOT}/plugin/.claude-plugin/plugin.json"
# Mirrors release-plugin.sh: the dev commands are the *-dev.md files inside the
# plugin's own commands/ directory, not project-local .claude/commands/.
COMMANDS_DIR="plugin/commands"

# Preflight: abort if repo has uncommitted changes
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -uno)" ]]; then
  echo "Error: repository has uncommitted changes. Commit or stash before running $(basename "$0")." >&2
  exit 1
fi

# Determine the release-prep commit to restore from
RELEASE_PREP_COMMIT="${1:-}"
if [[ -z "$RELEASE_PREP_COMMIT" ]]; then
  RELEASE_PREP_COMMIT="$(git -C "$REPO_ROOT" log -n 1 --grep='prepare plugin for release' --pretty=format:%H || true)"
  if [[ -z "$RELEASE_PREP_COMMIT" ]]; then
    echo "Error: could not find a 'prepare plugin for release' commit. Pass a commit or tag as the first argument." >&2
    exit 1
  fi
fi

echo "Restoring dev state from parent of ${RELEASE_PREP_COMMIT:0:12}"
git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- "$PLUGIN_JSON"

# Restore the plugin commands directory if the parent commit had one, bringing
# back whatever *-dev.md files release-plugin.sh removed.
#
# The pathspec carries NO trailing slash: `ls-tree -d -- <dir>/` matches nothing
# (it asks for a tree entry literally named "<dir>/"), so the guard as written
# with a slash was permanently false and this restore never ran -- every release
# put back plugin.json and silently left the dev commands deleted.
restored=false
if git -C "$REPO_ROOT" ls-tree -d "${RELEASE_PREP_COMMIT}^" -- "$COMMANDS_DIR" | grep -q .; then
  git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- "$COMMANDS_DIR"
  restored=true
fi

git -C "$REPO_ROOT" add "$PLUGIN_JSON"
# Staged only when something was actually restored, and UNGUARDED. The add used
# to run unconditionally behind `2>/dev/null || true`, which had to be tolerant
# because the common case was a directory that had never been touched -- and
# that tolerance also swallowed a genuine failure to stage the commands just
# restored, producing a "restore dev plugin state" commit carrying none of them
# and reporting success. Now the only add that runs is one that must succeed.
if [[ "$restored" == true ]]; then
  git -C "$REPO_ROOT" add "$COMMANDS_DIR"
fi
