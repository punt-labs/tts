---
description: "Start, transfer, or stop a Conversation Mode call"
argument-hint: "start --script <path> [--session <id>] | transfer [--session <id>] | stop"
allowed-tools: ["Bash(vox call:*)"]
---

# /call command

Conversation Mode: a live voice call between the human and this Claude Code
session, mediated by `voxd`. Unlike `/music` and `/rec`, there is no
`mic:call` MCP tool yet -- it is deferred to a follow-up mission (blocked on
`src/punt_vox/server.py`, currently locked by another open mission). Every
`/call` verb shells out to the `vox call` CLI instead.

## Usage

- `/call start --script <path>` -- start a call, reading scripted turns from
  a JSON Lines file (`{"text": ..., "confidence": ...}` per line). Live
  microphone capture and the real ElevenLabs speech-recognition provider are
  deferred alongside the `mic:call` MCP tool; this is the interim entry
  point while those land.
- `/call start --script <path> --session <id>` -- attach to a specific
  session id instead of letting `vox call start` discover one. Required
  when more than one Claude Code session is active for this directory --
  `vox call start` refuses to guess (verified unsafe in
  `docs/conversation-mode-session-attach-adr.md`).
- `/call transfer [--session <id>]` -- ask the running call to re-attach to
  a different active session, without ending the call.
- `/call stop` -- ask the running call to hang up (FR-2's explicit end).

## Implementation

Parse `$ARGUMENTS` and run the matching `vox call <verb>` command via Bash,
passing `--script`/`--session` through verbatim. `vox call start` blocks in
the foreground for the life of the call; `stop` and `transfer` are
fire-and-forget signals to whichever `vox call start` process is running,
via the cross-process control file
(`src/punt_vox/voxd/conversation_mode/call_control.py`).

While a call is active, `plugin/hooks/call-lock.sh` blocks the human's own
interactive `UserPromptSubmit` -- the call's own turns are relayed by
`vox call start` itself, which sets `VOX_CALL_RELAY=1` on its own process so
the lock never blocks its own traffic.
