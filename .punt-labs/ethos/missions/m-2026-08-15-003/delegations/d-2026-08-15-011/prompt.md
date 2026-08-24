Bead vox-prfr in <repo>. Run `bd show vox-prfr` first — I rewrote it today and the description is current.

THE DEFECT. `.punt-labs/vox/CLAUDE.md` is the agent guide vox deposits into a repo, and it is `@`-imported by the repo's own CLAUDE.md — so it is what every agent working in that repo actually reads. It is deposited from `src/punt_vox/assets/global-guidance.md` by `mic:enablement action="enable"` / `vox enable`. Nothing detects when the deposited copy has fallen behind the packaged source.

This is not hypothetical. I found it live this morning: the deposited copy still advertised the retired `mic:who` tool, the retired `mic:notify "n"` level, and `/vox model`, and was missing 23 lines of current guidance. The packaged source was correct the entire time — only the deposited copy had rotted. Agents in this repo were reading documentation for a tool that does not exist and acting on it. I re-deposited and the files are now byte-identical, but that only happened because I went looking.

WHAT TO BUILD. A staleness check with a client-observable answer:

1. Stamp the deposited guide with the identity of the source it came from — a content hash of the packaged asset is the honest choice, since a version string goes stale when the asset changes within a release. Put it where it survives a round trip and does not corrupt the markdown for a human reader.
2. Compare deposited against packaged, and surface divergence through `vox doctor` as a real check line alongside the existing ones. This repo's standard is that anything a client cares about reaches the client through the API — never only a log. Read the deposited stamp, hash the packaged asset, report agreement, divergence, or absent-stamp distinctly. "Absent stamp" is its own case: every guide deposited before this change has no stamp, and that must read as "unknown, re-deposit" rather than as a false pass.
3. A repo with vox not enabled has no deposited guide at all — that is not a failure, it is not applicable, and doctor must say so rather than error.

DO NOT write a migration, a legacy-format detector, or a compat path for unstamped guides. Punt Labs products have no installed base to migrate. The unstamped case is a REPORTED STATE, not a code path to bridge — report it and tell the user to re-run enable. That distinction matters; get it wrong and I will strike it.

ALSO IN SCOPE, small: `src/punt_vox/server_switches.py:302` describes "the current ``mic:who`` payload". `mic:who` is retired. `cascade.py:48` and `server.py:572` both say "retired" correctly — this comment is the outlier. Fix the wording; do not change behaviour.

BRANCH: `fix/vox-prfr-guide-staleness` off current main (9b9eaba), in the main working tree at <repo>. Pin every cwd-resolving tool — `git -C`, `uv --project`; that has bitten several workers here.

There is an UNCOMMITTED change already in the tree you must keep: `.punt-labs/vox/CLAUDE.md` is the freshly re-deposited guide. Commit it as part of your work — do NOT revert it, and do NOT `git commit -a` (that sweeps `.punt-labs/vox/vox.md`, the daemon's live session state; it leaked into a PR that way last night). Stage explicit paths.

QUALITY. `make check` green before every commit — no `noqa`, no `type: ignore`, no `xfail`. Tests lead. Two live hazards: `check-coupling`'s baseline is defective (bead vox-orvz — 171 of 264 files carry a sentinel), so a REGRESSED verdict is untrustworthy until you check the base blob, and label relaxations Class A (sentinel correction) vs Class B (real growth) distinctly. CI's suppression ratchet is merge-base scoped and does not read the in-tree baseline; count on main is 201.

No push, no PR, no `make install`, no daemon restart, no bd close. Report with commit SHAs, and state what your evidence does not cover alongside what it does.