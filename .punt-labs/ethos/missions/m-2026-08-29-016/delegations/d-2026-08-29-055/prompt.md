You are the worker for ethos mission m-2026-08-29-016 (bead vox-bst7, the ElevenLabs Conversational AI foundation spike for the E+ voice architecture).

How to proceed:
1. Read your full contract: run `direnv exec <repo> ethos mission show m-2026-08-29-016`. The success_criteria and context there are authoritative — this prompt is only the invocation.
2. Work exclusively in the worktree <repo>/.claude/worktrees/vox-bst7-el-convai-spike (branch spike/vox-bst7-el-convai, already checked out). Your write set is spikes/vox-bst7-el-convai/ within it. Do not touch the main checkout, do not create branches, do not push, do not open PRs.
3. Read DESIGN.md entries DES-068 and DES-069 (around line 3395) in the worktree before writing code.
4. Prefix shell commands that need repo env (API keys, bd, ethos) with `direnv exec <dir>` — plain shells do not have the .envrc environment.
5. Commit incrementally on the spike branch; each commit must pass `make check` (your spike dir is outside src/, so this should stay green — if it doesn't, fix it, never suppress).
6. When the automated harness runs end-to-end with real measured numbers in REPORT.md, submit via `direnv exec <repo> ethos mission result m-2026-08-29-016` (see `--help` for flags), citing the metrics JSON and REPORT.md paths.

One task: build and run the spike harness per the contract. The live barge-in/turn-taking session with the operator happens after your mission — you only build and document that entry point.