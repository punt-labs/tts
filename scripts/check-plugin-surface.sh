#!/usr/bin/env bash
# Verify the shippable plugin surface does not reach outside itself.
#
# A marketplace install fetches ONLY the surface directory (Claude Code's
# git-subdir source is a blobless clone plus `sparse-checkout set --cone
# plugin`), so any ${CLAUDE_PLUGIN_ROOT}-relative path that resolves outside it
# — or to a file that simply is not there — is a SILENT break: the hook or
# command runs, finds nothing, and the feature is quietly absent on every
# installed copy while working perfectly in the source tree. This gate is the
# reason that cannot happen twice. Fast (no network; standard shell utilities).
#
# Usage: check-plugin-surface.sh [surface-dir]   (default: <repo>/plugin)
set -euo pipefail
shopt -s nullglob

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SURFACE="${1:-$ROOT/plugin}"

if [[ ! -d "$SURFACE" ]]; then
  echo "error: plugin surface not found at $SURFACE" >&2
  exit 2
fi
SURFACE="$(cd "$SURFACE" && pwd -P)"

# Every plugin-root placeholder the surface can use to address its own files:
# ${CLAUDE_PLUGIN_ROOT} is what Claude Code substitutes in hooks.json, and
# PLUGIN_ROOT is what a script derives from its own location for the same job.
_PLACEHOLDER='\$\{?(CLAUDE_)?PLUGIN_ROOT\}?/'

# The trailing class is a POSITIVE one -- the characters a path can be made of
# -- not a list of terminators. Enumerating terminators means every character
# forgotten is a false positive: an unquoted `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh`
# would be extracted with the glob attached, `-e` on that literal is false, and
# the gate would fail correct code while claiming the surface does not ship it.
# Matching only path characters stops at a quote, whitespace, `$`, and every
# glob metacharacter at once, leaving the directory prefix -- which is the part
# that can actually be verified, since a glob's matches cannot be checked
# statically. `-` is last in the class so it is a literal, not a range.
#
# `while read` rather than mapfile for Bash 3.2 (stock macOS).
refs=()
while IFS= read -r ref; do
  refs+=("$ref")
done < <(grep -rhoE "${_PLACEHOLDER}[A-Za-z0-9._/-]*" "$SURFACE" | sort -u)

# Fail closed on extraction rot: hooks.json always registers its scripts through
# the placeholder, so matching nothing there means the pattern above stopped
# working, not that the surface got clean. Without this the gate would pass
# vacuously the moment someone reformatted the file.
if [[ -f "$SURFACE/hooks/hooks.json" ]] &&
  ! grep -qE "${_PLACEHOLDER}" "$SURFACE/hooks/hooks.json"; then
  echo "error: no plugin-root references found in hooks/hooks.json —" >&2
  echo "       either the hook registration is broken or this script's" >&2
  echo "       extraction pattern no longer matches it. Fix before relying" >&2
  echo "       on this gate." >&2
  exit 2
fi

status=0
for ref in "${refs[@]}"; do
  # Strip the placeholder prefix and any trailing slash, leaving a path
  # relative to the surface root.
  rel="${ref#*PLUGIN_ROOT}"
  rel="${rel#\}}"
  rel="${rel#/}"
  rel="${rel%/}"
  [[ -n "$rel" ]] || continue

  if [[ "/$rel/" == */../* ]]; then
    echo "error: reference escapes the plugin surface: $ref" >&2
    status=1
    continue
  fi
  if [[ ! -e "$SURFACE/$rel" ]]; then
    echo "error: reference points at a path the surface does not ship: $ref" >&2
    status=1
    continue
  fi
  # A hook Claude Code invokes as a command must be executable in the installed
  # copy; git carries the mode bit, so a non-executable script here ships broken.
  if [[ "$rel" == *.sh && ! -x "$SURFACE/$rel" ]]; then
    echo "error: hook script is not executable: $ref" >&2
    status=1
  fi
done

if [[ $status -eq 0 ]]; then
  echo "plugin-surface: ${#refs[@]} plugin-root reference(s) — all resolve inside $(basename "$SURFACE")/"
fi
exit $status
