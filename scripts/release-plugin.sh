#!/usr/bin/env bash
set -euo pipefail

# Prepare plugin for release: swap name to prod, remove -dev commands.
# The tagged commit has only prod artifacts; the marketplace cache clones
# from it.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_JSON="${REPO_ROOT}/plugin/.claude-plugin/plugin.json"
# The dev commands live in the plugin's own commands/ directory, which is what
# session-start.sh and check-skill-permissions.sh both skip *-dev.md from.
# This pointed at .claude/commands/ — the project-local command directory the
# plugin's dev commands were deliberately moved OUT of (9b18a26) — so the
# release prep had stopped stripping dev commands from the shipped surface and
# `find` errored on a directory that no longer exists.
COMMANDS_DIR="${REPO_ROOT}/plugin/commands"
# `find` on a missing directory writes to stderr and exits non-zero, but inside
# the process substitution below that status is discarded: dev_files would stay
# empty, the script would report "No -dev commands found" and go on to commit a
# release that stripped nothing. Refuse here instead of reporting success.
[[ -d "$COMMANDS_DIR" ]] || {
  echo "error: $COMMANDS_DIR missing" >&2
  exit 1
}

# Preflight: abort if repo has uncommitted changes
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -uno)" ]]; then
  echo "Error: repository has uncommitted changes. Commit or stash before running $(basename "$0")." >&2
  exit 1
fi

# Swap plugin name from *-dev to prod
current_name="$(python3 -c "import json; print(json.load(open('${PLUGIN_JSON}'))['name'])")"
prod_name="${current_name%-dev}"

if [[ "$current_name" == "$prod_name" ]]; then
  echo "Plugin name is already '${prod_name}' (no -dev suffix)" >&2
  exit 1
fi

echo "Swapping plugin name: ${current_name} → ${prod_name}"
python3 -c "
import json, pathlib
p = pathlib.Path('${PLUGIN_JSON}')
d = json.loads(p.read_text())
d['name'] = '${prod_name}'
p.write_text(json.dumps(d, indent=2) + '\n')
"

# Remove -dev commands
dev_files=()
while IFS= read -r -d '' f; do
  dev_files+=("$f")
done < <(find "$COMMANDS_DIR" -name '*-dev.md' -print0)

if [[ ${#dev_files[@]} -eq 0 ]]; then
  echo "No -dev commands found — name swap only"
else
  for f in "${dev_files[@]}"; do
    echo "Removing: $(basename "$f")"
  done
  git -C "$REPO_ROOT" rm "${dev_files[@]}"
fi

git -C "$REPO_ROOT" add "$PLUGIN_JSON"
# The org bans --no-verify; hooks run against the release-prep commit like
# any other.
git -C "$REPO_ROOT" commit -m "chore: prepare plugin for release"
