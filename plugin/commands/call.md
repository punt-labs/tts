---
description: "Start, transfer, or stop a Conversation Mode call"
argument-hint: "start [--script <path>] [--session <id>] | transfer [--session <id>] | stop"
allowed-tools: ["Bash(vox call:*)"]
---

# /call command

Conversation Mode: a live voice call between the human and this Claude Code
session, mediated by `voxd`. Unlike `/music` and `/rec`, there is no
`mic:call` MCP tool yet -- every `/call` verb shells out to the `vox call`
CLI instead.

## Usage

- `/call start` -- start a real call: real microphone capture, transcribed
  by ElevenLabs. This is the primary way to place a call.
- `/call start --script <path>` -- dev/test path: read scripted turns from a
  JSON Lines file (`{"text": ..., "confidence": ...}` per line) instead of
  the microphone -- no hardware, no ElevenLabs credentials, no network. For
  demos and CI.
- `/call start [--script <path>] --session <id>` -- attach to a specific
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
interactive `UserPromptSubmit`, self-clearing if the holding process has
died. The escape hatch is never blocked by its own lock: `/call stop` and
`/call transfer` prompts pass the hook unconditionally, and the call's own
turns -- relayed through a `claude -p --resume` subprocess
(`src/punt_vox/voxd/conversation_mode/claude_session_attach.py`) -- carry
`VOX_CALL_RELAY=1` in that subprocess's own environment, set by the process
that spawns it, so the lock the call holds for the *human's* input never
blocks its own traffic.
