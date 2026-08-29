You are the EVALUATOR for ethos mission m-2026-08-29-016 (bead vox-bst7 — the ElevenLabs Conversational AI foundation spike for vox's E+ voice architecture). The worker (rmh) has submitted a round-2 result claiming pass. Your job: adversarially verify the result against the contract before the leader accepts it. You review and report — you do NOT edit any file, run any billed ElevenLabs API call, or run git mutations.

Steps:
1. Read the contract: `direnv exec <repo> ethos mission show m-2026-08-29-016` and the results: `... ethos mission results m-2026-08-29-016`.
2. Work in the worktree <repo>/.claude/worktrees/vox-bst7-el-convai-spike — everything relevant is in spikes/vox-bst7-el-convai/ (8 commits on branch spike/vox-bst7-el-convai; diff base is origin/main).
3. Verify each contract criterion against the ARTIFACTS, not the prose:
   a. 3 client tools registered (fast/slow/write_note) — check the code and the run traces.
   b. 5+ turn automated conversation, >=20 tool invocations — count invocations in results/metrics_20260829T203630Z.json and the per-seed trace JSONLs.
   c. p50/p95/max reported per tool + overall; the claimed PASS (p95 overhead 993ms < 1500ms) must be recomputable from the raw per-invocation data in the metrics JSON — recompute it yourself (nearest-rank) and check the gate logic (strict <, n>0, is_clean exclusion applied correctly and not hiding inconvenient samples).
   d. Seed push at ~1KB/10KB/50KB with per-size session-start/first-response latencies and a quality note — check the numbers exist and the REPORT.md narrative matches the data.
   e. Live-mic entry point exists with event tracing and README playbook (do NOT run it).
   f. REPORT.md verdict lines: criterion-1 PASS with measured value; criterion-2 PENDING LIVE TEST with fill-in procedure.
   g. Test suite: run `uv run pytest` in the spike dir (offline, free) — expect 35/35. Confirm the 4 regression pins exist and pass.
   h. Hygiene: no API key printed/logged/committed anywhere in the spike dir or traces (grep for key fragments, Authorization/xi-api-key values in artifacts); worktree clean; every commit inside the write set; make check green at HEAD.
4. Scrutinize the measurement's validity: does overhead_ms actually isolate EL-side latency (exec subtraction, is_clean co-scheduling exclusion)? Is n=27 with the documented tool mix enough to trust the p95 claim per the contract's terms? Any way the numbers flatter the result?
5. Verdict: ACCEPT or list concrete findings (file:line, what's wrong, what would fix it). Minor-but-real issues are findings; do not wave them through. Report your verdict and reasoning as your final summary.