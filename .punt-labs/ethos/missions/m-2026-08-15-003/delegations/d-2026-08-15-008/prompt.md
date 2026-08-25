You wrote vox PR #426 (bead vox-w3f8, PR 3 of 3 — per-provider readiness). The prior session died to an API stream timeout before starting this round; the working tree is clean, nothing was lost. Three Copilot findings to fix.

WORK IN: <repo>/.claude/worktrees/vox-w3f8-pr3, branch `feat/vox-w3f8-observability`, currently at d0ecab9. Do NOT touch the main working tree. Pin every cwd-resolving tool: `git -C <worktree>`, `uv --project <worktree>`. Read the diff against main to reload context — you wrote all of it.

All three findings are the same class this PR exists to eliminate. That is the interesting part, so read them in that light.

FINDING 1 — `src/punt_vox/types_provider.py:54`. The producer enumeration is itself under-enumerated.

The comment claims `voxd_unavailable` is produced only by `server._provider_status_block` on `_DAEMON_ERRORS`. That same function ALSO returns `voxd_unavailable` when the daemon replies without a matching provider row — the protocol-bug path. Two producers, one claimed.

The shape of this bead's defects, three for three: F3 was a catch clause with no raise site. F4 was omitted from a catch whose comment carefully enumerated F2/F3/F5. Now the producer map — added specifically to stop under-enumeration — under-enumerates. Prose adjacent to a Literal cannot be trusted to stay true, which is exactly why you added the runtime frozenset.

FINDING 2 — `src/punt_vox/enablement.py:96`. A DESIGN BUG, and the leader has ruled: add a distinct reason.

`EnableOutcome`'s docstring says `voxd_unavailable` means the `provider_status` op could not be reached. But `ProviderProposal.propose_and_write()` also returns `voxd_unavailable` when writing `provider` to `vox.md` fails with `OSError`.

Those are different failures. One means the daemon is down; the other means the local disk write failed while the daemon answered correctly. Reporting a write failure as "voxd unavailable" sends the user to restart a healthy daemon — confidently wrong, the same defect PR 2 fixed when a rate limit was being misclassified as an auth failure.

Add a DISTINCT reason for the local write failure. Do NOT widen the docstring to cover both — that documents the conflation instead of removing it. Yes, this means a new value landing in all four places (Literal, frozenset, producer, render branches). That is the machinery working, not a cost. Give the detail enough to act on — the path and the OSError text; "could not write" without the path is another dead end.

FINDING 3 — `src/punt_vox/enablement.py:71`. The `_DAEMON_ERRORS` comment claims it mirrors server.py's tuple; server.py also includes `WebSocketException` and this one does not. Behaviourally fine — `VoxClient` wraps `WebSocketException` into `VoxdConnectionError` — but the comment is wrong, and a claim of parity where there is none sends someone to fix a non-bug. Either state why it legitimately differs or make it actually mirror.

A QUESTION I WANT YOUR REAL READ ON, not a reflexive yes. Given the producer-comment approach failed on its first outing: is there a mechanical form for the producer claim — a test asserting each reason is reachable from exactly the producers named, or a registry producers must go through? If there is a clean one, propose it and I will scope it. If prose is genuinely the only practical option, say so plainly and we keep it with sharper review. Answer in your report; do not build it unprompted.

CONSTRAINTS. `make check` green before every commit — no noqa, no type: ignore, no xfail. Do NOT use `git commit -a` (it sweeps `.punt-labs/vox/vox.md`, the daemon's live state; that happened on PR 2 and a bot caught it). Suppression count on main is 201 and CI's ratchet is merge-base scoped, so editing the in-tree baseline will not make CI pass. check-coupling's baseline is defective (bead vox-orvz) — a REGRESSED verdict is untrustworthy until you check the base blob, and label relaxations Class A (sentinel correction) vs Class B (real growth) distinctly.

No push, no PR, no `make install`, no daemon restart, no bd close. Report with commit SHAs, and keep stating what your evidence does not cover — it has been the most useful part of your reports.