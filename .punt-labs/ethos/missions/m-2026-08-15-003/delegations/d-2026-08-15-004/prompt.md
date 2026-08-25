You have mission m-2026-08-15-003 — PR 3 of 3 for bead vox-w3f8, the last one. Run `ethos mission show m-2026-08-15-003` and read the contract before touching anything.

PRs 1 and 2 are merged (`0843ddc`, `33c0868`). Read `docs/provider-authority.md` on main — you wrote it, the operator ratified its decisions, §9 records them all as settled. Implement **§3.6 only**: the `provider_status` wire op, the `mic:status` block, the doctor readiness section, and enable writing a daemon-proposed provider.

WORK IN THE WORKTREE I HAVE PREPARED:

    <repo>/.claude/worktrees/vox-w3f8-pr3

on branch `feat/vox-w3f8-observability`, already at main (`33c0868`). The MAIN working tree is occupied by another agent (`adb`, fixing a launchd session bug) — do not touch it and do not switch its branch. Pin every cwd-resolving tool: `git -C <worktree>`, `uv --project <worktree>`.

THIS IS THE SURFACE WHERE YOUR OWN LESSON APPLIES. `reason` is a closed set — `Literal["ok", "unconfigured", "unknown_provider", "no_credentials", "voxd_unavailable"]`. Both previous PRs shipped a defect of exactly one shape: a boundary that looked complete because everything it named was correct, and nobody checked what it *didn't* name. F3 was a catch clause with no raise site. F4 was omitted from a catch whose comment carefully enumerated F2, F3 and F5. You proposed carrying the enumeration fix into this PR — land it where the `Literal` lives, not in prose that rots, naming which values are producible from which code path.

D1 AS AMENDED: enable writes a provider chosen by **the daemon**, through the new op — not by probing the enabling process's own environment. That distinction is the whole reason the amendment exists: doctor used to check the caller's environment when the daemon's is the one that matters, and the first draft of enable repeated the mistake. If voxd is unreachable, enable writes nothing and says so.

TEST THE NO-DRIFT PROPERTY DIRECTLY. The design's argument is that `require` and `report` cannot disagree because they are one function called two ways. Prove it: for every provider, what `require()` rejects must `report()` as not-ready, and vice versa. That is the claim the whole observability surface rests on.

Two things from the last two PRs that cost real cycles:

Do NOT use `git commit -a`. It stages every modified tracked file, and `.punt-labs/vox/vox.md` is the daemon's live session state — almost always dirty. I swept it into PR 2 that way and a bot caught it before merge. Stage explicit paths.

CI's suppression ratchet is merge-base scoped and does not read the in-tree baseline file; editing that file will not make CI pass. Count on main is 201. And `check-coupling`'s baseline is defective (bead vox-orvz) — a REGRESSED verdict is untrustworthy until you check the base blob.

If this PR needs a FOURTH error module, stop and tell me rather than splitting — three already exist at two classes each, and a fourth means the PY-OO-2 cap is driving the design rather than a domain grouping being chosen.

Keep doing the thing you started on PR 2: state what your evidence does NOT cover alongside what it does. It is the most useful part of your reports.

No push, no PR, no make install, no daemon restart, no bd close.