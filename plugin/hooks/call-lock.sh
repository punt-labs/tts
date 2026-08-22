#!/usr/bin/env bash
# UserPromptSubmit — block the human's interactive prompt while a
# Conversation Mode call is active. Business rule: presence of
# .punt-labs/vox/call/call.lock (written by CallLock, punt_vox.voxd.conversation_mode.call_lock)
# means a call is live. Exit 2 blocks submission and shows stderr to the user
# (Claude Code's documented UserPromptSubmit blocking convention).
#
# VOX_CALL_RELAY=1 bypasses the block: set only on the headless `claude -p
# --resume` subprocess ClaudeSessionAttach spawns for the call's own turns
# (src/punt_vox/voxd/conversation_mode/claude_session_attach.py), so the
# call's own traffic is never blocked by the lock it itself holds.
[[ "${VOX_CALL_RELAY:-}" == "1" ]] && exit 0

_stdin=$(cat)
if command -v jq >/dev/null 2>&1; then
  _cwd=$(printf '%s' "$_stdin" | jq -r '.cwd // empty' 2>/dev/null)
else
  _cwd=$(printf '%s' "$_stdin" | grep -oE '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"//;s/"//')
fi
[[ -n "$_cwd" ]] || _cwd="$PWD"

_repo_root=$(git -C "$_cwd" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$_repo_root" ]; then
  _dir="$_cwd"
  while [ ! -f "$_dir/.punt-labs/vox/enabled" ] && [ "$_dir" != "/" ]; do
    _dir=$(dirname "$_dir")
  done
  _repo_root="$_dir"
fi
[ -f "$_repo_root/.punt-labs/vox/enabled" ] || exit 0

_lock_file="$_repo_root/.punt-labs/vox/call/call.lock"
[ -f "$_lock_file" ] || exit 0

echo "A Conversation Mode call is active -- interactive input is paused until the call ends (/call stop)." >&2
exit 2
