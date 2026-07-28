#!/usr/bin/env bash
# Stop hook — pipe stdin to the vox subprocess.
# Business logic lives in src/punt_vox/hooks.py (handle_stop).
_stdin=$(cat)
if command -v jq >/dev/null 2>&1; then
  _cwd=$(printf '%s' "$_stdin" | jq -r '.cwd // empty' 2>/dev/null)
else
  _cwd=$(printf '%s' "$_stdin" | grep -oE '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"//;s/"//')
fi
[[ -n "$_cwd" ]] || _cwd="$PWD"

# Per-repo opt-in: silent unless this repo ran `vox enable` and vox is installed.
_repo_root=$(git -C "$_cwd" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$_repo_root" ]; then
  # git absent/failed: walk parents from cwd for the marker so a subdir of an
  # enabled repo still opts in (git's toplevel is unavailable here).
  _dir="$_cwd"
  while [ ! -f "$_dir/.punt-labs/vox/enabled" ] && [ "$_dir" != "/" ]; do
    _dir=$(dirname "$_dir")
  done
  _repo_root="$_dir"
fi
[ -f "$_repo_root/.punt-labs/vox/enabled" ] || exit 0
command -v vox >/dev/null 2>&1 || exit 0

# Warnings ship to vox.log via the daemon; hook stderr is discarded by Claude Code.
printf '%s' "$_stdin" | vox hook stop 2>/dev/null || true
