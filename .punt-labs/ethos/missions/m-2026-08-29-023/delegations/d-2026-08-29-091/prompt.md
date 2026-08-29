You are the worker for ethos mission m-2026-08-29-023 (bead vox-bst7 — automated barge-in state-integrity adjudication for the ElevenLabs Conv AI spike).

How to proceed:
1. Read your contract: `direnv exec <repo> ethos mission show m-2026-08-29-023`. Its success_criteria and context are authoritative.
2. Work exclusively in <repo>/.claude/worktrees/vox-bst7-el-convai-spike (branch spike/vox-bst7-el-convai, already checked out; write set spikes/vox-bst7-el-convai/). No branches, no push, no PR, and do NOT tear down the live agent.
3. The worktree's own .envrc is direnv-blocked — prefix env-needing commands with `direnv exec <repo>` (same env).
4. Offline-first: extend the MockElServer dry-run to rehearse the barge-in flow before any billed run; hard cap of 3 billed runs total.
5. One task: adjudicate kill criterion 2 with machine evidence and fill REPORT.md's verdict line, honestly labeled as an automated synthesized-voice test. Submit via `direnv exec <repo> ethos mission result m-2026-08-29-023` citing the trace evidence.